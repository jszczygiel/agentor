---
title: _prompt_multiline reflows overlay on KEY_RESIZE
state: available
category: bug
---

`agentor/dashboard/render.py:790-798` — the `curses.textpad.Textbox`
validator in `_prompt_multiline` handles Ctrl-C/Esc/Ctrl-X/backspace but
treats `curses.KEY_RESIZE` (410) as a literal input char. If the operator
resizes the terminal mid-edit, the `frame` / `edit_win` windows stay at
the old dims, the overlay decouples from the centre, and long edits can
render off-screen.

Minimum-viable fix: in the validator, return 0 on `ch == curses.KEY_RESIZE`
so the char is swallowed and `Textbox.edit` keeps looping. Proper fix:
capture the current gathered text, tear down `frame` and `edit_win`,
rebuild at `stdscr.getmaxyx()` dims, and restore the text. `_handle_resize`
at `render.py:388` is the reference helper other getch loops use —
integrate it via a shim that also rebuilds the Textbox's host windows.

Verification: extend `tests/test_dashboard_resize.py` with a synthetic
`KEY_RESIZE` injection asserting the validator doesn't leak the char into
the gathered buffer.

Source: `docs/IMPROVEMENTS.md` (Open) and
`docs/agent-logs/2026-04-19-fix-top-line-hidden-narrow.md` (Follow-ups).
