# Prioritize backlog items — 2026-04-17

## Surprises
- `tests/test_dashboard_formatters.py` builds `StoredItem` positionally; any new required field forces a test patch. Spotted only after running the suite — grep `StoredItem(` before schema changes.

## Gotchas for future runs
- SQLite `ALTER TABLE ... DROP COLUMN` isn't universally available on stdlib `sqlite3` bundled with older system Pythons. The migration test emulates the pre-migration schema via `CREATE TABLE items_new` + `INSERT ... SELECT` + rename — use the same recipe if you need to simulate older DB state.
- `bump_priority` deliberately writes no transitions row (per-keystroke spam would drown real state history). Keep it that way for any future "settings"-style toggles.

## Follow-ups
- Consider surfacing priority as a table column or a `*` glyph when non-zero — for now the reorder itself is the only visual cue.
