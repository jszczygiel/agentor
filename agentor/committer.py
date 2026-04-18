import subprocess
from pathlib import Path
from typing import Callable

from . import git_ops
from .config import Config
from .models import ItemStatus
from .store import Store, StoredItem

ProgressCb = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


def _has_uncommitted(wt: Path) -> bool:
    cp = subprocess.run(
        ["git", "status", "--porcelain"], cwd=wt,
        capture_output=True, text=True,
    )
    return bool(cp.stdout.strip())


_BODY_CAP = 2000
_RAW_CAP = 1500


def _build_conflict_summary(
    item: StoredItem, mode: str, base: str, raw: str, *, retry: bool = False,
) -> str:
    """Compose the CONFLICTED `last_error` text: feature context first
    (title, branch, item body), then a short trailing block describing the
    merge failure mechanics. Operators want the bulk of this summary to
    describe the feature being integrated, not git's output."""
    body = (item.body or "").strip()
    if len(body) > _BODY_CAP:
        body = body[:_BODY_CAP].rstrip() + "\n…(body truncated)"
    mechanics = (raw or "").strip()
    if len(mechanics) > _RAW_CAP:
        mechanics = mechanics[:_RAW_CAP].rstrip() + "\n…(output truncated)"
    label = f"{mode} into {base}"
    if retry:
        label += ", retry"
    parts = [
        f"Feature: {item.title}",
        f"Branch:  {item.branch or '(unknown)'}",
    ]
    if body:
        parts.extend(["", body])
    parts.extend([
        "",
        f"── merge conflict ({label}) ──",
        mechanics or "(no git output captured)",
    ])
    return "\n".join(parts)


def approve_and_commit(
    config: Config, store: Store, item: StoredItem, message: str,
    *, progress: ProgressCb | None = None,
) -> str:
    """Approve the agent's work. Commit any pending changes in the worktree,
    then merge the feature branch into `git.base_branch` via an ephemeral
    detached worktree. Clean merge → remove feature worktree and branch,
    transition MERGED. Conflicts → keep the feature worktree and branch
    intact, transition CONFLICTED with the conflict summary stored in
    `last_error` for the inspect view.

    Returns the feature-branch commit SHA (not the merge commit), so
    callers can always address the agent's work directly."""
    assert item.status == ItemStatus.AWAITING_REVIEW, \
        f"commit expects AWAITING_REVIEW, got {item.status}"
    assert item.worktree_path and item.branch
    p = progress or _noop

    wt = Path(item.worktree_path)
    repo = config.project_root

    if config.parsing.mode == "frontmatter":
        src_in_wt = wt / item.source_file
        if src_in_wt.exists():
            src_in_wt.unlink()

    if _has_uncommitted(wt):
        p("committing agent work")
        sha = git_ops.commit_all(wt, message)
        note_prefix = "committed"
    else:
        sha = git_ops.run(wt, "rev-parse", "HEAD").stdout.strip()
        note_prefix = "recorded existing commit"

    tmp_root = repo / ".agentor" / "merge-tmp"
    mode = config.git.merge_mode
    verb = "rebasing onto" if mode == "rebase" else "merging into"
    p(f"{verb} {config.git.base_branch}")
    merge_sha, conflict = git_ops.merge_feature_into_base(
        repo, item.branch, config.git.base_branch,
        message=f"Merge branch '{item.branch}' into {config.git.base_branch}"
                f"\n\n{message}",
        tmp_root=tmp_root, mode=mode,
    )
    if conflict is not None:
        # Keep the worktree and branch — user resolves by hand, then calls
        # retry_merge (inspect [m]) once the conflict is fixed.
        summary = _build_conflict_summary(
            item, mode, config.git.base_branch, conflict,
        )
        store.transition(
            item.id, ItemStatus.CONFLICTED,
            last_error=summary[:4000],
            note=f"{mode} into {config.git.base_branch} conflicted; "
                 f"feature branch {item.branch} kept",
        )
        if config.git.auto_resolve_conflicts:
            p("auto-resubmitting for agent conflict resolution")
            refreshed = store.get(item.id)
            if refreshed is not None and \
                    refreshed.status == ItemStatus.CONFLICTED:
                resubmit_conflicted(config, store, refreshed)
        return sha

    assert merge_sha is not None
    p("cleaning up worktree and branch")
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
    *, progress: ProgressCb | None = None,
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
    p = progress or _noop

    wt = Path(item.worktree_path)
    repo = config.project_root

    if _has_uncommitted(wt):
        p("committing conflict resolution")
        git_ops.commit_all(wt, "resolve merge conflicts")

    tmp_root = repo / ".agentor" / "merge-tmp"
    mode = config.git.merge_mode
    verb = "rebasing onto" if mode == "rebase" else "merging into"
    p(f"{verb} {config.git.base_branch} (retry)")
    merge_sha, conflict = git_ops.merge_feature_into_base(
        repo, item.branch, config.git.base_branch,
        message=f"Merge branch '{item.branch}' into {config.git.base_branch}"
                f" (retry)",
        tmp_root=tmp_root, mode=mode,
    )
    if conflict is not None:
        summary = _build_conflict_summary(
            item, mode, config.git.base_branch, conflict, retry=True,
        )
        store.transition(
            item.id, ItemStatus.CONFLICTED,
            last_error=summary[:4000],
            note=f"retry {mode} still conflicts on {config.git.base_branch}",
        )
        return False, f"still conflicted: {conflict.splitlines()[0] if conflict else '?'}"

    assert merge_sha is not None
    p("cleaning up worktree and branch")
    git_ops.worktree_remove(repo, wt, force=False)
    git_ops.branch_delete(repo, item.branch, force=True)
    store.transition(
        item.id, ItemStatus.MERGED,
        last_error=None,
        note=f"resolved — {mode}d {merge_sha[:8]} into {config.git.base_branch}",
    )
    return True, f"{mode}d {merge_sha[:8]} into {config.git.base_branch}"


def resubmit_conflicted(
    config: Config, store: Store, item: StoredItem,
) -> None:
    """Send a CONFLICTED item back to the agent to resolve the merge
    conflict itself. Transitions CONFLICTED → QUEUED; the worktree,
    feature branch, and session_id are left intact so the runner resumes
    the same session in execute phase and the injected feedback tells
    the agent what to fix.

    The agent's instructions: run `git merge <base>` in its own worktree
    to surface the conflicts, resolve them, and commit. On next approval
    the integration retries — if the feature now includes base's tip,
    the merge fast-forwards."""
    assert item.status == ItemStatus.CONFLICTED, \
        f"resubmit_conflicted expects CONFLICTED, got {item.status}"
    assert item.worktree_path and item.branch
    base = config.git.base_branch
    conflict_detail = (item.last_error or "(no conflict summary recorded)")
    feedback = (
        f"Your branch's changes conflict with `{base}`. Resolve the "
        f"conflict so the integration can proceed.\n\n"
        f"Steps to run in this worktree:\n"
        f"  1. `git merge {base}` — pulls base's commits and surfaces "
        f"the conflicts as markers in the listed files.\n"
        f"  2. Fix each conflicted file. Preserve the intent of your "
        f"original changes AND the base-branch changes where possible; "
        f"pick base's version only when it clearly supersedes.\n"
        f"  3. `git add` the resolved files and `git commit` the merge "
        f"(a merge commit is fine — do NOT rebase).\n"
        f"  4. Re-run build + tests to confirm the merged result still "
        f"works.\n\n"
        f"Conflict summary from the failed integration:\n"
        f"{conflict_detail}"
    )
    store.transition(
        item.id, ItemStatus.QUEUED,
        feedback=feedback,
        last_error=None,
        attempts=0,
        note="resubmitted from CONFLICTED — agent will resolve",
    )


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
    """Re-queue a rejected or errored item for another attempt. Keeps the
    existing worktree and session_id so the runner can --resume if still live.
    Clears last_error and resets the attempt counter — human-driven retry
    shouldn't consume the agent's own retry budget."""
    assert item.status in (ItemStatus.REJECTED, ItemStatus.ERRORED)
    store.transition(
        item.id, ItemStatus.QUEUED,
        last_error=None, attempts=0,
        note=f"retry after {item.status.value}",
    )


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
        if t.from_status and t.from_status != ItemStatus.DEFERRED:
            prior = t.from_status
            break
    store.transition(item.id, prior, note=f"restored from deferred -> {prior.value}")
    return prior
