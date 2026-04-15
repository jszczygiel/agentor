import shutil
from dataclasses import dataclass
from pathlib import Path

from . import git_ops
from .config import Config
from .models import ItemStatus
from .store import Store, StoredItem


@dataclass
class RecoveryResult:
    requeued: list[str]   # items reset to QUEUED — must start fresh
    resumable: list[StoredItem]  # WORKING items with session_id + live worktree


def recover_on_startup(config: Config, store: Store) -> RecoveryResult:
    """Handle items left in WORKING from a prior run.

    If the item has a persisted session_id AND its worktree still exists on
    disk, leave it WORKING and return it as resumable — the caller re-invokes
    claude with `--resume <session_id>`. Otherwise nuke its worktree and
    requeue it for a fresh attempt."""
    stuck = store.list_by_status(ItemStatus.WORKING)
    requeued: list[str] = []
    resumable: list[StoredItem] = []
    repo = config.project_root
    for item in stuck:
        wt = Path(item.worktree_path) if item.worktree_path else None
        can_resume = bool(item.session_id and wt and wt.exists())
        if can_resume:
            resumable.append(item)
            continue
        if wt is not None:
            git_ops.worktree_remove(repo, wt, force=True)
            if wt.exists():
                shutil.rmtree(wt, ignore_errors=True)
        store.transition(
            item.id, ItemStatus.QUEUED,
            worktree_path=None, branch=None,
            note="recovered from crashed run (no session to resume)",
        )
        requeued.append(item.id)
    return RecoveryResult(requeued=requeued, resumable=resumable)
