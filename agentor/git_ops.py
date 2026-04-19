import subprocess
import uuid
from pathlib import Path


class GitError(RuntimeError):
    pass


def run(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    cp = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    )
    if check and cp.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed ({cp.returncode}) in {cwd}:\n"
            f"stdout: {cp.stdout}\nstderr: {cp.stderr}"
        )
    return cp


def worktree_add(repo: Path, path: Path, branch: str, base: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run(repo, "worktree", "add", "-b", branch, str(path), base)


def worktree_remove(repo: Path, path: Path, force: bool = False) -> None:
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(path))
    run(repo, *args, check=False)


def worktree_prune(repo: Path) -> None:
    """Drop stale worktree registrations (the directory was removed but
    `.git/worktrees/<name>/` still exists). Idempotent."""
    run(repo, "worktree", "prune", check=False)


def worktree_list(repo: Path) -> list[dict]:
    cp = run(repo, "worktree", "list", "--porcelain")
    out: list[dict] = []
    cur: dict = {}
    for line in cp.stdout.splitlines():
        if not line:
            if cur:
                out.append(cur)
                cur = {}
            continue
        if " " in line:
            k, v = line.split(" ", 1)
        else:
            k, v = line, ""
        cur[k] = v
    if cur:
        out.append(cur)
    return out


def current_branch(repo: Path) -> str:
    cp = run(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return cp.stdout.strip()


def branch_exists(repo: Path, branch: str) -> bool:
    cp = run(repo, "rev-parse", "--verify", branch, check=False)
    return cp.returncode == 0


def branch_checked_out_at(repo: Path, branch: str) -> Path | None:
    """Return the worktree path that currently has `branch` checked out, or
    None if no worktree holds it. Used before `branch_delete` to remove
    the worktree first — git refuses to delete a branch checked out
    elsewhere, even with -D."""
    target = f"refs/heads/{branch}"
    for entry in worktree_list(repo):
        if entry.get("branch") == target:
            wt = entry.get("worktree")
            if wt:
                return Path(wt)
    return None


def branch_delete(repo: Path, branch: str, force: bool = True) -> None:
    flag = "-D" if force else "-d"
    run(repo, "branch", flag, branch, check=False)


def diff_vs_base(worktree: Path, base: str) -> str:
    """Return combined diff (staged + unstaged + untracked) vs the base branch."""
    tracked = run(
        worktree, "diff", f"{base}...HEAD", check=False
    ).stdout
    working = run(worktree, "diff", "HEAD", check=False).stdout
    # untracked files, shown as new file diffs
    untracked = run(
        worktree, "ls-files", "--others", "--exclude-standard"
    ).stdout.strip().splitlines()
    untracked_diff_parts: list[str] = []
    for f in untracked:
        add = run(worktree, "diff", "--no-index", "/dev/null", f, check=False)
        untracked_diff_parts.append(add.stdout)
    return tracked + working + "".join(untracked_diff_parts)


def commit_all(worktree: Path, message: str) -> str:
    run(worktree, "add", "-A")
    run(worktree, "commit", "-m", message)
    cp = run(worktree, "rev-parse", "HEAD")
    return cp.stdout.strip()


def is_inside_repo(path: Path) -> bool:
    cp = run(path, "rev-parse", "--is-inside-work-tree", check=False)
    return cp.returncode == 0 and cp.stdout.strip() == "true"


def fast_forward_to_base(
    worktree: Path, base_branch: str,
) -> tuple[bool, str | None]:
    """Bring the worktree's checked-out branch up to the current tip of
    `base_branch` via `git merge --ff-only`. Used on resume so an item
    that waited through a long plan-review gap picks up any commits that
    landed on base in the meantime, before the agent starts producing
    commits that would otherwise be based on a stale fork point.

    Returns:
        (True, None)            — worktree was advanced (or was already at
                                  base's tip; git reports no-op as success).
        (False, reason)         — fast-forward refused because the feature
                                  has diverged (unexpected at execute start
                                  but possible if the agent committed
                                  during plan). Caller decides whether to
                                  escalate or proceed; state is untouched.
    """
    cp = run(worktree, "merge", "--ff-only", base_branch, check=False)
    if cp.returncode == 0:
        return True, None
    return False, (cp.stdout + cp.stderr).strip() or "ff-only refused"


def advance_user_checkout_allowed(
    repo: Path, base_branch: str, base_sha_before: str,
) -> tuple[bool, str | None]:
    """Pre-CAS guard check for `advance_user_checkout`. Call BEFORE
    `merge_feature_into_base` runs its CAS ref update — once the ref has
    moved, HEAD symbolically follows it and both the HEAD and the clean-
    tree guards become unreliable (stale index reports spurious staged
    diffs against the new HEAD tree).

    Returns `(True, None)` when every guard holds. Returns `(False,
    reason)` otherwise; the reason string is short and dashboard-safe so
    the caller can surface it on the MERGED transition note.

    Guards (first failing one wins):
      - `repo`'s current branch is `base_branch`. Detached HEAD is
        detected via `current_branch` returning `"HEAD"` (git's
        `rev-parse --abbrev-ref` shorthand) and reported as
        `"detached HEAD"`; any other branch is reported as
        `"checkout on <branch>"`.
      - working tree is clean (`git status --porcelain` empty) — never
        risk clobbering uncommitted user work. Reported as
        `"dirty worktree"`.
      - HEAD resolves to `base_sha_before` — the checkout sits exactly at
        the pre-merge base tip. Reported as
        `"HEAD diverged from pre-merge base"` when the user committed or
        reset above base between dispatch and merge."""
    current = current_branch(repo)
    if current != base_branch:
        return False, "detached HEAD" if current == "HEAD" \
            else f"checkout on {current}"
    if run(repo, "status", "--porcelain", check=False).stdout.strip():
        return False, "dirty worktree"
    head = run(repo, "rev-parse", "HEAD", check=False).stdout.strip()
    if head != base_sha_before:
        return False, "HEAD diverged from pre-merge base"
    return True, None


def advance_user_checkout(repo: Path, new_sha: str) -> bool:
    """Sync `repo`'s primary checkout (index + working tree) to `new_sha`
    after `merge_feature_into_base` has CAS-advanced the ref. The ref
    itself already points at `new_sha`; this brings HEAD's working-tree
    view up to date so `git status` doesn't report spurious staged diffs
    and editors don't read stale files.

    Must only be called after `advance_user_checkout_allowed` returned
    True against the pre-CAS state — this function intentionally does no
    guards of its own (post-CAS guard checks are unreliable, see
    `advance_user_checkout_allowed`). Uses `git reset --hard` so no hooks
    fire (unlike `git merge --ff-only`, which triggers post-merge).
    Returns True iff the reset succeeded."""
    cp = run(repo, "reset", "--hard", new_sha, check=False)
    return cp.returncode == 0


def merge_feature_into_base(
    repo: Path, feature_branch: str, base_branch: str, message: str,
    tmp_root: Path, mode: str = "merge",
) -> tuple[str | None, str | None]:
    """Integrate `feature_branch` into `base_branch` without touching any
    worktree the user might have checked out on base. All work happens in a
    throwaway `--detach`ed worktree pinned to base's current tip; the final
    step CAS-advances `refs/heads/<base_branch>` via `update-ref OLD NEW`
    so a concurrent commit on base aborts us safely instead of overwriting
    unseen work.

    mode="merge" (default) — `git merge --no-ff`, produces a merge commit.
    mode="rebase"          — `git rebase <base>` onto the temp worktree,
                             then CAS-fast-forward base to the rebased tip
                             for a linear history.

    Returns (new_base_sha, None) on success, (None, summary) on conflict
    or CAS loss. The temporary worktree is always cleaned up; feature
    branch and its worktree are never mutated."""
    if mode not in ("merge", "rebase"):
        raise ValueError(f"unknown merge_mode: {mode!r}")
    base_sha = run(
        repo, "rev-parse", f"refs/heads/{base_branch}"
    ).stdout.strip()
    tmp_root.mkdir(parents=True, exist_ok=True)
    tmp_dir = tmp_root / f"merge-{uuid.uuid4().hex[:8]}"
    run(repo, "worktree", "add", "--detach", str(tmp_dir), base_sha)
    try:
        if mode == "merge":
            return _run_merge(repo, tmp_dir, feature_branch, base_branch,
                              base_sha, message)
        return _run_rebase(repo, tmp_dir, feature_branch, base_branch,
                           base_sha)
    finally:
        run(repo, "worktree", "remove", "--force", str(tmp_dir), check=False)


def _cas_update_base(
    repo: Path, base_branch: str, new_sha: str, base_sha_before: str,
) -> str | None:
    """CAS-update base; returns a failure summary string or None on success."""
    cp = run(
        repo, "update-ref", f"refs/heads/{base_branch}", new_sha,
        base_sha_before, check=False,
    )
    if cp.returncode != 0:
        return (
            f"base branch {base_branch} moved during merge "
            "(update-ref CAS failed); retry when stable.\n"
            f"{cp.stderr}"
        )
    return None


def _conflict_paths(status_out: str) -> list[str]:
    return [
        ln[3:] for ln in status_out.splitlines()
        if ln[:2] in ("UU", "AA", "DD", "AU", "UA", "DU", "UD")
    ]


def _summarize(conflicts: list[str], git_out: str, fallback: str) -> str:
    parts: list[str] = []
    if conflicts:
        parts.append("conflicted files:")
        parts.extend(f"  {c}" for c in conflicts)
        parts.append("")
    out = git_out.strip()
    if out:
        parts.append(out)
    return "\n".join(parts) or fallback


def _run_merge(
    repo: Path, tmp_dir: Path, feature_branch: str, base_branch: str,
    base_sha: str, message: str,
) -> tuple[str | None, str | None]:
    cp = run(
        tmp_dir, "merge", "--no-ff", "-m", message, feature_branch,
        check=False,
    )
    if cp.returncode == 0:
        new_sha = run(tmp_dir, "rev-parse", "HEAD").stdout.strip()
        err = _cas_update_base(repo, base_branch, new_sha, base_sha)
        return (None, err) if err else (new_sha, None)
    conflicts = _conflict_paths(
        run(tmp_dir, "status", "--porcelain", check=False).stdout
    )
    run(tmp_dir, "merge", "--abort", check=False)
    return None, _summarize(conflicts, cp.stdout + cp.stderr,
                            "merge failed with unknown error")


def _run_rebase(
    repo: Path, tmp_dir: Path, feature_branch: str, base_branch: str,
    base_sha: str,
) -> tuple[str | None, str | None]:
    """Rebase plays the feature's commits onto base in the temp worktree.
    We check out the feature tip detached first so the feature branch ref
    is never rewritten — only the temp HEAD moves — then CAS-advance base
    to the rebased HEAD."""
    run(tmp_dir, "checkout", "--detach", feature_branch)
    cp = run(tmp_dir, "rebase", base_branch, check=False)
    if cp.returncode == 0:
        new_sha = run(tmp_dir, "rev-parse", "HEAD").stdout.strip()
        err = _cas_update_base(repo, base_branch, new_sha, base_sha)
        return (None, err) if err else (new_sha, None)
    conflicts = _conflict_paths(
        run(tmp_dir, "status", "--porcelain", check=False).stdout
    )
    run(tmp_dir, "rebase", "--abort", check=False)
    return None, _summarize(conflicts, cp.stdout + cp.stderr,
                            "rebase failed with unknown error")
