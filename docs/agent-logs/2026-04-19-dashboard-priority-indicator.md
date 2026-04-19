# Visual priority indicator for dashboard rows — 2026-04-19

## Gotchas for future runs
- Unit-testing `_render_table` directly requires stubbing `curses.color_pair` — it raises `_curses.error: must call initscr() first` outside a `curses.wrapper` context. `test_dashboard_render.py:TestPriorityGlyph` patches `agentor.dashboard.render.curses.color_pair` to sidestep this; reuse the pattern for any future direct-table-render test.
- The TITLE column is not a fixed-width slot — it's whatever width is left after the other `_COL_*` constants. When adding prefix decorations inside the title cell, shrink `title_max` by the decoration width so long titles still truncate correctly on narrow terminals.
