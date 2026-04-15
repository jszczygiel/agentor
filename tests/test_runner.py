import json
import os
import stat
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentor.committer import approve_and_commit, reject, retry
from agentor.config import (AgentConfig, Config, GitConfig, ParsingConfig,
                            ReviewConfig, SourcesConfig)
from agentor.models import ItemStatus
from agentor.recovery import recover_on_startup
from agentor.runner import ClaudeRunner, StubRunner, make_runner, plan_worktree
from agentor.store import Store
from agentor.watcher import scan_once


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_project(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@test")
    _git(root, "config", "user.name", "test")
    (root / "README.md").write_text("# project\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")


def _mk_config(root: Path, mode: str = "checkbox",
               watch: list[str] | None = None,
               mark_done: bool = True) -> Config:
    return Config(
        project_name=root.name,
        project_root=root,
        sources=SourcesConfig(
            watch=watch or ["backlog.md"], exclude=[], mark_done=mark_done,
        ),
        parsing=ParsingConfig(mode=mode),
        agent=AgentConfig(pool_size=1),
        git=GitConfig(base_branch="main", branch_prefix="agent/"),
        review=ReviewConfig(),
    )


class TestStubRunner(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] Fix a bug\n  details\n")
        self.cfg = _mk_config(self.root)
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _claim_first(self):
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        return self.store.claim_next_queued(str(wt), br)

    def test_stub_runner_transitions_to_awaiting_review(self):
        claimed = self._claim_first()
        result = StubRunner(self.cfg, self.store).run(claimed)
        self.assertIsNone(result.error)
        self.assertTrue(result.worktree_path.exists())
        self.assertTrue(result.diff)  # untracked file should show
        refreshed = self.store.get(claimed.id)
        self.assertEqual(refreshed.status, ItemStatus.AWAITING_REVIEW)
        self.assertIsNotNone(refreshed.result_json)
        data = json.loads(refreshed.result_json)
        self.assertIn("summary", data)

    def test_approve_commits_and_removes_worktree(self):
        claimed = self._claim_first()
        StubRunner(self.cfg, self.store).run(claimed)
        item = self.store.get(claimed.id)
        wt = Path(item.worktree_path)
        self.assertTrue(wt.exists())
        sha = approve_and_commit(self.cfg, self.store, item, "test commit")
        self.assertTrue(sha)
        self.assertFalse(wt.exists())
        final = self.store.get(claimed.id)
        self.assertEqual(final.status, ItemStatus.MERGED)

    def test_reject_keeps_worktree_and_marks_rejected(self):
        claimed = self._claim_first()
        StubRunner(self.cfg, self.store).run(claimed)
        item = self.store.get(claimed.id)
        wt = Path(item.worktree_path)
        reject(self.store, item, "not what I wanted")
        self.assertEqual(self.store.get(claimed.id).status, ItemStatus.REJECTED)
        self.assertTrue(wt.exists())  # worktree preserved for retry

    def test_retry_requeues(self):
        claimed = self._claim_first()
        StubRunner(self.cfg, self.store).run(claimed)
        item = self.store.get(claimed.id)
        reject(self.store, item, "fb")
        retry(self.store, self.store.get(claimed.id))
        self.assertEqual(self.store.get(claimed.id).status, ItemStatus.QUEUED)


class TestFrontmatterMarkDone(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "backlog").mkdir()
        self.src = self.root / "docs" / "backlog" / "bug-a.md"
        self.src.write_text(
            "---\ntitle: Bug A\nstate: available\n---\nDescription.\n"
        )
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-q", "-m", "add bug")
        self.cfg = _mk_config(
            self.root, mode="frontmatter",
            watch=["docs/backlog/*.md"], mark_done=True,
        )
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_approve_deletes_source_file_in_frontmatter_mode(self):
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        StubRunner(self.cfg, self.store).run(claimed)
        item = self.store.get(claimed.id)
        approve_and_commit(self.cfg, self.store, item, "fix bug A")
        # source file still exists on main (we only removed in worktree, then committed there)
        self.assertTrue(self.src.exists())
        # but on the agent branch, the file is gone
        cp = subprocess.run(
            ["git", "show", f"{item.branch}:docs/backlog/bug-a.md"],
            cwd=self.root, capture_output=True, text=True,
        )
        self.assertNotEqual(cp.returncode, 0)


def _write_fake_claude(bin_dir: Path, script: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "claude"
    fake.write_text("#!/bin/sh\n" + script)
    st = os.stat(fake)
    os.chmod(fake, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake


class TestClaudeRunner(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name) / "proj"
        self.root.mkdir()
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] Add hello file\n  greet world\n")
        self.store = Store(self.root / ".agentor" / "state.db")

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _run_with_fake(self, script: str) -> tuple:
        bin_dir = Path(self.td.name) / "bin"
        _write_fake_claude(bin_dir, script)
        cfg = Config(
            project_name=self.root.name, project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[], mark_done=False),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(
                runner="claude", pool_size=1, max_attempts=1,
                command=[str(bin_dir / "claude"), "-p", "{prompt}"],
                prompt_template="TASK: {title}\n\n{body}",
                timeout_seconds=10,
            ),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )
        scan_once(cfg, self.store)
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        return cfg, claimed, make_runner(cfg, self.store).run(claimed)

    def test_claude_runner_committed_change(self):
        script = """
set -e
echo "HELLO" > hello.txt
git add hello.txt
git -c user.email=x -c user.name=x commit -q -m "add hello"
echo "/develop finished"
"""
        cfg, claimed, result = self._run_with_fake(script)
        self.assertIsNone(result.error, msg=result.error)
        refreshed = self.store.get(claimed.id)
        self.assertEqual(refreshed.status, ItemStatus.AWAITING_REVIEW)
        data = json.loads(refreshed.result_json)
        self.assertIn("hello.txt", data["files_changed"])

    def test_claude_runner_approve_records_existing_sha(self):
        script = """
set -e
echo "HELLO" > hello.txt
git add hello.txt
git -c user.email=x -c user.name=x commit -q -m "add hello"
"""
        cfg, claimed, _ = self._run_with_fake(script)
        item = self.store.get(claimed.id)
        wt = Path(item.worktree_path)
        pre_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=wt,
            capture_output=True, text=True,
        ).stdout.strip()
        sha = approve_and_commit(cfg, self.store, item, "new commit (should not fire)")
        self.assertEqual(sha, pre_sha)
        final = self.store.get(claimed.id)
        self.assertEqual(final.status, ItemStatus.MERGED)
        notes = [t["note"] for t in self.store.transitions_for(claimed.id) if t["note"]]
        self.assertTrue(any("recorded existing commit" in n for n in notes),
                        f"expected 'recorded existing commit' note, got {notes}")
        self.assertFalse(wt.exists())

    def test_claude_runner_fails_on_nonzero_exit(self):
        script = "echo oops >&2\nexit 2\n"
        cfg, claimed, result = self._run_with_fake(script)
        self.assertIsNotNone(result.error)
        refreshed = self.store.get(claimed.id)
        # with max_attempts=1, failure goes to REJECTED
        self.assertEqual(refreshed.status, ItemStatus.REJECTED)

    def test_claude_runner_timeout(self):
        # sleep > timeout
        script = "sleep 5\n"
        bin_dir = Path(self.td.name) / "bin"
        _write_fake_claude(bin_dir, script)
        cfg = Config(
            project_name=self.root.name, project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[], mark_done=False),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(
                runner="claude", pool_size=1, max_attempts=1,
                command=[str(bin_dir / "claude"), "-p", "{prompt}"],
                timeout_seconds=1,
            ),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )
        scan_once(cfg, self.store)
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        result = make_runner(cfg, self.store).run(claimed)
        self.assertIsNotNone(result.error)
        self.assertIn("timed out", result.error)


class TestDaemonPickupModes(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] one\n- [ ] two\n")
        self.store = Store(self.root / ".agentor" / "state.db")

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _cfg(self, pickup_mode: str) -> Config:
        return Config(
            project_name="t", project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[], mark_done=False),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(runner="stub", pool_size=1, max_attempts=1,
                              pickup_mode=pickup_mode),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )

    def test_manual_mode_does_not_auto_dispatch(self):
        from agentor.daemon import Daemon
        from agentor.runner import make_runner
        cfg = self._cfg("manual")
        scan_once(cfg, self.store)
        d = Daemon(cfg, self.store, make_runner, scan_interval=0.05,
                   log=lambda m: None, install_signals=False)
        # run for one tick worth, then signal stop
        import threading
        t = threading.Thread(target=d.run, daemon=True)
        t.start()
        import time as _t
        _t.sleep(0.2)
        d.stop_event.set()
        t.join(timeout=5)
        # still queued, nothing dispatched
        self.assertEqual(d.stats.dispatched, 0)
        self.assertEqual(len(self.store.list_by_status(ItemStatus.QUEUED)), 2)

    def test_dispatch_specific_works_in_manual_mode(self):
        from agentor.daemon import Daemon
        from agentor.runner import make_runner
        cfg = self._cfg("manual")
        scan_once(cfg, self.store)
        d = Daemon(cfg, self.store, make_runner, scan_interval=0.05,
                   log=lambda m: None, install_signals=False)
        import threading, time as _t
        t = threading.Thread(target=d.run, daemon=True)
        t.start()
        _t.sleep(0.1)  # let recovery run
        target = self.store.list_by_status(ItemStatus.QUEUED)[1]
        ok = d.dispatch_specific(target.id)
        self.assertTrue(ok)
        # wait for stub runner to finish
        for _ in range(30):
            if self.store.get(target.id).status != ItemStatus.WORKING:
                break
            _t.sleep(0.1)
        d.stop_event.set()
        t.join(timeout=5)
        final = self.store.get(target.id)
        self.assertEqual(final.status, ItemStatus.AWAITING_REVIEW)


class TestRecovery(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] do X\n")
        self.cfg = _mk_config(self.root)
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_recovery_requeues_stuck_working(self):
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        # simulate crash: item is WORKING but no worktree created
        self.assertEqual(claimed.status, ItemStatus.WORKING)
        recovered = recover_on_startup(self.cfg, self.store)
        self.assertEqual(recovered, [claimed.id])
        self.assertEqual(self.store.get(claimed.id).status, ItemStatus.QUEUED)
        self.assertIsNone(self.store.get(claimed.id).worktree_path)


if __name__ == "__main__":
    unittest.main()
