# Multiline feedback popup leaks main table — 2026-04-19

## Gotchas for future runs
- Test doubles for `stdscr` must add `addnstr` — `_FakeStdscr` in
  `tests/test_dashboard_prompt.py` did not implement it, so adding any
  paint call to `_prompt_multiline` pre-blows the existing tests unless
  the fake is extended first. Same caveat applies to any new stdscr paint
  in `render.py`.
- `curses.newwin` + `_FakeWin` is already patched via `_install_fakes`;
  passing a shared `event_log` list lets tests lock ordering between
  stdscr paints and popup window creation without inventing a new harness.
- The main dashboard loop erases stdscr every `REFRESH_MS=500ms`
  (`render.py:85`), so any one-shot backdrop paint written to stdscr is
  reclaimed on the next tick. Don't add manual `touch` loops for the
  underlying panels — they repaint themselves.
