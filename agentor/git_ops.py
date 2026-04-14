import subprocess
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
