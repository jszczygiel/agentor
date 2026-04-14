import shutil
from pathlib import Path

from . import git_ops
from .config import Config
from .models import ItemStatus
from .store import Store


def recover_on_startup(config: Config, store: Store) -> list[str]:
    """Handle items left in WORKING from a prior crashed run. Resets them to
    QUEUED and cleans up their worktrees. Returns item IDs that were recovered."""
    stuck = store.list_by_status(ItemStatus.WORKING)
    recovered: list[str] = []
    repo = config.project_root
    for item in stuck:
        if item.worktree_path:
            wt = Path(item.worktree_path)
            git_ops.worktree_remove(repo, wt, force=True)
            if wt.exists():
                shutil.rmtree(wt, ignore_errors=True)
        store.transition(
            item.id, ItemStatus.QUEUED,
            worktree_path=None, branch=None,
            note="recovered from crashed run",
        )
        recovered.append(item.id)
    return recovered
