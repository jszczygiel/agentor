import json
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from . import git_ops
from .config import Config
from .models import ItemStatus
from .store import Store, StoredItem

if TYPE_CHECKING:  # pragma: no cover — import cycle guard only
    from .daemon import Daemon

ProgressCb = Callable[[str], None]

# Process-wide serialisation of base-branch updates. `approve_and_commit`
# and `retry_merge` both spawn a detached ephemeral worktree off the
# current tip of `git.base_branch` and CAS-advance the ref. Two integrations
# running concurrently would race: the second one's `update-ref OLD NEW`
# trips on a stale OLD and transitions CONFLICTED with a spurious
# "ref changed under us" error for work that wasn't actually conflicting.
# Holding this lock over only the integration block (not the per-feature
# commit / rebase-in-place) keeps the serialisation window tight.
_INTEGRATION_LOCK = threading.Lock()


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

# Marker prefix on the CONFLICTED → QUEUED transition note when the
# auto-resolve chain fires from `approve_and_commit`. The dashboard reads
# this to distinguish an auto-chained resubmit from a manual `[e]` press.
AUTO_RESOLVE_NOTE_PREFIX = "auto-resolve"
_AUTO_RESOLVE_NOTE = (
    f"{AUTO_RESOLVE_NOTE_PREFIX}: resubmitted from CONFLICTED — agent will resolve"
)


def _plan_checkout_advance(
    config: Config, repo: Path, base_sha_before: str, p: ProgressCb,
) -> tuple[bool, str]:
    """Decide whether the user's primary checkout should fast-forward
    after a clean merge, and return the MERGED-note suffix capturing the
    outcome. Called BEFORE `merge_feature_into_base` runs its CAS so the
    guards read a meaningful pre-CAS state.

    Returns `(will_advance, suffix)`:
      - gate off             → (False, "")                    — silent
      - gate on, allowed     → (True,  ", checkout advanced")
      - gate on, skip reason → (False, ", checkout skipped: <reason>")

    Also emits a progress message so the curses progress dialog surfaces
    the decision live during the merge."""
    if not config.git.advance_user_checkout:
        return False, ""
    allowed, reason = git_ops.advance_user_checkout_allowed(
        repo, config.git.base_branch, base_sha_before,
    )
    if allowed:
        p(f"user checkout will advance to new {config.git.base_branch} tip")
        return True, ", checkout advanced"
    assert reason is not None  # allowed=False always carries a reason
    p(f"checkout will not advance — {reason}")
    return False, f", checkout skipped: {reason}"


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
    with _INTEGRATION_LOCK:
        # Capture pre-CAS state under the lock: the base sha the CAS will
        # use as OLD, plus whether the user's checkout is safely advanceable.
        # Once merge_feature_into_base runs, refs/heads/<base> has moved and
        # HEAD symbolically follows it — the clean-tree and HEAD-equals-
        # base_sha_before guards both become unreliable post-CAS.
        base_sha_before = git_ops.run(
            repo, "rev-parse", f"refs/heads/{config.git.base_branch}",
        ).stdout.strip()
        will_advance_checkout, checkout_suffix = _plan_checkout_advance(
            config, repo, base_sha_before, p,
        )
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
                    resubmit_conflicted(
                        config, store, refreshed,
                        force_execute=True, note=_AUTO_RESOLVE_NOTE,
                    )
            return sha

        assert merge_sha is not None
        p("cleaning up worktree and branch")
        git_ops.worktree_remove(repo, wt, force=False)
        git_ops.branch_delete(repo, item.branch, force=True)
        if will_advance_checkout:
            git_ops.advance_user_checkout(repo, merge_sha)
        store.transition(
            item.id, ItemStatus.MERGED,
            note=f"{note_prefix} {sha[:8]}, {mode}d {merge_sha[:8]} into "
                 f"{config.git.base_branch}{checkout_suffix}",
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
    with _INTEGRATION_LOCK:
        base_sha_before = git_ops.run(
            repo, "rev-parse", f"refs/heads/{config.git.base_branch}",
        ).stdout.strip()
        will_advance_checkout, checkout_suffix = _plan_checkout_advance(
            config, repo, base_sha_before, p,
        )
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
        if will_advance_checkout:
            git_ops.advance_user_checkout(repo, merge_sha)
        store.transition(
            item.id, ItemStatus.MERGED,
            last_error=None,
            note=f"resolved — {mode}d {merge_sha[:8]} into "
                 f"{config.git.base_branch}{checkout_suffix}",
        )
        return True, f"{mode}d {merge_sha[:8]} into {config.git.base_branch}"


_FORCE_EXECUTE_PLAN_FALLBACK = "(no plan; conflict resolution — see feedback)"


def _coerce_phase_plan(blob: str | None) -> str:
    """Rewrite a stored `result_json` so the runner's two-phase dispatch
    routes the next run into execute-only. Preserves all prior keys (usage,
    session_id, summary, etc.) and guarantees a non-empty `plan` string so
    the execute prompt template's `{plan}` placeholder substitutes cleanly."""
    try:
        data = json.loads(blob) if blob else {}
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["phase"] = "plan"
    if not (isinstance(data.get("plan"), str) and data["plan"].strip()):
        data["plan"] = _FORCE_EXECUTE_PLAN_FALLBACK
    return json.dumps(data)


def resubmit_conflicted(
    config: Config, store: Store, item: StoredItem,
    *, force_execute: bool = False, note: str | None = None,
) -> None:
    """Send a CONFLICTED item back to the agent to resolve the merge
    conflict itself. Transitions CONFLICTED → QUEUED; the worktree,
    feature branch, and session_id are left intact so the runner resumes
    the same session in execute phase and the injected feedback tells
    the agent what to fix.

    The agent's instructions: run `git merge <base>` in its own worktree
    to surface the conflicts, resolve them, and commit. On next approval
    the integration retries — if the feature now includes base's tip,
    the merge fast-forwards.

    `force_execute=True` rewrites `result_json` so the runner skips its
    plan phase and dispatches straight into execute — conflict resolution
    is pure execute work (open worktree, resolve markers, re-run tests,
    commit), and the plan turn is wasted tokens + wall-clock. Used by
    `approve_and_commit`'s auto-resolve chain; manual `[e]resubmit` from
    the dashboard keeps the default (re-plans first).

    `note` overrides the transition note — `approve_and_commit` passes a
    string prefixed with `AUTO_RESOLVE_NOTE_PREFIX` so the dashboard can
    tell an auto-chained resubmit apart from a manual `[e]` press."""
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
    fields: dict[str, object] = {
        "feedback": feedback,
        "last_error": None,
        "attempts": 0,
    }
    if force_execute:
        fields["result_json"] = _coerce_phase_plan(item.result_json)
    final_note = note or "resubmitted from CONFLICTED — agent will resolve"
    if force_execute:
        final_note += " (force_execute: skip plan phase)"
    store.transition(
        item.id, ItemStatus.QUEUED,
        note=final_note,
        **fields,
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


def approve_plan(
    store: Store, item: StoredItem, feedback: str | None = None,
) -> None:
    """User approved the agent's development plan. Push the item back to QUEUED
    so the daemon re-claims it; the runner sees the persisted plan in
    result_json and runs the execute phase (resumes the same claude session).

    Optional `feedback` is persisted and consumed once by the runner's
    `_prepend_feedback` on the execute phase — same path as reject_and_retry."""
    assert item.status == ItemStatus.AWAITING_PLAN_REVIEW
    fields: dict[str, object] = {}
    if feedback:
        fields["feedback"] = feedback
    store.transition(
        item.id, ItemStatus.QUEUED,
        note="plan approved — execute phase queued"
        + (" with feedback" if feedback else ""),
        **fields,
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


_DELETE_WAIT_SECONDS = 5.0
_DELETE_POLL_INTERVAL = 0.1


def delete_idea(
    config: Config | None, store: Store, daemon: "Daemon | None",
    item: StoredItem,
) -> bool:
    """Unified delete for the inspect view. Hard-removes the item from
    the store (via `Store.delete_item` — row + failures + transitions
    cleared, tombstoned in `deletions`) after tearing down any live
    runner state. Returns True when the deletion happened, False when
    the row was already tombstoned or vanished between read and write.

    Teardown order matters:
      1. If WORKING, signal the registered subprocess via
         `daemon.proc_registry.kill_one(item.id)` and poll the store until
         the runner's own error path writes a terminal-ish status (ERRORED
         / QUEUED / AWAITING_*). Bounded at ~5s so a wedged runner can't
         hang the dashboard. Waiting here narrows (but does not eliminate)
         the window where a late runner write hits a tombstoned row and
         raises KeyError; the worker swallows that via
         `Daemon._run_worker`'s broad `except Exception`.
      2. If a worktree_path is recorded (live, resumable, or forensic),
         force-remove the git worktree, prune stale registrations, and
         force-delete the feature branch. Best-effort — git errors don't
         block the hard-delete.
      3. Call `store.delete_item` LAST. This drops dependent rows + the
         items row and records a `deletions` tombstone so `scan_once`
         refuses to re-enqueue the id from the unchanged source markdown.

    `config` is only required for worktree/branch cleanup; callers with
    no worktree can pass None. `daemon` is only needed to kill a live
    subprocess; None is safe for non-WORKING items."""
    prev_status = item.status
    if store.is_deleted(item.id) or store.get(item.id) is None:
        return False

    if prev_status == ItemStatus.WORKING and daemon is not None:
        daemon.proc_registry.kill_one(item.id)
        deadline = time.monotonic() + _DELETE_WAIT_SECONDS
        while time.monotonic() < deadline:
            refreshed = store.get(item.id)
            if refreshed is None or refreshed.status != ItemStatus.WORKING:
                break
            time.sleep(_DELETE_POLL_INTERVAL)

    if config is not None and item.worktree_path:
        wt = Path(item.worktree_path)
        try:
            git_ops.worktree_remove(config.project_root, wt, force=True)
            git_ops.worktree_prune(config.project_root)
        except git_ops.GitError:
            pass
        if item.branch:
            try:
                git_ops.branch_delete(
                    config.project_root, item.branch, force=True,
                )
            except git_ops.GitError:
                pass

    try:
        store.delete_item(
            item.id, note=f"deleted from {prev_status.value}",
        )
    except KeyError:
        # Runner thread raced us and tombstoned the row (or removed it
        # via some other path) between our precheck and the write. Treat
        # as a no-op rather than propagating — the end state the operator
        # wanted is already in place.
        return False
    return True
    return True


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
