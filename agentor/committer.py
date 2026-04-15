import subprocess
from pathlib import Path

from . import git_ops
from .config import Config
from .models import ItemStatus
from .store import Store, StoredItem


def _has_uncommitted(wt: Path) -> bool:
    cp = subprocess.run(
        ["git", "status", "--porcelain"], cwd=wt,
        capture_output=True, text=True,
    )
    return bool(cp.stdout.strip())


def approve_and_commit(
    config: Config, store: Store, item: StoredItem, message: str
) -> str:
    """Approve the agent's work. If there are uncommitted changes in the
    worktree, commit them with `message`. If the agent already committed
    (e.g. via /develop), just record the existing HEAD. Then remove the
    worktree and transition to MERGED. Returns the commit SHA."""
    assert item.status == ItemStatus.AWAITING_REVIEW, \
        f"commit expects AWAITING_REVIEW, got {item.status}"
    assert item.worktree_path

    wt = Path(item.worktree_path)
    repo = config.project_root

    if config.sources.mark_done and config.parsing.mode == "frontmatter":
        src_in_wt = wt / item.source_file
        if src_in_wt.exists():
            src_in_wt.unlink()

    if _has_uncommitted(wt):
        sha = git_ops.commit_all(wt, message)
        note_prefix = "committed"
    else:
        sha = git_ops.run(wt, "rev-parse", "HEAD").stdout.strip()
        note_prefix = "recorded existing commit"

    git_ops.worktree_remove(repo, wt, force=False)

    store.transition(
        item.id, ItemStatus.MERGED,
        note=f"{note_prefix} {sha[:8]} on {item.branch}",
    )
    return sha


def reject(store: Store, item: StoredItem, feedback: str) -> None:
    """Terminal rejection. Keeps worktree+session around for forensics but
    moves the item out of the active flow. Valid at either plan or code
    review stage."""
    assert item.status in (
        ItemStatus.AWAITING_REVIEW, ItemStatus.AWAITING_PLAN_REVIEW,
    )
    store.transition(
        item.id, ItemStatus.REJECTED,
        last_error=feedback, note="rejected by user",
    )


def reject_and_retry(store: Store, item: StoredItem, feedback: str) -> None:
    """Reject the agent's output but re-queue the item so it can iterate on
    the feedback. The runner injects `last_error` into the next prompt.

    - From AWAITING_PLAN_REVIEW → QUEUED with result_json cleared, so the
      runner re-enters the plan phase (same session via --resume).
    - From AWAITING_REVIEW → QUEUED with result_json.phase=plan preserved,
      so the runner re-enters the execute phase.

    Attempts is reset to 0: human-driven iteration shouldn't eat the agent's
    own retry budget."""
    assert item.status in (
        ItemStatus.AWAITING_REVIEW, ItemStatus.AWAITING_PLAN_REVIEW,
    )
    if item.status == ItemStatus.AWAITING_PLAN_REVIEW:
        store.transition(
            item.id, ItemStatus.QUEUED,
            result_json=None,
            last_error=feedback,
            attempts=0,
            note="plan rejected — re-plan with user feedback",
        )
    else:
        store.transition(
            item.id, ItemStatus.QUEUED,
            last_error=feedback,
            attempts=0,
            note="code rejected — re-execute with user feedback",
        )


def approve_backlog(store: Store, item: StoredItem) -> None:
    """Promote a backlog item to QUEUED so the daemon can dispatch it. Used
    by the pickup UI when pickup_mode is 'manual'."""
    assert item.status == ItemStatus.BACKLOG
    store.transition(
        item.id, ItemStatus.QUEUED,
        note="approved by user (backlog → queued)",
    )


def approve_plan(store: Store, item: StoredItem) -> None:
    """User approved the agent's development plan. Push the item back to QUEUED
    so the daemon re-claims it; the runner sees the persisted plan in
    result_json and runs the execute phase (resumes the same claude session)."""
    assert item.status == ItemStatus.AWAITING_PLAN_REVIEW
    store.transition(
        item.id, ItemStatus.QUEUED,
        note="plan approved — execute phase queued",
    )


def retry(store: Store, item: StoredItem) -> None:
    """Re-queue a rejected item for another attempt. Keeps the existing worktree."""
    assert item.status == ItemStatus.REJECTED
    store.transition(item.id, ItemStatus.QUEUED, note="retry after rejection")


def defer(store: Store, item: StoredItem) -> None:
    """Set an item aside without acting on it. Used by skip in the pickup
    and review pickers. Restorable via `restore_deferred`."""
    store.transition(
        item.id, ItemStatus.DEFERRED,
        note=f"deferred from {item.status.value}",
    )


def restore_deferred(store: Store, item: StoredItem) -> ItemStatus:
    """Bring a deferred item back to its previous status (the last non-deferred
    status in its history). Returns the restored status."""
    assert item.status == ItemStatus.DEFERRED
    history = store.transitions_for(item.id)
    prior = ItemStatus.QUEUED  # default if history somehow has nothing
    for t in reversed(history):
        if t["from_status"] and t["from_status"] != ItemStatus.DEFERRED.value:
            prior = ItemStatus(t["from_status"])
            break
    store.transition(item.id, prior, note=f"restored from deferred -> {prior.value}")
    return prior
