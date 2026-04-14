import signal
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .config import Config
from .models import ItemStatus
from .recovery import recover_on_startup
from .runner import Runner, plan_worktree
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

    def _dispatch_one(self) -> bool:
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
        wt_path, branch = plan_worktree(self.config, nxt)
        claimed = self.store.claim_next_queued(str(wt_path), branch)
        if claimed is None:
            return False
        if claimed.id != nxt.id:
            # another path already claimed it — planned wt_path may be wrong,
            # re-plan against the actual claimed item
            wt_path, branch = plan_worktree(self.config, claimed)
            self.store.transition(
                claimed.id, ItemStatus.WORKING,
                worktree_path=str(wt_path), branch=branch,
                note="re-planned worktree",
            )
            claimed = self.store.get(claimed.id)

        runner = self.runner_factory(self.config, self.store)
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
        if self.install_signals:
            self._install_signal_handlers()
        recovered = recover_on_startup(self.config, self.store)
        if recovered:
            self.log(f"recovered {len(recovered)} items from crashed run")

        while not self.stop_event.is_set():
            result = scan_once(self.config, self.store)
            self.stats.scans += 1
            if result.new_items:
                self.log(f"scan: {result.new_items} new items")
            # dispatch as many as pool allows
            while self._dispatch_one():
                pass
            self.stop_event.wait(self.scan_interval)

        # drain workers
        self.log(f"waiting for {len(self.workers)} worker(s)")
        for t in list(self.workers):
            t.join(timeout=30)
        return self.stats
