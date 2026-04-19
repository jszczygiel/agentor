# Always fast-forward user's base_branch checkout after auto-merge — 2026-04-19

## Surprises
- The "HEAD diverged from pre-merge base SHA" guard the ticket sketched
  is unreachable in practice: `update-ref refs/heads/<base> NEW OLD` moves
  HEAD as a *symbolic* ref before the advance helper runs, so
  `rev-parse HEAD` already returns `new_sha` by the time we check. Guard
  semantics had to flip from comparing HEAD-to-expected to comparing
  index/worktree trees to `expected_sha`.
- `git status --porcelain` in the root reports the phantom-reversion
  state as "dirty" (because HEAD moved but index didn't), so it was
  unusable as the dirty-worktree guard. Switched to
  `git diff --cached --quiet <expected_sha>` + `git diff --quiet <expected_sha>`
  + `ls-files --others --exclude-standard` which compare trees and are
  immune to the symbolic HEAD jump.
- `git merge --ff-only <new_sha>` is also a no-op once the ref lands,
  since `merge` sees HEAD already at `new_sha` and returns "Already up
  to date" without refreshing index/worktree. The advance uses
  `read-tree -u -m <expected> <new>` which applies the delta explicitly.

## Gotchas for future runs
- Any post-CAS operation on the user's checkout must reason about HEAD
  being a symbolic ref — it moves the instant `update-ref` lands, even
  though index and worktree lag. Check trees against the pre-update SHA
  rather than HEAD.
- Tests that drive through `_drive_to_awaiting_review` leave `backlog.md`
  and `.agentor/` untracked on the root; the new dirty-worktree guard
  sees them via `ls-files --others`, so tests that want the advance
  path to run need to commit a `.gitignore` first. Real operators already
  do this.

## Follow-ups
- Pre-existing F401 ruff errors in `tests/test_dashboard_resize.py`
  logged to docs/IMPROVEMENTS.md for a separate CI-audit sweep.
