# Remove BACKLOG from ItemStatus enum — 2026-04-19

## Surprises
- Task description cited `dashboard/modes.py:_pickup_one_screen` but that function no longer exists — the DEFERRED→BACKLOG history-walk fallback now lives in `_inspect_dispatch` inside `dashboard/modes.py` (recent refactor). Grep by symbol, not prior path.
- `agentor/fold.py:_NON_TERMINAL_STATUSES` also listed `BACKLOG`; it was missed by the original scope walkthrough and only surfaced on import.

## Gotchas for future runs
- Removing an `ItemStatus` member requires healing BOTH `items.status` AND `transitions.from_status` + `transitions.to_status` in `_migrate`. A migration that touches only `items` leaves `transitions_for` ready to `ValueError` on the next read of any legacy-history item.
- `previous_settled_status` tolerates the heal silently: legacy `QUEUED→BACKLOG→QUEUED` collapses to `QUEUED→QUEUED→QUEUED`; the function falls through to QUEUED either way.

## Follow-ups
- None — fold-checker tuple and CLAUDE.md both updated inline.

## Stop if
- A test fails with `AttributeError: type object 'ItemStatus' has no attribute 'BACKLOG'` after these changes — means another import of the symbol was missed; `grep -rn BACKLOG agentor/ tests/` before committing.
