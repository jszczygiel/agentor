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
        self.assertNotIn("total_cost_usd", data)
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
        # any agent-side failure parks the item in ERRORED (no auto-retry)
        self.assertEqual(refreshed.status, ItemStatus.ERRORED)

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


    def test_recovery_uses_previous_settled_state(self):
        """An item that had reached AWAITING_PLAN_REVIEW and was re-queued
        for execute, then crashed mid-work, should revert to QUEUED (the
        execute-phase wait) — not be sent back to BACKLOG. Verifies the
        recovery hook to previous_settled_status."""
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        self.store.transition(claimed.id, ItemStatus.AWAITING_PLAN_REVIEW)
        self.store.transition(claimed.id, ItemStatus.QUEUED, note="approved")
        # claim again, then "crash" without session_id
        self.store.claim_next_queued(str(wt), br)
        rec = recover_on_startup(self.cfg, self.store)
        # restored to QUEUED (the execute-phase resting state), not back
        # to BACKLOG, and worktree fields cleared.
        item_after = self.store.get(claimed.id)
        self.assertEqual(item_after.status, ItemStatus.QUEUED)
        self.assertIsNone(item_after.worktree_path)
        self.assertIsNone(item_after.session_id)
        self.assertEqual(rec.requeued, [claimed.id])


class TestAutoRecoverySweep(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text(
            "- [ ] one\n- [ ] two\n- [ ] three\n")
        self.cfg = _mk_config(self.root)
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _seed(self, n: int, err: str) -> str:
        items = self.store.list_by_status(ItemStatus.QUEUED)
        item = items[n]
        self.store.transition(
            item.id, ItemStatus.QUEUED, last_error=err,
        )
        # bump attempts via direct SQL — claim/unclaim cycle would overwrite
        self.store.conn.execute(
            "UPDATE items SET attempts = 2 WHERE id = ?", (item.id,))
        return item.id

    def test_shutdown_error_auto_recovered(self):
        iid = self._seed(0, "do_work: claude killed: agentor shutdown")
        rec = recover_on_startup(self.cfg, self.store)
        self.assertIn(iid, rec.auto_recovered)
        item = self.store.get(iid)
        self.assertIsNone(item.last_error)
        self.assertEqual(item.attempts, 0)
        self.assertEqual(item.status, ItemStatus.QUEUED)

    def test_max_cost_error_auto_recovered(self):
        iid = self._seed(0, "do_work: claude killed: max_cost_usd=3.0 hit")
        rec = recover_on_startup(self.cfg, self.store)
        self.assertIn(iid, rec.auto_recovered)

    def test_dead_session_error_auto_recovered(self):
        iid = self._seed(0, "No conversation found with session ID: abc")
        rec = recover_on_startup(self.cfg, self.store)
        self.assertIn(iid, rec.auto_recovered)

    def test_non_benign_error_left_alone(self):
        iid = self._seed(0, "do_work: SyntaxError in foo.py line 42")
        rec = recover_on_startup(self.cfg, self.store)
        self.assertNotIn(iid, rec.auto_recovered)
        item = self.store.get(iid)
        self.assertEqual(item.last_error,
                         "do_work: SyntaxError in foo.py line 42")
        self.assertEqual(item.attempts, 2)

    def test_infra_error_auto_recovered(self):
        iid = self._seed(0, "worktree_add: fatal: not a git repository")
        rec = recover_on_startup(self.cfg, self.store)
        self.assertIn(iid, rec.auto_recovered)
        self.assertIsNone(self.store.get(iid).last_error)

    def test_terminal_state_any_error_cleared(self):
        iid = self._seed(0, "do_work: some unique item-level error")
        self.store.transition(iid, ItemStatus.MERGED, last_error="a")
        rec = recover_on_startup(self.cfg, self.store)
        self.assertIn(iid, rec.auto_recovered)
        self.assertIsNone(self.store.get(iid).last_error)


class TestErrorClassifiers(unittest.TestCase):
    def test_infrastructure_classifier_matches_git_failures(self):
        from agentor.runner import _is_infrastructure_error
        self.assertTrue(_is_infrastructure_error(
            "fatal: not a git repository: /x/y"))
        self.assertTrue(_is_infrastructure_error(
            "fatal: a branch named 'agent/foo' already exists"))
        self.assertTrue(_is_infrastructure_error(
            "is already checked out at /x/y"))

    def test_infrastructure_classifier_skips_item_failures(self):
        from agentor.runner import _is_infrastructure_error
        self.assertFalse(_is_infrastructure_error(
            "claude exited 1: random failure"))
        self.assertFalse(_is_infrastructure_error(
            "claude killed: max_turns=30 hit"))
        self.assertFalse(_is_infrastructure_error(""))

    def test_dead_session_classifier(self):
        from agentor.runner import _is_dead_session_error
        self.assertTrue(_is_dead_session_error(
            "claude exited 1: No conversation found with session ID: abc"))
        self.assertFalse(_is_dead_session_error("anything else"))

    def test_shutdown_classifier(self):
        from agentor.runner import _is_shutdown_error
        self.assertTrue(_is_shutdown_error("claude killed: agentor shutdown"))
        self.assertFalse(_is_shutdown_error("claude exited 1"))

    def test_error_signature_strips_variable_bits(self):
        from agentor.runner import _error_signature
        # Same class → same signature, different magnitude.
        s1 = _error_signature("claude killed: max_turns=30 hit (30 turns)")
        s2 = _error_signature("claude killed: max_turns=50 hit (50 turns)")
        self.assertEqual(s1, s2)
        # Different class → different signature.
        s3 = _error_signature(
            "claude killed: max_cost_usd=3.0 hit (~$3.02)")
        self.assertNotEqual(s1, s3)


class TestProcRegistry(unittest.TestCase):
    def test_register_kill_unregister(self):
        from agentor.runner import ProcRegistry
        reg = ProcRegistry()
        # spawn a real long-running child so kill_all has something to kill
        p = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        reg.register("a", p)
        killed = reg.kill_all(log=lambda m: None)
        self.assertEqual(killed, 1)
        # after a brief moment, process should be dead
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.fail("process not killed by kill_all")
        self.assertIsNotNone(p.returncode)

    def test_kill_all_skips_already_exited(self):
        from agentor.runner import ProcRegistry
        reg = ProcRegistry()
        p = subprocess.Popen(["true"], start_new_session=True)
        p.wait()
        reg.register("a", p)
        # already exited — kill_all reports 0 live, returns 0
        self.assertEqual(reg.kill_all(log=lambda m: None), 0)

    def test_unregister_removes(self):
        from agentor.runner import ProcRegistry
        reg = ProcRegistry()
        p = subprocess.Popen(["true"], start_new_session=True)
        p.wait()
        reg.register("a", p)
        reg.unregister("a")
        self.assertEqual(reg.kill_all(log=lambda m: None), 0)


class _ScriptedRunner(StubRunner):
    """Test runner whose do_work outcome is controlled by a callable. Lets
    us drive the runner through the various failure paths without spawning
    claude."""

    def __init__(self, config, store, behavior):
        super().__init__(config, store)
        self.behavior = behavior  # called with (item, worktree)
        self.calls = 0

    def do_work(self, item, worktree):
        self.calls += 1
        return self.behavior(item, worktree, self.calls)


class TestFailureLandsInErrored(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] failing task\n")
        self.cfg = Config(
            project_name="t", project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[],
                                  mark_done=False),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(runner="stub", pool_size=1, max_attempts=3),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)
        for q in self.store.list_by_status(ItemStatus.BACKLOG):
            self.store.transition(q.id, ItemStatus.QUEUED)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _dispatch(self, runner) -> None:
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        runner.run(claimed)

    def test_first_failure_parks_item_in_errored(self):
        """Any agent-side failure goes straight to ERRORED — no retry loop,
        no attempts-bumping bounce-back to QUEUED. Daemon is free to pick the
        next queued item; operator re-queues via `revert` once fixed."""
        def always_fail(item, wt, n):
            raise RuntimeError("claude killed: max_turns=30 hit (30 turns)")

        runner = _ScriptedRunner(self.cfg, self.store, always_fail)
        self._dispatch(runner)
        errored = self.store.list_by_status(ItemStatus.ERRORED)
        self.assertEqual(len(errored), 1)
        self.assertIn("max_turns", errored[0].last_error)
        self.assertEqual(self.store.list_by_status(ItemStatus.QUEUED), [])


class TestRunnerRecordsFailures(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] one\n")
        self.cfg = Config(
            project_name="t", project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[],
                                  mark_done=False),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(runner="stub", pool_size=1, max_attempts=3),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)
        for q in self.store.list_by_status(ItemStatus.BACKLOG):
            self.store.transition(q.id, ItemStatus.QUEUED)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_do_work_exception_records_failure_row(self):
        class _Boom(StubRunner):
            def do_work(self, item, wt):
                raise RuntimeError("kaboom")

        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        _Boom(self.cfg, self.store).run(claimed)
        rows = self.store.list_failures(claimed.id)
        self.assertEqual(len(rows), 1)
        self.assertIn("kaboom", rows[0]["error"])
        self.assertEqual(rows[0]["phase"], "do_work")
        self.assertEqual(rows[0]["attempt"], 1)
        self.assertIsNotNone(rows[0]["error_sig"])


class TestDaemonPause(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] task\n")
        self.cfg = Config(
            project_name="t", project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[],
                                  mark_done=False),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(runner="stub", pool_size=1, max_attempts=3,
                              pickup_mode="auto"),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_infra_error_pauses_daemon_and_sets_alert(self):
        from agentor.daemon import Daemon
        from agentor.runner import InfrastructureError

        def factory(cfg, store):
            r = StubRunner(cfg, store)

            def boom(item, wt):
                raise InfrastructureError("fatal: not a git repository")
            r.do_work = boom
            return r

        d = Daemon(self.cfg, self.store, factory, scan_interval=0.05,
                   log=lambda m: None, install_signals=False)
        import threading as _t, time as _tm
        t = _t.Thread(target=d.run, daemon=True)
        t.start()
        # wait until the alert flips
        for _ in range(40):
            if d.system_alert is not None:
                break
            _tm.sleep(0.05)
        d.stop_event.set()
        t.join(timeout=5)
        self.assertIsNotNone(d.system_alert)
        self.assertTrue(d.paused)
        # dispatch refuses while paused
        self.assertFalse(d._dispatch_one())

    def test_clear_alert_resumes_dispatch(self):
        from agentor.daemon import Daemon
        d = Daemon(self.cfg, self.store, lambda c, s: StubRunner(c, s),
                   scan_interval=0.05, log=lambda m: None,
                   install_signals=False)
        d.system_alert = "previously broken"
        d.paused = True
        d.clear_alert()
        self.assertIsNone(d.system_alert)
        self.assertFalse(d.paused)


class TestBranchCleanupHelpers(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)

    def tearDown(self):
        self.td.cleanup()

    def test_branch_checked_out_at_finds_holding_worktree(self):
        from agentor import git_ops
        wt = self.root / "wt-x"
        git_ops.worktree_add(self.root, wt, "agent/x", "main")
        held = git_ops.branch_checked_out_at(self.root, "agent/x")
        self.assertIsNotNone(held)
        self.assertEqual(held.resolve(), wt.resolve())

    def test_branch_checked_out_at_returns_none_when_unheld(self):
        from agentor import git_ops
        # branch exists but no worktree holds it
        subprocess.run(["git", "branch", "agent/y", "main"],
                       cwd=self.root, check=True)
        self.assertIsNone(
            git_ops.branch_checked_out_at(self.root, "agent/y"),
        )


if __name__ == "__main__":
    unittest.main()
