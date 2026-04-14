from pathlib import Path

from . import git_ops
from .config import Config
from .models import ItemStatus
from .store import Store, StoredItem


def approve_and_commit(
    config: Config, store: Store, item: StoredItem, message: str
) -> str:
    """Commit pending changes in the item's worktree, transition to MERGED,
    remove the worktree, and optionally delete the source file.
    Returns the commit SHA."""
    assert item.status == ItemStatus.AWAITING_REVIEW, \
        f"commit expects AWAITING_REVIEW, got {item.status}"
    assert item.worktree_path

    wt = Path(item.worktree_path)
    repo = config.project_root

    if config.sources.mark_done and config.parsing.mode == "frontmatter":
        src_in_wt = wt / item.source_file
        if src_in_wt.exists():
            src_in_wt.unlink()

    sha = git_ops.commit_all(wt, message)

    git_ops.worktree_remove(repo, wt, force=False)

    store.transition(
        item.id, ItemStatus.MERGED,
        note=f"committed {sha[:8]} on {item.branch}",
    )
    return sha


def reject(store: Store, item: StoredItem, feedback: str) -> None:
    """Reject the agent's work. Keep the worktree around so the agent can retry
    on top of the existing branch with the user's feedback."""
    assert item.status == ItemStatus.AWAITING_REVIEW
    store.transition(
        item.id, ItemStatus.REJECTED,
        last_error=feedback, note="rejected by user",
    )


def retry(store: Store, item: StoredItem) -> None:
    """Re-queue a rejected item for another attempt. Keeps the existing worktree."""
    assert item.status == ItemStatus.REJECTED
    store.transition(item.id, ItemStatus.QUEUED, note="retry after rejection")
