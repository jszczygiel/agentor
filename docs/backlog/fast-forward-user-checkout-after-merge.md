---
title: Always fast-forward user's base_branch checkout after auto-merge
state: available
category: feature
---

After `merge_feature_into_base` CAS-advances `refs/heads/<base_branch>` via `update-ref` (see `agentor/git_ops.py:180-194`), the user's primary checkout at `project.root` still has its index and working tree pinned to the old base SHA. If the operator has `base_branch` checked out (the typical self-hosted/dogfood scenario), `git status` accumulates thousands of phantom "reversions" as more merges land — the current working tree of agentor itself demonstrates this (129 files dirty, −8802 lines, entirely the gap between the worktree's old tip and `refs/heads/main`'s current tip). The CLAUDE.md invariant "auto-merge never touches the user's checkout of base_branch" needs to flip: the new default should be that every successful auto-merge advances the user's checkout too.

Proposed behaviour: after every clean auto-merge (conflict=None, CAS succeeded), fast-forward the user's primary checkout at `project.root` so `HEAD`, index, and working tree all point at the new base tip. This becomes the default — not an opt-in.

Safety rails (required to avoid clobbering user work):

- Advance the branch ref FIRST via `update-ref` (existing CAS path), then advance the checkout. If checkout-advance fails or races, the ref stays at the new tip and the user sees the existing "behind" state rather than losing data.
- Skip the checkout advance (and surface a visible, non-fatal dashboard message — not silent) when:
  - The primary checkout is not on `base_branch` (feature branch, detached HEAD) — operator made a deliberate choice; respect it.
  - Working tree or index is dirty (`git status --porcelain` non-empty) — we must never overwrite uncommitted work. Prompt the operator to commit/stash before the next merge will be able to advance the checkout.
  - `HEAD` doesn't resolve to `base_sha_before` (the pre-merge SHA captured at `git_ops.py:164-166`) — means the user committed or reset between dispatch and merge; pure-ff is no longer available.
- Config escape hatch: `git.advance_user_checkout: bool = true` in `agentor.toml` — true by default per the new behaviour, set false if the operator wants to stay on a different branch across merges.

Implementation sketch: add `advance_user_checkout(repo, base_branch, expected_sha, new_sha) -> tuple[bool, str | None]` in `agentor/git_ops.py`. Runs the guards, then `git -C <repo> merge --ff-only <new_sha>` (or `read-tree -m -u HEAD <new_sha>` + `update-ref HEAD <new_sha>` to avoid any merge hooks firing). Call from `committer.approve_and_commit` and `committer.retry_merge` on the success path, after the existing `update-ref` advances base. Return a summary string for the dashboard when skipped so the operator knows *why* the checkout stayed put (e.g. "checkout not advanced — dirty worktree").

Update `CLAUDE.md:56` invariant: replace "auto-merge never touches the user's checkout of base_branch" with "auto-merge advances the user's checkout to the new base tip when (a) the checkout is on base_branch, (b) the worktree is clean, and (c) HEAD matches the pre-merge SHA — otherwise the ref still advances but the checkout is left untouched and the dashboard surfaces the reason."

Testing: extend `tests/test_committer.py` covering (a) clean ff happy path — ref advances AND checkout advances, (b) dirty worktree → ref advances, checkout skipped with surfaced reason, (c) checkout on a different branch → ref advances, checkout skipped, (d) HEAD diverged from base_sha_before → ref advances, checkout skipped, (e) config gate off → checkout skipped unconditionally even when safe, (f) mid-advance failure (simulated by read-only worktree) → base ref still at new tip, checkout untouched, no rollback.

One-shot recovery note: once this lands, operators stuck in the "129 phantom reversions" state can escape by committing or stashing anything they care about, then running a no-op agentor operation (or `git reset --hard <base-tip>` manually). Document that in the PR.
