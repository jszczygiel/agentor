# priority keybinding in inspect view — 2026-04-19

## Gotchas for future runs
- `_inspect_render` lowercases `ch` via `chr(ch).lower()` before dispatch, so `ord("P")`/`ord("O")` must be matched against raw `ch` *before* that line, not via the lowercased `k`. Also applies to any future uppercase-only bindings.
- `curses` was not imported in `agentor/dashboard/modes.py` — `render.py` and `__init__.py` have it, but modes.py relied purely on curses constants it received via other helpers. Any new Shift-arrow / keypad binding in modes.py needs an explicit `import curses`.
- `_flash` calls `curses.napms(1200)` — tests driving `_inspect_render` must patch `agentor.dashboard.render.curses.napms` (that is where the symbol resolves), not `modes.curses.napms`.
