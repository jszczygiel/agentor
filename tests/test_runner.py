import json
import os
import stat
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentor.committer import (approve_and_commit, approve_plan, defer,
                                reject, restore_deferred, retry)
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

    def _run_with_fake(self, script: str, full_cycle: bool = True,
                       max_attempts: int = 2) -> tuple:
        """Spawns the runner against a fake claude. When `full_cycle=True` (the
        default) drives plan → approve_plan → execute and returns the execute
        phase's RunResult; otherwise returns after the plan phase only."""
        bin_dir = Path(self.td.name) / "bin"
        _write_fake_claude(bin_dir, script)
        cfg = Config(
            project_name=self.root.name, project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[], mark_done=False),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(
                runner="claude", pool_size=1, max_attempts=max_attempts,
                command=[str(bin_dir / "claude"), "-p", "{prompt}"],
                plan_prompt_template="PLAN: {title}",
                execute_prompt_template="EXEC: {title}\nplan={plan}",
                timeout_seconds=10,
            ),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )
        scan_once(cfg, self.store)
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        runner = make_runner(cfg, self.store)
        result = runner.run(claimed)
        if not full_cycle or result.error:
            return cfg, claimed, result
        fresh = self.store.get(claimed.id)
        if fresh.status != ItemStatus.AWAITING_PLAN_REVIEW:
            return cfg, claimed, result
        approve_plan(self.store, fresh)
        # re-claim for execute phase
        wt2, br2 = plan_worktree(cfg, fresh)
        claimed2 = self.store.claim_next_queued(str(wt2), br2)
        exec_result = runner.run(claimed2)
        return cfg, claimed2, exec_result

    def test_claude_runner_committed_change(self):
        script = """
set -e
if [ ! -f hello.txt ]; then
  echo "HELLO" > hello.txt
  git add hello.txt
  git -c user.email=x -c user.name=x commit -q -m "add hello"
fi
echo "/develop finished"
"""
        cfg, claimed, result = self._run_with_fake(script)
        self.assertIsNone(result.error, msg=result.error)
        refreshed = self.store.get(claimed.id)
        self.assertEqual(refreshed.status, ItemStatus.AWAITING_REVIEW)
        data = json.loads(refreshed.result_json)
        self.assertEqual(data.get("phase"), "execute")
        self.assertIn("hello.txt", data["files_changed"])

    def test_claude_runner_approve_records_existing_sha(self):
        script = """
set -e
if [ ! -f hello.txt ]; then
  echo "HELLO" > hello.txt
  git add hello.txt
  git -c user.email=x -c user.name=x commit -q -m "add hello"
fi
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

    def test_claude_runner_stream_json_live_updates(self):
        """The streaming path should read each stream-json event, populate
        iterations + modelUsage, and publish a `live=True` snapshot to
        result_json before the final transition."""
        # Two assistant events + a terminal result event.
        script = r"""printf '%s\n' '{"type":"system","subtype":"init","session_id":"sess-xyz"}'
printf '%s\n' '{"type":"assistant","message":{"role":"assistant","model":"claude-opus-4-6","usage":{"input_tokens":100,"cache_read_input_tokens":5000,"cache_creation_input_tokens":200,"output_tokens":50}}}'
printf '%s\n' '{"type":"assistant","message":{"role":"assistant","model":"claude-opus-4-6","usage":{"input_tokens":5,"cache_read_input_tokens":5300,"cache_creation_input_tokens":0,"output_tokens":120}}}'
printf '%s\n' '{"type":"result","subtype":"success","result":"here is the plan","num_turns":2,"total_cost_usd":0.04,"stop_reason":"end_turn","modelUsage":{"claude-opus-4-6":{"inputTokens":105,"outputTokens":170,"cacheReadInputTokens":10300,"cacheCreationInputTokens":200,"costUSD":0.04,"contextWindow":1000000}}}'
"""
        bin_dir = Path(self.td.name) / "bin"
        _write_fake_claude(bin_dir, script)
        cfg = Config(
            project_name=self.root.name, project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[], mark_done=False),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(
                runner="claude", pool_size=1, max_attempts=2,
                command=[str(bin_dir / "claude"), "-p", "{prompt}",
                         "--output-format", "stream-json", "--verbose"],
                plan_prompt_template="PLAN: {title}",
                execute_prompt_template="EXEC: {title}\nplan={plan}",
                timeout_seconds=10,
            ),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )
        scan_once(cfg, self.store)
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        runner = make_runner(cfg, self.store)
        result = runner.run(claimed)
        self.assertIsNone(result.error, msg=result.error)
        refreshed = self.store.get(claimed.id)
        self.assertEqual(refreshed.status, ItemStatus.AWAITING_PLAN_REVIEW)
        data = json.loads(refreshed.result_json)
        self.assertEqual(data["phase"], "plan")
        self.assertEqual(data["plan"], "here is the plan")
        self.assertEqual(data["num_turns"], 2)
        self.assertAlmostEqual(data["total_cost_usd"], 0.04)
        iters = data["iterations"]
        self.assertEqual(len(iters), 2)
        self.assertEqual(iters[-1]["cache_read_input_tokens"], 5300)
        self.assertIn("claude-opus-4-6", data["modelUsage"])

    def test_claude_runner_plan_phase_lands_in_plan_review(self):
        """First run (no prior session) is the planning phase; item lands in
        AWAITING_PLAN_REVIEW with phase=plan persisted for the execute pass."""
        script = 'echo "I will add hello.txt, then commit."\n'
        cfg, claimed, result = self._run_with_fake(script, full_cycle=False)
        self.assertIsNone(result.error, msg=result.error)
        refreshed = self.store.get(claimed.id)
        self.assertEqual(refreshed.status, ItemStatus.AWAITING_PLAN_REVIEW)
        data = json.loads(refreshed.result_json)
        self.assertEqual(data["phase"], "plan")
        self.assertTrue(data.get("plan"))
        # session_id must be set so the execute phase can --resume
        self.assertIsNotNone(refreshed.session_id)

    def test_claude_runner_fails_on_nonzero_exit(self):
        script = "echo oops >&2\nexit 2\n"
        cfg, claimed, result = self._run_with_fake(
            script, full_cycle=False, max_attempts=1,
        )
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


class TestDeferred(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] one\n- [ ] two\n")
        self.cfg = Config(
            project_name="t", project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[]),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(pool_size=1, max_attempts=1),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_defer_from_queued_then_restore(self):
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        defer(self.store, item)
        self.assertEqual(self.store.get(item.id).status, ItemStatus.DEFERRED)
        target = restore_deferred(self.store, self.store.get(item.id))
        self.assertEqual(target, ItemStatus.QUEUED)
        self.assertEqual(self.store.get(item.id).status, ItemStatus.QUEUED)

    def test_defer_from_awaiting_then_restore(self):
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        StubRunner(self.cfg, self.store).run(claimed)
        item = self.store.get(claimed.id)
        self.assertEqual(item.status, ItemStatus.AWAITING_REVIEW)
        defer(self.store, item)
        self.assertEqual(self.store.get(item.id).status, ItemStatus.DEFERRED)
        target = restore_deferred(self.store, self.store.get(item.id))
        self.assertEqual(target, ItemStatus.AWAITING_REVIEW)


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

    def test_manual_mode_keeps_items_in_backlog(self):
        """In manual pickup mode, discovery lands items in BACKLOG and the
        daemon refuses to dispatch them until a human approves them into
        QUEUED."""
        from agentor.daemon import Daemon
        from agentor.runner import make_runner
        cfg = self._cfg("manual")
        scan_once(cfg, self.store)
        d = Daemon(cfg, self.store, make_runner, scan_interval=0.05,
                   log=lambda m: None, install_signals=False)
        import threading
        t = threading.Thread(target=d.run, daemon=True)
        t.start()
        import time as _t
        _t.sleep(0.2)
        d.stop_event.set()
        t.join(timeout=5)
        # still in backlog, nothing dispatched
        self.assertEqual(d.stats.dispatched, 0)
        self.assertEqual(len(self.store.list_by_status(ItemStatus.BACKLOG)), 2)
        self.assertEqual(len(self.store.list_by_status(ItemStatus.QUEUED)), 0)

    def test_auto_mode_promotes_discovery_to_queued(self):
        """In auto pickup mode, scan_once auto-promotes new items from
        BACKLOG into QUEUED so the daemon can claim them without human
        intervention."""
        from agentor.daemon import Daemon
        from agentor.runner import make_runner
        cfg = self._cfg("auto")
        d = Daemon(cfg, self.store, make_runner, scan_interval=0.05,
                   log=lambda m: None, install_signals=False)
        import threading, time as _t
        t = threading.Thread(target=d.run, daemon=True)
        t.start()
        _t.sleep(0.3)
        d.stop_event.set()
        t.join(timeout=5)
        # at least one item should have been dispatched by the daemon loop
        self.assertGreaterEqual(d.stats.dispatched, 1)
        self.assertEqual(len(self.store.list_by_status(ItemStatus.BACKLOG)), 0)

    def test_dispatch_specific_works_in_manual_mode(self):
        from agentor.committer import approve_backlog
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
        # items are in BACKLOG; promote the second one manually then dispatch.
        target = self.store.list_by_status(ItemStatus.BACKLOG)[1]
        approve_backlog(self.store, target)
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
        rec = recover_on_startup(self.cfg, self.store)
        self.assertEqual(rec.requeued, [claimed.id])
        self.assertEqual(rec.resumable, [])
        self.assertEqual(self.store.get(claimed.id).status, ItemStatus.QUEUED)
        self.assertIsNone(self.store.get(claimed.id).worktree_path)

    def test_recovery_preserves_resumable_session(self):
        """Item with session_id + live worktree → keep WORKING, mark resumable."""
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        wt.mkdir(parents=True, exist_ok=True)
        self.store.transition(
            claimed.id, ItemStatus.WORKING,
            session_id="abcd-1234", note="session assigned",
        )
        rec = recover_on_startup(self.cfg, self.store)
        self.assertEqual(rec.requeued, [])
        self.assertEqual(len(rec.resumable), 1)
        self.assertEqual(rec.resumable[0].id, claimed.id)
        self.assertEqual(
            self.store.get(claimed.id).status, ItemStatus.WORKING,
        )


if __name__ == "__main__":
    unittest.main()
