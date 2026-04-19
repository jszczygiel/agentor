import signal
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .config import Config
from .fold import maybe_enqueue_fold_item
from .models import ItemStatus
from .recovery import recover_on_startup
from .runner import InfrastructureError, ProcRegistry, Runner, plan_worktree
from .store import Store
from .watcher import scan_once


@dataclass
class DaemonStats:
    scans: int = 0
    dispatched: int = 0
    completed: int = 0
    failed: int = 0


class Daemon:
    """Long-running loop: scan sources on interval, claim queued items up to
    pool_size, hand each to a runner running on a worker thread."""

    def __init__(
        self,
        config: Config,
        store: Store,
        runner_factory: Callable[[Config, Store], Runner],
        scan_interval: float = 5.0,
        log: Callable[[str], None] = print,
        install_signals: bool = True,
    ):
        self.config = config
        self.store = store
        self.runner_factory = runner_factory
        self.scan_interval = scan_interval
        self.log = log
        self.install_signals = install_signals
        self.stop_event = threading.Event()
        self.force_stop = False
        self.workers: set[threading.Thread] = set()
        self.stats = DaemonStats()
        self.proc_registry = ProcRegistry()
        # Set when a runner raises InfrastructureError. While set:
        #  - dispatch_one and dispatch_specific refuse to claim new items
        #    (the slot is broken; claiming would just hit the same error
        #    and waste another attempt, even though we already refund it)
        #  - dashboard renders a sticky red banner with the message
        # Cleared by dashboard 'u' key (or programmatically via
        # clear_alert) once the user has fixed the underlying problem.
        self.system_alert: str | None = None
        self.paused: bool = False
        self._heartbeat_last: float = 0.0
        self._heartbeat_dispatched: int = 0
        # Epoch set when run() starts; used as the "since-daemon-start" cutoff
        # for the dashboard token-usage panel. Zero means the loop has not
        # been entered yet (tests constructing a bare Daemon).
        self.started_at: float = 0.0

    #: Wall-clock seconds of idle before a heartbeat log line fires. Class-
    #: level so tests can shrink it without patching time.
    heartbeat_interval: float = 30.0

    def _maybe_log_heartbeat(self, new_items: int) -> None:
        """If the daemon is idle (no new items, no new dispatches, no live
        workers) and the last heartbeat is older than `heartbeat_interval`,
        emit one log line. Called once per main-loop iteration."""
        idle = (
            new_items == 0
            and self.stats.dispatched == self._heartbeat_dispatched
            and not self.workers
        )
        now = time.monotonic()
        if idle:
            if now - self._heartbeat_last >= self.heartbeat_interval:
                self.log(f"heartbeat: idle ({self.stats.scans} scans)")
                self._heartbeat_last = now
        else:
            self._heartbeat_last = now
            self._heartbeat_dispatched = self.stats.dispatched

    def clear_alert(self) -> None:
        """Acknowledge the alert and resume dispatching. Called from the
        dashboard 'u' key after the user has fixed the broken slot/repo."""
        self.system_alert = None
        self.paused = False
        self.log("alert cleared; dispatch resumed")

    def _make_runner(self) -> Runner:
        r = self.runner_factory(self.config, self.store)
        r.proc_registry = self.proc_registry
        r.stop_event = self.stop_event
        return r

    def dispatch_specific(self, item_id: str) -> bool:
        """Manually approve a queued item for pickup. Returns True if it was
        dispatched, False if it could not be (already claimed, no slot, gone)."""
        if self.paused:
            self.log("manual dispatch denied: paused (system alert active)")
            return False
        if not self.store.pool_has_slot(self.config.agent.pool_size):
            self.log("manual dispatch denied: pool full")
            return False
        item = self.store.get(item_id)
        if item is None or item.status != ItemStatus.QUEUED:
            self.log(f"manual dispatch denied: {item_id} not queued")
            return False
        wt_path, branch = plan_worktree(self.config, store=self.store, item=item)
        # claim_next_queued returns oldest; we want THIS item. Transition by hand.
        self.store.transition(
            item.id, ItemStatus.WORKING,
            worktree_path=str(wt_path), branch=branch,
            attempts=item.attempts + 1,
            note="manual pickup approval",
        )
        claimed = self.store.get(item.id)
        assert claimed is not None
        runner = self._make_runner()
        self.stats.dispatched += 1
        self.log(f"manual dispatch: {claimed.id} {claimed.title!r} -> {branch}")
        t = threading.Thread(
            target=self._run_worker, args=(runner, claimed), daemon=True,
        )
        self.workers.add(t)
        t.start()
        return True

    def try_fill_pool(self) -> int:
        """Attempt to dispatch as many queued items as the current pool allows,
        right now — bypasses the scan-interval wait. Returns how many were
        dispatched."""
        n = 0
        stagger = self.config.agent.dispatch_stagger_seconds
        while self._dispatch_one():
            n += 1
            if stagger <= 0:
                continue
            # Give the first agent a head start writing the shared system-
            # prompt prefix into Anthropic's cache before siblings race for
            # it. Skip when another dispatch isn't plausibly about to fire
            # (pool just filled) so a solo dispatch never waits.
            if not self.store.pool_has_slot(self.config.agent.pool_size):
                break
            self._stagger_wait(stagger)
        return n

    def _stagger_wait(self, seconds: float) -> None:
        """Interruptible sleep between staggered dispatches. Uses stop_event
        so a shutdown signal unblocks the wait. Overridable for tests."""
        self.stop_event.wait(seconds)

    def _dispatch_one(self) -> bool:
        """Dispatch one QUEUED item if a pool slot is free. New items land
        at QUEUED on discovery (see Store.upsert_discovered); the daemon
        claims the oldest unattempted one when a pool slot frees up."""
        if self.paused:
            return False
        if not self.store.pool_has_slot(self.config.agent.pool_size):
            return False
        # peek to plan worktree path before claiming (need title for slug)
        queued = self.store.list_by_status(ItemStatus.QUEUED)
        max_attempts = self.config.agent.max_attempts
        nxt = next((q for q in queued if q.attempts < max_attempts), None)
        if nxt is None:
            if queued:
                # all remaining have exhausted attempts — auto-reject them
                for q in queued:
                    self.store.transition(
                        q.id, ItemStatus.REJECTED,
                        last_error=q.last_error or "max_attempts reached before dispatch",
                        note="auto-reject: exhausted",
                    )
            return False
        wt_path, branch = plan_worktree(self.config, store=self.store, item=nxt)
        claimed = self.store.claim_next_queued(str(wt_path), branch)
        if claimed is None:
            return False
        if claimed.id != nxt.id:
            # another path already claimed it — planned wt_path may be wrong,
            # re-plan against the actual claimed item
            wt_path, branch = plan_worktree(self.config, store=self.store, item=claimed)
            self.store.transition(
                claimed.id, ItemStatus.WORKING,
                worktree_path=str(wt_path), branch=branch,
                note="re-planned worktree",
            )
            claimed = self.store.get(claimed.id)
            assert claimed is not None

        runner = self._make_runner()
        self.stats.dispatched += 1
        self.log(f"dispatch: {claimed.id} {claimed.title!r} -> {branch}")
        t = threading.Thread(
            target=self._run_worker, args=(runner, claimed), daemon=True,
        )
        self.workers.add(t)
        t.start()
        return True

    def _run_worker(self, runner: Runner, claimed) -> None:
        try:
            result = runner.run(claimed)
            if result.error:
                self.stats.failed += 1
                self.log(f"failed: {claimed.id}: {result.error}")
            else:
                self.stats.completed += 1
                self.log(f"awaiting_review: {claimed.id} — {result.summary}")
        except InfrastructureError as e:
            # Don't count as a failure (the item didn't fail; the system
            # did). Item was left in WORKING by note_infra_failure with
            # the error recorded; runner refunded its attempt.
            msg = (f"infrastructure error on {claimed.id} "
                   f"{claimed.title!r}: {e}")
            self.system_alert = str(e)
            self.paused = True
            self.log(f"[ALERT] {msg}")
            self.log("[ALERT] dispatch paused — fix issue, then press 'u' "
                     "in dashboard to resume")
        except Exception as e:
            self.stats.failed += 1
            self.log(f"worker crashed: {claimed.id}: {e}")
        finally:
            self.workers.discard(threading.current_thread())

    def _install_signal_handlers(self) -> None:
        def handler(signum, frame):
            if self.force_stop:
                self.log("force stop; exiting immediately")
                raise SystemExit(130)
            self.log("shutdown requested; draining in-flight work (ctrl-c again to force)")
            self.force_stop = True
            self.stop_event.set()
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def run(self) -> DaemonStats:
        self.started_at = time.time()
        if self.install_signals:
            self._install_signal_handlers()
        rec = recover_on_startup(self.config, self.store)
        if rec.requeued:
            self.log(f"requeued {len(rec.requeued)} items from crashed run "
                     f"(no resumable session)")
        if rec.auto_recovered:
            self.log(f"auto-recovered {len(rec.auto_recovered)} items "
                     f"with benign last_error (shutdown/cap/stale session)")
        if rec.resumable:
            # Resumable items are now demoted to QUEUED by recovery; the
            # normal dispatch loop claims them when a pool slot opens, and
            # the runner picks up the stored session via --resume.
            self.log(f"queued {len(rec.resumable)} resumable item(s) "
                     f"for dispatch")

        # Heartbeat bookkeeping: log a one-liner once the daemon has been
        # idle (no new items, no dispatches, no workers) for ~30s of wall
        # time, so operator reports of "the app seems hung" can be
        # confirmed against a log that demonstrates the main loop is
        # alive. Not a progress signal — only fires when nothing else is.
        self._heartbeat_last = 0.0
        self._heartbeat_dispatched = self.stats.dispatched

        while not self.stop_event.is_set():
            # scan_once lands new items directly at QUEUED; dispatch claims
            # them as pool slots free up.
            result = scan_once(self.config, self.store)
            self.stats.scans += 1
            if result.new_items:
                self.log(f"scan: {result.new_items} new items")
            self.try_fill_pool()
            try:
                created = maybe_enqueue_fold_item(self.config, self.store)
            except Exception as e:
                self.log(f"fold-queue error: {e}")
            else:
                if created is not None:
                    self.log(f"queued agent-log fold item: {created}")
            self._maybe_log_heartbeat(result.new_items)
            self.stop_event.wait(self.scan_interval)

        # Kill in-flight agent subprocesses before draining worker threads.
        # Worker threads are blocked reading subprocess stdout — without the
        # kill, join() below would time out and the children would be
        # orphaned at interpreter shutdown.
        killed = self.proc_registry.kill_all(log=self.log)
        if killed:
            self.log(f"killed {killed} in-flight agent(s)")
        self.log(f"waiting for {len(self.workers)} worker(s)")
        for t in list(self.workers):
            t.join(timeout=30)
        return self.stats
