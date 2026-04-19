# delete-in-deferred — 2026-04-19

## Surprises
- The `x` keybinding + `_prompt_yn` + `delete_idea` were already wired in `dashboard/modes.py` and `committer.py`. The gap was behavioural: `delete_idea` transitioned to CANCELLED rather than physically removing rows. Just rewiring `delete_idea` plus the store-layer `delete_item` closed the feature.
- `PRAGMA foreign_keys = ON` is set on every connection (`store.py:181`). DELETE order matters — dependents first, items last — or SQLite rejects the parent delete.
- `upsert_discovered` had to learn about the tombstone table too. Without that, any permanent delete would be resurrected by the very next `scan_once` as long as the source markdown still carried the entry.

## Gotchas for future runs
- When adding a new schema table, `CREATE TABLE IF NOT EXISTS` inside the `SCHEMA` script is enough — `_migrate` is only needed for in-place ALTERs on pre-existing DBs.
- `delete_item` uses `INSERT OR REPLACE INTO deletions` so a re-deletion of the same id (e.g. after the operator re-adds + deletes the same backlog line twice) overwrites the old tombstone rather than erroring.

## Follow-ups
- Cross-status delete (ERRORED/REJECTED/CONFLICTED) was left out of scope — trivial to extend by adding `x` entries to those keymaps plus mirroring the confirm/call site.
- Tombstone is forever by design. If the operator ever wants a prune path (e.g. to re-adopt a previously-deleted id), add a `Store.undelete(item_id)` + a dashboard action.
- `ItemStatus.CANCELLED` no longer has a producer; consider removing the enum member in a later sweep (would require `_migrate` heal for legacy rows).

## Stop if
- A future run finds `PRAGMA foreign_keys` flipped to OFF — the `delete_item` DELETE order is the only safety net and an FK-off store will silently orphan rows instead of failing loudly.
