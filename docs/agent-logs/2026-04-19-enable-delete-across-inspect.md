# Enable delete across inspect stages — 2026-04-19

## Surprises
- Source markdown `docs/backlog/enable-delete-action-across-all-inspect.md`
  was not present on the branch or in main's git history — nothing to
  `git rm` despite execute-phase instructions calling for one.
- Another branch (`46b3745` in `git log --all`) already reworks
  `delete_idea` into a hard DB deletion with a tombstone. It hasn't
  merged into main yet — this work targets the current main's soft
  CANCELLED semantics. A post-merge reconciliation may be needed.

## Gotchas for future runs
- `_inspect_dispatch` tests must patch `agentor.committer._DELETE_WAIT_SECONDS`
  when exercising the WORKING-teardown path. Without the patch each
  subTest stalls ~5s polling for a runner that never transitions the
  stub out of WORKING.
- The `[x]delete` handler lives BEFORE the per-status branches in
  `_inspect_dispatch` because it's a cross-cutting action. New per-status
  keys should keep that ordering — delete must not be gated by
  per-status fallthrough.

## Stop if
- Tests hanging >10s on a dashboard test suggests the `_DELETE_WAIT_SECONDS`
  patch is missing or the poll-loop is spinning on a stub that never
  exits WORKING.
