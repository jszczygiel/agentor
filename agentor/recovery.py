import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import git_ops
from .config import Config
from .models import ItemStatus
from .store import Store, StoredItem


# Known-benign last_error patterns. An item carrying any of these is safe to
# auto-recover on startup: clear the error, zero attempts, leave status
# alone. Reasons:
#   - "agentor shutdown"  — operator ^C, not the item's fault
#   - "max_cost_usd"      — obsolete runaway cap (removed)
#   - "no conversation found with session id" — already handled at runtime,
#     safe to clear stale traces
#   - "no agent result yet — no token data" — dashboard placeholder that
#     occasionally gets stored; no diagnostic value
_AUTO_RECOVERABLE_PATTERNS = (
    "agentor shutdown",
    "max_cost_usd",
    "no conversation found with session id",
    "no agent result yet",
    "no token data",
    # Infrastructure-class: the slot was broken at dispatch time. We
    # already refund attempts at runtime via note_infra_failure, but a
    # stale last_error can persist on items queued under older code.
    # The self-heal + branch cleanup in runner makes these recoverable
    # on the next dispatch; no reason to leave the marker on.
    "not a git repository",
    "not a working tree",
    "fatal: invalid reference",
    "fatal: bad object",
    "fatal: bad revision",
    "already exists",
    "is already checked out",
    "already used by worktree",
)


def _is_auto_recoverable_error(msg: str | None) -> bool:
    if not msg:
        return False
    low = msg.lower()
    return any(p in low for p in _AUTO_RECOVERABLE_PATTERNS)


@dataclass
class RecoveryResult:
    requeued: list[str]   # items reset to QUEUED — must start fresh
    resumable: list[StoredItem]  # WORKING items with session_id + live worktree
    auto_recovered: list[str] = field(default_factory=list)  # errors cleared


def recover_on_startup(config: Config, store: Store) -> RecoveryResult:
    """Handle items left in WORKING from a prior run.

    If the item has a persisted session_id AND its worktree still exists on
    disk, demote it to QUEUED while preserving `session_id`, `worktree_path`,
    `branch`, and `result_json` — the normal dispatch loop will claim it
    when a pool slot opens, and the runner detects the resumable state via
    `session_id + worktree exists` and calls claude with `--resume`.
    Resetting attempts to 0 keeps the operator-driven resume from eating
    the item's retry budget.

    If the item cannot be resumed (no session, worktree gone), nuke its
    worktree and revert to its previous settled state — typically QUEUED
    for a fresh item, but AWAITING_PLAN_REVIEW or AWAITING_REVIEW for
    items that had reached a user-checkpoint before the crash. Without
    this revert, user-visible progress would be silently lost on every
    restart.

    Returns the list of demoted resumable items for logging — the daemon
    no longer needs a separate startup dispatch loop; `_dispatch_one`
    handles everything uniformly."""
    stuck = store.list_by_status(ItemStatus.WORKING)
    requeued: list[str] = []
    resumable: list[StoredItem] = []
    repo = config.project_root
    for item in stuck:
        wt = Path(item.worktree_path) if item.worktree_path else None
        can_resume = bool(item.session_id and wt and wt.exists())
        if can_resume:
            store.transition(
                item.id, ItemStatus.QUEUED,
                attempts=0,
                note="resumable session demoted to QUEUED for dispatch",
            )
            refreshed = store.get(item.id)
            assert refreshed is not None
            resumable.append(refreshed)
            continue
        if wt is not None:
            git_ops.worktree_remove(repo, wt, force=True)
            if wt.exists():
                shutil.rmtree(wt, ignore_errors=True)
        # Find the last safe state. Falls back to QUEUED when the item has
        # no settled history (brand-new items that crashed mid-first-run).
        prev = store.previous_settled_status(item.id) or ItemStatus.QUEUED
        store.transition(
            item.id, prev,
            worktree_path=None, branch=None, session_id=None,
            note=f"recovered from crashed run → {prev.value} (no resumable session)",
        )
        requeued.append(item.id)

    # Sweep two cases:
    #  1. Non-terminal items whose last_error matches a known benign
    #     class. Clear last_error + reset attempts; status unchanged so
    #     QUEUED items re-enter dispatch, DEFERRED/REJECTED ones just
    #     lose the `!` marker.
    #  2. Terminal items (MERGED, CANCELLED) carrying any stale
    #     last_error from a pre-merge bounce. The work is done; the
    #     error is noise regardless of class.
    auto_recovered: list[str] = []
    active_states = [
        ItemStatus.QUEUED, ItemStatus.BACKLOG,
        ItemStatus.AWAITING_PLAN_REVIEW, ItemStatus.AWAITING_REVIEW,
        ItemStatus.DEFERRED, ItemStatus.REJECTED,
    ]
    for st in active_states:
        for item in store.list_by_status(st):
            if _is_auto_recoverable_error(item.last_error):
                store.clear_error_and_reset_attempts(item.id)
                auto_recovered.append(item.id)
    for st in (ItemStatus.MERGED, ItemStatus.CANCELLED):
        for item in store.list_by_status(st):
            if item.last_error:
                store.clear_error_and_reset_attempts(item.id)
                auto_recovered.append(item.id)
    return RecoveryResult(
        requeued=requeued, resumable=resumable,
        auto_recovered=auto_recovered,
    )
