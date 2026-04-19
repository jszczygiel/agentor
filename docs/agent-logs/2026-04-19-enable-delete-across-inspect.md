# Enable delete across inspect stages — 2026-04-19

## Surprises
- Source markdown `docs/backlog/enable-delete-action-across-all-inspect.md`
  was not present on the branch or in main's git history — nothing to
  `git rm` despite execute-phase instructions calling for one.
- Main landed a hard-delete (`Store.delete_item` + `deletions` tombstone)
  between the first attempt and this merge. Reconciliation switched this
  branch from soft `transition(CANCELLED)` to the tombstone path. Net:
  operator delete is now irreversible — the scanner can't resurrect a
  deleted id even if the source markdown still carries it.

## Gotchas for future runs
- `_inspect_dispatch` tests must patch `agentor.committer._DELETE_WAIT_SECONDS`
  when exercising the WORKING-teardown path. Without the patch each
  subTest stalls ~5s polling for a runner that never transitions the
  stub out of WORKING.
- The `[x]delete` handler lives BEFORE the per-status branches in
  `_inspect_dispatch` because it's a cross-cutting action. New per-status
  keys should keep that ordering — delete must not be gated by
  per-status fallthrough.
- `delete_idea` now hard-deletes. Re-checking `store.is_deleted(item.id)`
  and `store.get(item.id)` at the top of the function is load-bearing —
  without it, racing callers would hit `KeyError` inside
  `store.delete_item`. The function wraps that KeyError too as a belt-
  and-braces guard against the runner-race window (WORKING row torn down
  by a concurrent writer during our wait poll).
- Hard-deleting a WORKING item may race with the runner's post-kill
  `store.transition(ERRORED, …)` or `note_infra_failure`. Both raise
  `KeyError` on a missing row. `Daemon._run_worker` catches it via its
  outer `except Exception` and logs "worker crashed: <id>: <err>" — not
  fatal, but noisy. Acceptable collateral; the alternative was plumbing
  a "being-deleted" flag through the runner which was out of scope.

## Follow-ups
- None — scope closed with main's hard-delete semantics inherited.

## Stop if
- Tests hanging >10s on a dashboard test suggests the `_DELETE_WAIT_SECONDS`
  patch is missing or the poll-loop is spinning on a stub that never
  exits WORKING.
- If `Store.delete_item` ever removes the `deletions` tombstone write,
  re-enable the scanner-based resurrection guard or revert to the soft
  CANCELLED path — hard-delete without tombstone creates a
  scan-re-enqueue loop.
