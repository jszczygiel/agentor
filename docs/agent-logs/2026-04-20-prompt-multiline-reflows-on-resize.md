# _prompt_multiline reflows overlay on KEY_RESIZE — 2026-04-20

## Surprises
- `Textbox.edit` reads `self.win.getch()` each loop, so swapping `box.win`
  mid-`edit()` is a clean reflow — no need to break/restart the editor.
- Returning `0` from the validator (vs. a printable char) maps to
  `if not ch: continue` in `Textbox.edit`, which is exactly the
  "swallow this keycode" semantics the resize path wants.

## Gotchas for future runs
- `curses.textpad.Textbox` caches `maxy`/`maxx` once at `__init__` (via the
  underscored `_update_max_yx`). Any code that swaps `box.win` MUST also
  reset `box.maxy`/`box.maxx` or `do_command` will use stale bounds. Setting
  the public attributes directly is the documented-by-source escape hatch;
  calling `_update_max_yx()` would also work but is private API.
- After overlay rebuild on resize, the underlying dashboard is blank until
  the next `_render` tick. Acceptable because `_render` runs every 500ms,
  but adding a `stdscr.touchwin()`/`stdscr.refresh()` loop in long-blocking
  overlays would surface it.

## Follow-ups
- Long lines clip when the operator shrinks the terminal mid-edit; the
  rebuild paints with `addnstr(line, nic - 1)`. A wrapping pre-pass would
  preserve the full text but adds complexity and the current behavior
  matches every other narrow-tier render in the dashboard.
- `_prompt_text` (single-line variant) does not handle KEY_RESIZE either —
  it uses `stdscr.getstr` which blocks on the OS line discipline and
  therefore can't observe the keycode. Worth checking whether a resize
  during a single-line prompt corrupts the screen as a separate item.

## Outcome
- Files touched: `agentor/dashboard/render.py`,
  `tests/test_dashboard_prompt.py`,
  `docs/backlog/prompt-multiline-reflows-on-resize.md` (deleted),
  `docs/agent-logs/2026-04-20-prompt-multiline-reflows-on-resize.md`.
- Tests added: `TestPromptMultilineResize` in
  `tests.test_dashboard_prompt` with four cases —
  `test_key_resize_swallowed_by_validator`,
  `test_key_resize_rebuilds_overlay`, `test_key_resize_restores_text`,
  `test_key_resize_to_tiny_terminal_cancels`.
- Full suite: 537 tests OK.
