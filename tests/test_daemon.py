import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentor.config import (AgentConfig, Config, GitConfig, ParsingConfig,
                            ReviewConfig, SourcesConfig)
from agentor.daemon import Daemon
from agentor.models import Item, ItemStatus
from agentor.runner import InfrastructureError, RunResult, Runner
from agentor.store import Store


def _mk_item(id: str, title: str = "T", body: str = "B") -> Item:
    return Item(
        id=id, title=title, body=body,
        source_file="backlog.md", source_line=1, tags={},
    )


def _mk_config(root: Path, pool_size: int = 1, max_attempts: int = 3) -> Config:
    return Config(
        project_name="t",
        project_root=root,
        sources=SourcesConfig(),
        parsing=ParsingConfig(),
        agent=AgentConfig(pool_size=pool_size, max_attempts=max_attempts),
        git=GitConfig(),
        review=ReviewConfig(),
    )


class FakeRunner(Runner):
    """Test runner — behavior injected via `behavior` callable."""

    def __init__(self, config, store, behavior):
        super().__init__(config, store)
        self.behavior = behavior
        self.started = threading.Event()

    def run(self, item):
        self.started.set()
        return self.behavior(self, item)


def _drain_workers(daemon: Daemon, timeout: float = 2.0) -> None:
    for t in list(daemon.workers):
        t.join(timeout=timeout)


class TestDispatchPoolCap(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        self.store = Store(self.root / ".agentor" / "state.db")
        self.block = threading.Event()  # held high = runners hang

    def tearDown(self):
        self.block.set()  # release any hanging runners
        self.store.close()
        self.td.cleanup()

    def _queue(self, id: str) -> None:
        self.store.upsert_discovered(_mk_item(id))
        self.store.transition(id, ItemStatus.QUEUED)

    def _factory(self):
        block = self.block

        def behavior(runner, item):
            block.wait(timeout=2)
            return RunResult(item.id, Path("/x"), "br", "ok", [], "")

        def make(cfg, store):
            return FakeRunner(cfg, store, behavior)

        return make

    def test_does_not_dispatch_past_pool_size(self):
        for i in ("a", "b", "c"):
            self._queue(i)
        cfg = _mk_config(self.root, pool_size=2)
        d = Daemon(cfg, self.store, self._factory(),
                   install_signals=False, log=lambda m: None)
        self.assertTrue(d._dispatch_one())
        self.assertTrue(d._dispatch_one())
        # Pool full: third dispatch refused.
        self.assertFalse(d._dispatch_one())
        self.assertEqual(
            self.store.count_by_status(ItemStatus.WORKING), 2)
        self.assertEqual(d.stats.dispatched, 2)
        # Release blocked runners and drain.
        self.block.set()
        _drain_workers(d)

    def test_pool_size_zero_never_dispatches(self):
        self._queue("a")
        cfg = _mk_config(self.root, pool_size=0)
        d = Daemon(cfg, self.store, self._factory(),
                   install_signals=False, log=lambda m: None)
        self.assertFalse(d._dispatch_one())
        self.assertEqual(d.stats.dispatched, 0)

    def test_try_fill_pool_dispatches_up_to_cap(self):
        for i in ("a", "b", "c"):
            self._queue(i)
        cfg = _mk_config(self.root, pool_size=2)
        d = Daemon(cfg, self.store, self._factory(),
                   install_signals=False, log=lambda m: None)
        n = d.try_fill_pool()
        self.assertEqual(n, 2)
        self.assertEqual(
            self.store.count_by_status(ItemStatus.WORKING), 2)
        self.block.set()
        _drain_workers(d)

    def test_empty_queue_returns_false(self):
        cfg = _mk_config(self.root, pool_size=2)
        d = Daemon(cfg, self.store, self._factory(),
                   install_signals=False, log=lambda m: None)
        self.assertFalse(d._dispatch_one())

    def test_exhausted_attempts_auto_rejected(self):
        """Queued items whose attempts ≥ max_attempts never enter WORKING —
        they are moved to REJECTED in-place."""
        self._queue("a")
        # Bump attempts past the cap.
        self.store.transition("a", ItemStatus.QUEUED, attempts=3)
        cfg = _mk_config(self.root, pool_size=2, max_attempts=3)
        d = Daemon(cfg, self.store, self._factory(),
                   install_signals=False, log=lambda m: None)
        self.assertFalse(d._dispatch_one())
        item = self.store.get("a")
        self.assertEqual(item.status, ItemStatus.REJECTED)
        self.assertIn("max_attempts", (item.last_error or ""))


class TestDispatchStagger(unittest.TestCase):
    """try_fill_pool waits between successive dispatches when
    dispatch_stagger_seconds > 0 so the first agent populates the shared
    system-prompt cache before siblings race for the same prefix."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        self.store = Store(self.root / ".agentor" / "state.db")
        self.block = threading.Event()

    def tearDown(self):
        self.block.set()
        self.store.close()
        self.td.cleanup()

    def _queue(self, id: str) -> None:
        self.store.upsert_discovered(_mk_item(id))
        self.store.transition(id, ItemStatus.QUEUED)

    def _factory(self):
        block = self.block

        def behavior(runner, item):
            block.wait(timeout=2)
            return RunResult(item.id, Path("/x"), "br", "ok", [], "")

        def make(cfg, store):
            return FakeRunner(cfg, store, behavior)

        return make

    def _mk_daemon(self, pool_size: int, stagger: float) -> Daemon:
        cfg = _mk_config(self.root, pool_size=pool_size)
        cfg.agent.dispatch_stagger_seconds = stagger
        d = Daemon(cfg, self.store, self._factory(),
                   install_signals=False, log=lambda m: None)
        d._stagger_waits = []  # type: ignore[attr-defined]
        d._stagger_wait = d._stagger_waits.append  # type: ignore[method-assign]
        return d

    def test_stagger_between_sibling_dispatches(self):
        for i in ("a", "b", "c"):
            self._queue(i)
        d = self._mk_daemon(pool_size=3, stagger=2.5)
        n = d.try_fill_pool()
        self.assertEqual(n, 3)
        # Two gaps between three dispatches, each the configured duration.
        self.assertEqual(d._stagger_waits, [2.5, 2.5])
        self.block.set()
        _drain_workers(d)

    def test_no_stagger_when_zero(self):
        for i in ("a", "b", "c"):
            self._queue(i)
        d = self._mk_daemon(pool_size=3, stagger=0.0)
        n = d.try_fill_pool()
        self.assertEqual(n, 3)
        self.assertEqual(d._stagger_waits, [])
        self.block.set()
        _drain_workers(d)

    def test_no_stagger_for_solo_dispatch(self):
        """Single-item burst (pool holds one slot) must not sleep — there
        are no siblings to share the cache with."""
        self._queue("a")
        d = self._mk_daemon(pool_size=1, stagger=3.0)
        n = d.try_fill_pool()
        self.assertEqual(n, 1)
        # pool_has_slot is False after filling the only slot, so no wait.
        self.assertEqual(d._stagger_waits, [])
        self.block.set()
        _drain_workers(d)

    def test_no_stagger_after_final_dispatch_in_burst(self):
        """Three items, three-slot pool — two staggers total (between 1-2
        and 2-3), nothing trailing after the last item fills the pool."""
        for i in ("a", "b", "c"):
            self._queue(i)
        d = self._mk_daemon(pool_size=3, stagger=1.0)
        n = d.try_fill_pool()
        self.assertEqual(n, 3)
        self.assertEqual(len(d._stagger_waits), 2)
        self.block.set()
        _drain_workers(d)


class TestInfraErrorStickyAlert(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        self.store = Store(self.root / ".agentor" / "state.db")

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _queue(self, id: str) -> None:
        self.store.upsert_discovered(_mk_item(id))
        self.store.transition(id, ItemStatus.QUEUED)

    def test_infra_error_sets_alert_and_pauses(self):
        self._queue("a")

        def behavior(runner, item):
            raise InfrastructureError("fatal: not a git repository")

        def factory(cfg, store):
            return FakeRunner(cfg, store, behavior)

        cfg = _mk_config(self.root, pool_size=2)
        logs: list[str] = []
        d = Daemon(cfg, self.store, factory,
                   install_signals=False, log=logs.append)
        self.assertTrue(d._dispatch_one())
        _drain_workers(d)
        self.assertEqual(d.system_alert, "fatal: not a git repository")
        self.assertTrue(d.paused)
        self.assertTrue(any("[ALERT]" in m for m in logs))
        self.assertTrue(any("press 'u'" in m for m in logs))
        # Failure counter NOT incremented (system failed, not the item).
        self.assertEqual(d.stats.failed, 0)

    def test_paused_daemon_refuses_further_dispatch(self):
        self._queue("a")
        self._queue("b")

        def behavior(runner, item):
            raise InfrastructureError("slot broken")

        def factory(cfg, store):
            return FakeRunner(cfg, store, behavior)

        cfg = _mk_config(self.root, pool_size=2)
        d = Daemon(cfg, self.store, factory,
                   install_signals=False, log=lambda m: None)
        self.assertTrue(d._dispatch_one())
        _drain_workers(d)
        self.assertTrue(d.paused)
        # Second dispatch refused while alert is sticky.
        self.assertFalse(d._dispatch_one())
        # Manual dispatch also refused.
        self.assertFalse(d.dispatch_specific("b"))

    def test_clear_alert_resumes_dispatch(self):
        self._queue("a")

        def behavior(runner, item):
            raise InfrastructureError("broken")

        def factory(cfg, store):
            return FakeRunner(cfg, store, behavior)

        cfg = _mk_config(self.root, pool_size=1)
        d = Daemon(cfg, self.store, factory,
                   install_signals=False, log=lambda m: None)
        d._dispatch_one()
        _drain_workers(d)
        self.assertTrue(d.paused)
        d.clear_alert()
        self.assertIsNone(d.system_alert)
        self.assertFalse(d.paused)

    def test_regular_exception_counts_as_failure(self):
        """Non-infra exceptions bump stats.failed and do NOT trigger alert."""
        self._queue("a")

        def behavior(runner, item):
            raise RuntimeError("boom")

        def factory(cfg, store):
            return FakeRunner(cfg, store, behavior)

        cfg = _mk_config(self.root, pool_size=1)
        d = Daemon(cfg, self.store, factory,
                   install_signals=False, log=lambda m: None)
        d._dispatch_one()
        _drain_workers(d)
        self.assertEqual(d.stats.failed, 1)
        self.assertIsNone(d.system_alert)
        self.assertFalse(d.paused)

    def test_successful_run_increments_completed(self):
        self._queue("a")

        def behavior(runner, item):
            return RunResult(item.id, Path("/x"), "br", "done", [], "")

        def factory(cfg, store):
            return FakeRunner(cfg, store, behavior)

        cfg = _mk_config(self.root, pool_size=1)
        d = Daemon(cfg, self.store, factory,
                   install_signals=False, log=lambda m: None)
        d._dispatch_one()
        _drain_workers(d)
        self.assertEqual(d.stats.completed, 1)
        self.assertEqual(d.stats.failed, 0)

    def test_run_result_with_error_counts_as_failure(self):
        self._queue("a")

        def behavior(runner, item):
            return RunResult(item.id, Path("/x"), "br", "", [], "",
                             error="agent error")

        def factory(cfg, store):
            return FakeRunner(cfg, store, behavior)

        cfg = _mk_config(self.root, pool_size=1)
        d = Daemon(cfg, self.store, factory,
                   install_signals=False, log=lambda m: None)
        d._dispatch_one()
        _drain_workers(d)
        self.assertEqual(d.stats.failed, 1)
        self.assertEqual(d.stats.completed, 0)


class TestDispatchSpecific(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        self.store = Store(self.root / ".agentor" / "state.db")
        self.block = threading.Event()

    def tearDown(self):
        self.block.set()
        self.store.close()
        self.td.cleanup()

    def _queue(self, id: str) -> None:
        self.store.upsert_discovered(_mk_item(id))
        self.store.transition(id, ItemStatus.QUEUED)

    def _factory(self):
        block = self.block

        def behavior(runner, item):
            block.wait(timeout=2)
            return RunResult(item.id, Path("/x"), "br", "ok", [], "")

        def make(cfg, store):
            return FakeRunner(cfg, store, behavior)
        return make

    def test_dispatches_specific_queued_item(self):
        self._queue("a")
        self._queue("b")
        cfg = _mk_config(self.root, pool_size=2)
        d = Daemon(cfg, self.store, self._factory(),
                   install_signals=False, log=lambda m: None)
        self.assertTrue(d.dispatch_specific("b"))
        item = self.store.get("b")
        self.assertEqual(item.status, ItemStatus.WORKING)
        # `a` still queued.
        self.assertEqual(self.store.get("a").status, ItemStatus.QUEUED)
        self.block.set()
        _drain_workers(d)

    def test_refuses_when_pool_full(self):
        self._queue("a")
        self._queue("b")
        cfg = _mk_config(self.root, pool_size=1)
        d = Daemon(cfg, self.store, self._factory(),
                   install_signals=False, log=lambda m: None)
        d._dispatch_one()  # fills the pool with "a"
        self.assertFalse(d.dispatch_specific("b"))
        self.assertEqual(self.store.get("b").status, ItemStatus.QUEUED)
        self.block.set()
        _drain_workers(d)

    def test_refuses_unknown_item(self):
        cfg = _mk_config(self.root, pool_size=1)
        d = Daemon(cfg, self.store, self._factory(),
                   install_signals=False, log=lambda m: None)
        self.assertFalse(d.dispatch_specific("ghost"))

    def test_refuses_non_queued_item(self):
        self._queue("a")
        self.store.transition("a", ItemStatus.WORKING)
        cfg = _mk_config(self.root, pool_size=1)
        d = Daemon(cfg, self.store, self._factory(),
                   install_signals=False, log=lambda m: None)
        self.assertFalse(d.dispatch_specific("a"))


class TestHeartbeatLog(unittest.TestCase):
    """Heartbeat fires when the daemon's main loop has been idle (no new
    items, no new dispatches, no live workers) for `heartbeat_interval`
    seconds. We exercise the decision helper directly — running the full
    loop and faking time across threads invited sync bugs."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        self.store = Store(self.root / ".agentor" / "state.db")
        self.logs: list[str] = []
        cfg = _mk_config(self.root, pool_size=0)

        def runner_factory(cfg, store):
            return FakeRunner(cfg, store, lambda *a: None)

        self.d = Daemon(
            cfg, self.store, runner_factory,
            scan_interval=0.01, install_signals=False,
            log=self.logs.append,
        )
        self.d.heartbeat_interval = 0.0
        self.d._heartbeat_last = 0.0
        self.d._heartbeat_dispatched = 0

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_heartbeat_emits_when_idle(self):
        self.d._maybe_log_heartbeat(new_items=0)
        heartbeats = [m for m in self.logs if m.startswith("heartbeat:")]
        self.assertEqual(len(heartbeats), 1, self.logs)

    def test_heartbeat_silent_when_new_items(self):
        self.d._maybe_log_heartbeat(new_items=3)
        self.assertFalse([m for m in self.logs
                          if m.startswith("heartbeat:")])

    def test_heartbeat_silent_when_workers_live(self):
        self.d.workers.add(threading.current_thread())
        try:
            self.d._maybe_log_heartbeat(new_items=0)
        finally:
            self.d.workers.discard(threading.current_thread())
        self.assertFalse([m for m in self.logs
                          if m.startswith("heartbeat:")])

    def test_heartbeat_respects_interval_gap(self):
        self.d.heartbeat_interval = 300.0  # never fire inside this test
        self.d._heartbeat_last = 1e6
        # Make monotonic-clock comparison short of the interval by seeding
        # `_heartbeat_last` to a future timestamp.
        import agentor.daemon as daemon_mod
        orig = daemon_mod.time.monotonic
        daemon_mod.time.monotonic = lambda: 0.0
        try:
            self.d._maybe_log_heartbeat(new_items=0)
        finally:
            daemon_mod.time.monotonic = orig
        self.assertFalse([m for m in self.logs
                          if m.startswith("heartbeat:")])


class TestFoldAutoQueueInLoop(unittest.TestCase):
    """The daemon's main loop calls `maybe_enqueue_fold_item` once per tick
    after `try_fill_pool`. With enough agent-logs accumulated, a backlog
    file is created, `scan_once` on the next tick enqueues it, and a
    queued item runs through the normal dispatch path to AWAITING_REVIEW."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        self.store = Store(self.root / ".agentor" / "state.db")

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _seed_logs(self, n: int) -> None:
        d = self.root / "docs" / "agent-logs"
        d.mkdir(parents=True, exist_ok=True)
        for i in range(n):
            (d / f"2026-04-{i:02d}-note.md").write_text("x\n")

    def test_one_tick_creates_and_queues(self):
        self._seed_logs(10)
        cfg = Config(
            project_name="t",
            project_root=self.root,
            sources=SourcesConfig(watch=["docs/backlog/*.md"]),
            parsing=ParsingConfig(mode="frontmatter"),
            agent=AgentConfig(pool_size=0, fold_threshold=10),
            git=GitConfig(),
            review=ReviewConfig(),
        )

        def factory(c, s):
            return FakeRunner(c, s, lambda r, i: None)

        logs: list[str] = []
        d = Daemon(cfg, self.store, factory,
                   install_signals=False, log=logs.append)

        # Exercise the hook directly — the daemon calls this helper in
        # its main loop, and wrapping it in the stop_event dance would
        # drag real git-worktree setup into a unit test.
        from agentor.fold import maybe_enqueue_fold_item
        created = maybe_enqueue_fold_item(d.config, d.store)
        self.assertIsNotNone(created)
        self.assertTrue(created.exists())

        # scan_once is the next step the real loop takes — it lifts the
        # new backlog file into a QUEUED row.
        from agentor.watcher import scan_once
        result = scan_once(d.config, d.store)
        self.assertEqual(result.new_items, 1)
        queued = self.store.list_by_status(ItemStatus.QUEUED)
        self.assertEqual(len(queued), 1)
        self.assertTrue(queued[0].title.startswith("Fold agent log lessons"))
        self.assertEqual(queued[0].tags.get("category"), "meta")

        # Second tick: the guard sees a QUEUED fold item, so no duplicate
        # file or duplicate row even though the logs directory is still
        # above threshold.
        again = maybe_enqueue_fold_item(d.config, d.store)
        self.assertIsNone(again)
        scan_once(d.config, d.store)
        self.assertEqual(
            len(self.store.list_by_status(ItemStatus.QUEUED)), 1,
        )


class TestStaleSessionWatchdog(unittest.TestCase):
    """Detects WORKING items whose transcript hasn't been touched in a
    while. Informational — the process is not killed, dispatch is not
    paused; `timeout_seconds` still owns the terminal decision."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        self.store = Store(self.root / ".agentor" / "state.db")
        self.logs: list[str] = []
        self.transcripts_dir = self.root / ".agentor" / "transcripts"
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _mk_daemon(self, threshold: int = 300) -> Daemon:
        cfg = Config(
            project_name="t",
            project_root=self.root,
            sources=SourcesConfig(),
            parsing=ParsingConfig(),
            agent=AgentConfig(
                pool_size=1,
                stale_session_alert_seconds=threshold,
            ),
            git=GitConfig(),
            review=ReviewConfig(),
        )

        def factory(c, s):
            return FakeRunner(c, s, lambda r, i: None)

        return Daemon(cfg, self.store, factory,
                      install_signals=False, log=self.logs.append)

    def _seed_working(self, id: str = "a", session_id: str = "sess-x") -> None:
        self.store.upsert_discovered(_mk_item(id))
        self.store.transition(id, ItemStatus.QUEUED)
        self.store.transition(
            id, ItemStatus.WORKING,
            worktree_path=str(self.root / "wt"),
            branch="agent/test",
            session_id=session_id,
        )

    def _write_transcript(self, id: str, phase: str, age_seconds: float) -> int:
        import os
        p = self.transcripts_dir / f"{id}.{phase}.log"
        p.write_text("dummy\n")
        mtime = time.time() - age_seconds
        os.utime(p, (mtime, mtime))
        return p.stat().st_mtime_ns

    def test_stale_transcript_sets_alert(self):
        self._seed_working("a")
        old_mtime_ns = self._write_transcript("a", "execute", age_seconds=310)
        d = self._mk_daemon(threshold=300)
        d._check_stale_sessions(time.time_ns())
        self.assertEqual(d.stale_session_alerts, {"a": old_mtime_ns})
        self.assertFalse(d.paused)
        self.assertEqual(d.stats.failed, 0)
        alerts = [m for m in self.logs if "[ALERT]" in m and "a" in m]
        self.assertEqual(len(alerts), 1, self.logs)
        self.assertIn("stale session", alerts[0])

    def test_fresh_transcript_no_alert(self):
        self._seed_working("a")
        self._write_transcript("a", "execute", age_seconds=5)
        d = self._mk_daemon(threshold=300)
        d._check_stale_sessions(time.time_ns())
        self.assertEqual(d.stale_session_alerts, {})
        self.assertFalse(
            [m for m in self.logs if "[ALERT]" in m],
        )

    def test_missing_transcript_no_crash(self):
        self._seed_working("a")
        # no transcript written
        d = self._mk_daemon(threshold=300)
        d._check_stale_sessions(time.time_ns())
        self.assertEqual(d.stale_session_alerts, {})

    def test_threshold_zero_disables(self):
        self._seed_working("a")
        self._write_transcript("a", "execute", age_seconds=99999)
        d = self._mk_daemon(threshold=0)
        d._check_stale_sessions(time.time_ns())
        self.assertEqual(d.stale_session_alerts, {})

    def test_no_session_id_skipped(self):
        """WORKING items without a live session_id are not yet dispatched
        through a claude/codex session — skip them so we don't alert on
        stub-runner fixtures or pre-session setup."""
        self.store.upsert_discovered(_mk_item("a"))
        self.store.transition("a", ItemStatus.QUEUED)
        self.store.transition(
            "a", ItemStatus.WORKING,
            worktree_path=str(self.root / "wt"), branch="agent/test",
        )
        self._write_transcript("a", "execute", age_seconds=600)
        d = self._mk_daemon(threshold=300)
        d._check_stale_sessions(time.time_ns())
        self.assertEqual(d.stale_session_alerts, {})

    def test_dedupe_on_same_mtime(self):
        self._seed_working("a")
        self._write_transcript("a", "execute", age_seconds=400)
        d = self._mk_daemon(threshold=300)
        d._check_stale_sessions(time.time_ns())
        d._check_stale_sessions(time.time_ns())
        alerts = [m for m in self.logs if "[ALERT]" in m]
        self.assertEqual(len(alerts), 1, self.logs)

    def test_clear_alert_wipes_active_only(self):
        self._seed_working("a")
        self._write_transcript("a", "execute", age_seconds=400)
        d = self._mk_daemon(threshold=300)
        d._check_stale_sessions(time.time_ns())
        self.assertIn("a", d.stale_session_alerts)
        d.clear_alert()
        self.assertEqual(d.stale_session_alerts, {})
        # Next tick with same mtime stays muted via dedupe memory.
        d._check_stale_sessions(time.time_ns())
        self.assertEqual(d.stale_session_alerts, {})
        alerts = [m for m in self.logs if "[ALERT]" in m]
        self.assertEqual(len(alerts), 1, self.logs)

    def test_plan_transcript_used_when_only_plan_exists(self):
        """Watchdog checks both phases; if only plan.log exists, it picks
        plan's mtime rather than silently no-op'ing."""
        self._seed_working("a", session_id="sess-plan")
        self._write_transcript("a", "plan", age_seconds=400)
        d = self._mk_daemon(threshold=300)
        d._check_stale_sessions(time.time_ns())
        self.assertIn("a", d.stale_session_alerts)


class TestAutoAcceptPlan(unittest.TestCase):
    """After the runner leaves an item at AWAITING_PLAN_REVIEW, the
    daemon invokes the auto-accept predicate. A pass demotes the item
    to QUEUED with an `auto-accepted: <reason>` transition note, so
    the next dispatch tick picks up the execute phase with no operator
    keypress."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        self.store = Store(self.root / ".agentor" / "state.db")

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _queue(self, id: str) -> None:
        self.store.upsert_discovered(_mk_item(id))
        self.store.transition(id, ItemStatus.QUEUED)

    def _plan_behavior(self):
        """Runner that simulates plan-phase completion: transitions the
        claimed item into AWAITING_PLAN_REVIEW and returns a successful
        RunResult. Mirrors what ClaudeRunner.run does at runner.py:570."""
        store = self.store

        def behavior(runner, item):
            store.transition(
                item.id, ItemStatus.AWAITING_PLAN_REVIEW,
                result_json='{"phase":"plan","plan":"sketch"}',
                note="plan ready for human review",
            )
            return RunResult(item.id, Path("/x"), "br", "plan ok", [], "")
        return behavior

    def _factory(self, behavior):
        def make(cfg, store):
            return FakeRunner(cfg, store, behavior)
        return make

    def test_always_mode_demotes_to_queued_with_audit_note(self):
        self._queue("a")
        cfg = _mk_config(self.root, pool_size=1)
        cfg.agent.auto_accept_plan = "always"
        d = Daemon(cfg, self.store, self._factory(self._plan_behavior()),
                   install_signals=False, log=lambda m: None)
        d._dispatch_one()
        _drain_workers(d)

        item = self.store.get("a")
        self.assertEqual(item.status, ItemStatus.QUEUED)
        last = self.store.transitions_for("a")[-1]
        self.assertEqual(last.note, "auto-accepted: always")

    def test_off_mode_leaves_item_at_plan_review(self):
        self._queue("a")
        cfg = _mk_config(self.root, pool_size=1)  # default auto_accept_plan="off"
        d = Daemon(cfg, self.store, self._factory(self._plan_behavior()),
                   install_signals=False, log=lambda m: None)
        d._dispatch_one()
        _drain_workers(d)

        item = self.store.get("a")
        self.assertEqual(item.status, ItemStatus.AWAITING_PLAN_REVIEW)
        last = self.store.transitions_for("a")[-1]
        self.assertNotIn("auto-accepted", last.note or "")

    def test_runner_error_skips_auto_accept(self):
        """A failed run must not trigger the auto-accept path — the item
        isn't at AWAITING_PLAN_REVIEW, so the predicate is never consulted."""
        self._queue("a")

        def behavior(runner, item):
            return RunResult(item.id, Path("/x"), "br", "", [], "",
                             error="agent crashed")

        cfg = _mk_config(self.root, pool_size=1)
        cfg.agent.auto_accept_plan = "always"
        d = Daemon(cfg, self.store, self._factory(behavior),
                   install_signals=False, log=lambda m: None)
        d._dispatch_one()
        _drain_workers(d)

        item = self.store.get("a")
        self.assertNotEqual(item.status, ItemStatus.QUEUED)


if __name__ == "__main__":
    unittest.main()
