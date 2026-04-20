import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import git_ops
from .config import Config
from .models import ItemStatus
from .providers import Provider, make_provider
from .store import Store, StoredItem


# Marker placed on `last_error` when the recovery sweep demotes a stale
# session. Operators should see it for one tick to understand why the
# item restarted; the next sweep clears it via `_AUTO_RECOVERABLE_PATTERNS`.
_STALE_SESSION_MARKER = "session expired; restarting plan"


# Known-benign last_error patterns. An item carrying any of these is safe to
# auto-recover on startup: clear the error, zero attempts, leave status
# alone. Reasons:
#   - "agentor shutdown"  — operator ^C, not the item's fault
#   - "max_cost_usd"      — obsolete runaway cap (removed)
#   - dead-session signatures (claude + codex) — already handled at runtime,
#     safe to clear stale traces regardless of which CLI produced the row
#   - "no agent result yet — no token data" — dashboard placeholder that
#     occasionally gets stored; no diagnostic value
_AUTO_RECOVERABLE_PATTERNS = (
    "agentor shutdown",
    "max_cost_usd",
    "no conversation found with session id",
    "thread not found",
    "thread/start failed",
    "session not found",
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
    # Single-tick marker the recovery sweep itself plants when it demotes
    # a stale session — clears on the next startup so it doesn't linger.
    _STALE_SESSION_MARKER,
)


def _is_auto_recoverable_error(msg: str | None) -> bool:
    if not msg:
        return False
    low = msg.lower()
    return any(p in low for p in _AUTO_RECOVERABLE_PATTERNS)


@dataclass
class RecoveryResult:
    requeued: list[str]   # items reset to QUEUED — must start fresh
    resumable: list[StoredItem]  # WORKING items with agent_ref + live worktree
    auto_recovered: list[str] = field(default_factory=list)  # errors cleared
    # WORKING items whose persisted agent_ref is presumed dead (age over
    # the active provider's `session_max_age_hours`, or a prior failure
    # row matching the provider's dead-session signature). Demoted to
    # QUEUED with agent_ref cleared so the next dispatch starts a fresh
    # plan run instead of paying for a doomed `--resume` round-trip.
    stale_sessions: list[str] = field(default_factory=list)


def _has_dead_session_failure(
    store: Store, item_id: str, provider: Provider,
) -> bool:
    """True when the item's most recent failure row matches the active
    provider's dead-session signature. We only inspect the latest failure
    to avoid re-acting on errors the operator has already moved past. Both
    the raw `error` and the whitespace-stripped `error_sig` fields are
    tested — `ClaudeProvider` / `CodexProvider` each handle both forms."""
    rows = store.list_failures(item_id, limit=1)
    if not rows:
        return False
    last = rows[0]
    err = last.get("error") or ""
    sig = last.get("error_sig") or ""
    return provider.is_dead_session_error(err) or provider.is_dead_session_error(sig)


def _session_age_seconds(store: Store, item: StoredItem, now: float) -> float:
    """Age of the WORKING claim, used as a proxy for session age. Falls
    back to `items.updated_at` when no WORKING transition row exists
    (shouldn't happen for items that hold an agent_ref, but the fallback
    keeps the sweep robust against partial DB state)."""
    at = store.latest_transition_at(item.id, ItemStatus.WORKING)
    if at is None:
        at = item.updated_at
    return max(0.0, now - at)


def recover_on_startup(config: Config, store: Store) -> RecoveryResult:
    """Handle items left in WORKING from a prior run.

    If the item has a persisted agent_ref AND its worktree still exists on
    disk, demote it to QUEUED while preserving `agent_ref`, `worktree_path`,
    `branch`, and `result_json` — the normal dispatch loop will claim it
    when a pool slot opens, and the runner detects the resumable state via
    `agent_ref + worktree exists` and calls claude with `--resume`.
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
    provider = make_provider(config)
    stuck = store.list_by_status(ItemStatus.WORKING)
    requeued: list[str] = []
    resumable: list[StoredItem] = []
    stale_sessions: list[str] = []
    repo = config.project_root
    now = time.time()
    max_age_hours = provider.session_max_age_hours()
    # `None` means the provider has no wall-clock expiry (stub, or any
    # future provider whose sessions don't age out) — leave max_age_seconds
    # at 0 so the age gate is skipped; the per-failure predicate still
    # runs, so a genuine dead-session error row still demotes.
    max_age_seconds = (
        max(0.0, float(max_age_hours)) * 3600.0 if max_age_hours is not None else 0.0
    )
    for item in stuck:
        wt = Path(item.worktree_path) if item.worktree_path else None
        # Stale-session check runs before the resumable check. An item with
        # an agent_ref whose age exceeds the provider's threshold, or whose
        # last failure was a dead-session error per the provider, is demoted
        # to a fresh plan run — `--resume` against an expired session
        # always exits 1 and burns tokens per attempt.
        if item.agent_ref:
            age = _session_age_seconds(store, item, now)
            stale = (max_age_seconds > 0 and age > max_age_seconds)
            stale = stale or _has_dead_session_failure(store, item.id, provider)
            if stale:
                if wt is not None and wt.exists():
                    git_ops.worktree_remove(repo, wt, force=True)
                    if wt.exists():
                        shutil.rmtree(wt, ignore_errors=True)
                store.transition(
                    item.id, ItemStatus.QUEUED,
                    attempts=0,
                    agent_ref=None, worktree_path=None, branch=None,
                    last_error=_STALE_SESSION_MARKER,
                    note="stale session demoted; restarting plan",
                )
                stale_sessions.append(item.id)
                continue
        can_resume = bool(item.agent_ref and wt and wt.exists())
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
            worktree_path=None, branch=None, agent_ref=None,
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
        ItemStatus.QUEUED,
        ItemStatus.AWAITING_PLAN_REVIEW, ItemStatus.AWAITING_REVIEW,
        ItemStatus.DEFERRED, ItemStatus.REJECTED,
    ]
    # Items demoted by the stale-session branch live in QUEUED with the
    # _STALE_SESSION_MARKER on `last_error`. We deliberately leave that
    # marker visible for the current tick so operators can see why the
    # item restarted; the *next* startup sweep treats the marker as
    # benign and clears it.
    just_demoted = set(stale_sessions)
    for st in active_states:
        for item in store.list_by_status(st):
            if item.id in just_demoted:
                continue
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
        stale_sessions=stale_sessions,
    )
