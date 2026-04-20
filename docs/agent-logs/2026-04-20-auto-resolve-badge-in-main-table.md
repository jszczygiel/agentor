# Surface auto-resolve chain in main dashboard table — 2026-04-20

## Gotchas for future runs
- Narrow-tier state cell is exactly 3 chars `{marker}{glyph}{tail}` — the trailing slot is the cheapest place to stash a 1-char status modifier without disturbing `cols_used` or header alignment (`_table_header` already reserves `'S':<3`).
- `_table_row` is hot (500ms × every visible row). Short-circuit per-row auxiliary queries (`_is_auto_resolve_chain` reads transitions) on `st == QUEUED` so WORKING/AWAITING rows don't trigger a tail scan of `transitions_for` every tick.
- Render → modes imports are allowed (render as the leaf consumer); modes → render is not. Keep the helper in `dashboard/modes.py` and lazy-import from render inside the loop body to preserve that direction.

## Outcome
- Files touched: `agentor/dashboard/render.py`, `tests/test_dashboard_render.py`, `docs/backlog/auto-resolve-badge-in-main-table.md` (deleted), this log.
- Tests added: `TestAutoResolveBadge` (4 cases) in `tests/test_dashboard_render.py` — wide/mid `·auto` suffix, narrow `Qa` flip, full tier width-fit regression.
