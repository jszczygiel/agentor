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
    """Approve the agent's work. Commit any pending changes in the worktree,
    then (optionally) merge the feature branch into `git.base_branch`.

    Behavior depends on `config.git.auto_merge`:
      - False (default): commit on the feature branch, remove the worktree,
        transition MERGED. The feature branch stays; user merges by hand.
      - True: commit, then try `git merge --no-ff` into base via an
        ephemeral detached worktree. Clean merge → remove feature worktree
        and branch, transition MERGED. Conflicts → keep the feature
        worktree and branch intact, transition CONFLICTED with the
        conflict summary stored in `last_error` for the inspect view.

    Returns the feature-branch commit SHA (not the merge commit) either
    way, so callers can always address the agent's work directly."""
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

    if not config.git.auto_merge:
        git_ops.worktree_remove(repo, wt, force=False)
        store.transition(
            item.id, ItemStatus.MERGED,
            note=f"{note_prefix} {sha[:8]} on {item.branch} "
                 f"(no auto-merge; merge into {config.git.base_branch} by hand)",
        )
        return sha

    tmp_root = repo / ".agentor" / "merge-tmp"
    mode = config.git.merge_mode
    merge_sha, conflict = git_ops.merge_feature_into_base(
        repo, item.branch, config.git.base_branch,
        message=f"Merge branch '{item.branch}' into {config.git.base_branch}"
                f"\n\n{message}",
        tmp_root=tmp_root, mode=mode,
    )
    if conflict is not None:
        # Keep the worktree and branch — user resolves by hand, then calls
        # retry_merge (inspect [m]) once the conflict is fixed.
        store.transition(
            item.id, ItemStatus.CONFLICTED,
            last_error=conflict[:4000],
            note=f"{mode} into {config.git.base_branch} conflicted; "
                 f"feature branch {item.branch} kept",
        )
        return sha

    git_ops.worktree_remove(repo, wt, force=False)
    git_ops.branch_delete(repo, item.branch, force=True)
    store.transition(
        item.id, ItemStatus.MERGED,
        note=f"{note_prefix} {sha[:8]}, {mode}d {merge_sha[:8]} into "
             f"{config.git.base_branch}",
    )
    return sha


def retry_merge(
    config: Config, store: Store, item: StoredItem,
) -> tuple[bool, str]:
    """Re-attempt the auto-merge for a CONFLICTED item after the user has
    resolved conflicts in the feature worktree (or otherwise made the
    branch merge-clean). Uses the same `merge_mode` as the original
    approval.

    Returns (ok, message). On success the item transitions MERGED and the
    worktree + feature branch are removed. On continued conflict the item
    stays CONFLICTED with an updated `last_error`.

    Any still-uncommitted resolution edits in the feature worktree are
    folded into a `resolved conflicts` commit first so the merge has a
    clean tip to work with."""
    assert item.status == ItemStatus.CONFLICTED, \
        f"retry_merge expects CONFLICTED, got {item.status}"
    assert item.worktree_path and item.branch

    wt = Path(item.worktree_path)
    repo = config.project_root

    if _has_uncommitted(wt):
        git_ops.commit_all(wt, "resolve merge conflicts")

    tmp_root = repo / ".agentor" / "merge-tmp"
    mode = config.git.merge_mode
    merge_sha, conflict = git_ops.merge_feature_into_base(
        repo, item.branch, config.git.base_branch,
        message=f"Merge branch '{item.branch}' into {config.git.base_branch}"
                f" (retry)",
        tmp_root=tmp_root, mode=mode,
    )
    if conflict is not None:
        store.transition(
            item.id, ItemStatus.CONFLICTED,
            last_error=conflict[:4000],
            note=f"retry {mode} still conflicts on {config.git.base_branch}",
        )
        return False, f"still conflicted: {conflict.splitlines()[0] if conflict else '?'}"

    git_ops.worktree_remove(repo, wt, force=False)
    git_ops.branch_delete(repo, item.branch, force=True)
    store.transition(
        item.id, ItemStatus.MERGED,
        last_error=None,
        note=f"resolved — {mode}d {merge_sha[:8]} into {config.git.base_branch}",
    )
    return True, f"{mode}d {merge_sha[:8]} into {config.git.base_branch}"


def reject(store: Store, item: StoredItem, feedback: str) -> None:
    """Terminal rejection. Keeps worktree+session around for forensics but
    moves the item out of the active flow. Valid at either plan or code
    review stage."""
    assert item.status in (
        ItemStatus.AWAITING_REVIEW, ItemStatus.AWAITING_PLAN_REVIEW,
    )
    store.transition(
        item.id, ItemStatus.REJECTED,
        feedback=feedback, note="rejected by user",
    )


def reject_and_retry(store: Store, item: StoredItem, feedback: str) -> None:
    """Reject the agent's output but re-queue the item so it can iterate on
    the feedback. The runner injects `feedback` into the next prompt.

    - From AWAITING_PLAN_REVIEW → QUEUED with result_json cleared, so the
      runner re-enters the plan phase (same session via --resume).
    - From AWAITING_REVIEW → QUEUED with result_json.phase=plan preserved,
      so the runner re-enters the execute phase.

    Attempts is reset to 0: human-driven iteration shouldn't eat the agent's
    own retry budget. Feedback is stored separately from last_error — it's
    not a failure, just guidance for the next pass."""
    assert item.status in (
        ItemStatus.AWAITING_REVIEW, ItemStatus.AWAITING_PLAN_REVIEW,
    )
    if item.status == ItemStatus.AWAITING_PLAN_REVIEW:
        store.transition(
            item.id, ItemStatus.QUEUED,
            result_json=None,
            feedback=feedback,
            attempts=0,
            note="plan rejected — re-plan with user feedback",
        )
    else:
        store.transition(
            item.id, ItemStatus.QUEUED,
            feedback=feedback,
            attempts=0,
            note="code rejected — re-execute with user feedback",
        )


def approve_backlog(
    store: Store, item: StoredItem, feedback: str | None = None
) -> None:
    """Promote a backlog item to QUEUED so the daemon can dispatch it. Used
    by the pickup UI when pickup_mode is 'manual'. Optional `feedback` is
    persisted and prepended to the agent's first plan prompt (consumed and
    cleared after one use, same path as reject_and_retry)."""
    assert item.status == ItemStatus.BACKLOG
    fields: dict[str, object] = {}
    if feedback:
        fields["feedback"] = feedback
    store.transition(
        item.id, ItemStatus.QUEUED,
        note="approved by user (backlog → queued)"
        + (" with feedback" if feedback else ""),
        **fields,
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


def delete_idea(store: Store, item: StoredItem) -> None:
    """User rejected an idea at pickup — park it in CANCELLED so scan_once
    doesn't re-enqueue it from the source markdown on the next pass. Source
    file is left intact; user can remove the markdown entry whenever."""
    store.transition(
        item.id, ItemStatus.CANCELLED,
        note=f"deleted from pickup (was {item.status.value})",
    )


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
