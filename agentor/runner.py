import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import git_ops
from .config import Config
from .models import ItemStatus
from .slug import slugify
from .store import Store, StoredItem


@dataclass
class RunResult:
    item_id: str
    worktree_path: Path
    branch: str
    summary: str
    files_changed: list[str]
    diff: str
    error: str | None = None


def worktree_root(config: Config) -> Path:
    return config.project_root / ".agentor" / "worktrees"


def plan_worktree(config: Config, item: StoredItem) -> tuple[Path, str]:
    slug = slugify(item.title)
    unique = f"{slug}-{item.id[:8]}"
    branch = f"{config.git.branch_prefix}{unique}"
    path = worktree_root(config) / unique
    return path, branch


class Runner:
    """Base runner interface. Subclasses implement `do_work`."""

    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store

    def do_work(self, item: StoredItem, worktree: Path) -> tuple[str, list[str]]:
        """Perform the agent's work inside the worktree. Return (summary, files_changed).
        Subclasses override. The base class commits no changes — committer does that."""
        raise NotImplementedError

    def run(self, item: StoredItem) -> RunResult:
        """Item must already be in WORKING state with worktree_path and branch set
        (daemon does this via store.claim_next_queued)."""
        assert item.status == ItemStatus.WORKING, f"runner expects WORKING, got {item.status}"
        assert item.worktree_path and item.branch
        wt_path = Path(item.worktree_path)
        branch = item.branch
        repo = self.config.project_root

        if wt_path.exists():
            git_ops.worktree_remove(repo, wt_path, force=True)
            if wt_path.exists():
                shutil.rmtree(wt_path, ignore_errors=True)

        try:
            git_ops.worktree_add(repo, wt_path, branch, self.config.git.base_branch)
        except git_ops.GitError as e:
            self.store.transition(
                item.id, ItemStatus.QUEUED,
                worktree_path=None, branch=None,
                last_error=f"worktree_add: {e}",
            )
            return RunResult(item.id, wt_path, branch, "", [], "", error=str(e))

        try:
            summary, files_changed = self.do_work(item, wt_path)
        except Exception as e:
            last_error = f"do_work: {e}"
            self.store.transition(
                item.id, ItemStatus.REJECTED,
                last_error=last_error,
                note="runner failed",
            )
            git_ops.worktree_remove(repo, wt_path, force=True)
            return RunResult(item.id, wt_path, branch, "", [], "", error=last_error)

        diff = git_ops.diff_vs_base(wt_path, self.config.git.base_branch)
        result = {
            "summary": summary,
            "files_changed": files_changed,
            "diff_len": len(diff),
        }
        self.store.transition(
            item.id, ItemStatus.AWAITING_REVIEW,
            result_json=json.dumps(result),
            note="awaiting user review",
        )
        return RunResult(
            item_id=item.id, worktree_path=wt_path, branch=branch,
            summary=summary, files_changed=files_changed, diff=diff,
        )


class StubRunner(Runner):
    """Test runner that writes a trivial AGENT_NOTE.md. Proves the pipeline
    end-to-end without spawning Claude."""

    def do_work(self, item: StoredItem, worktree: Path) -> tuple[str, list[str]]:
        note_path = worktree / f".agentor-note-{item.id[:8]}.md"
        note_path.write_text(
            f"# Stub agent note\n\n"
            f"Item: {item.title}\n\n"
            f"Body:\n{item.body}\n\n"
            f"(This is a stub runner. Replace with real agent.)\n"
        )
        summary = f"stub: added note for '{item.title}'"
        return summary, [str(note_path.relative_to(worktree))]
