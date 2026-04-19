---
title: Multi-line input field for feedback prompts
state: available
category: ux
---

`_prompt_text` in `agentor/dashboard/render.py:458-476` uses
`curses.getstr` on the bottom row — a single line, clipped to
`w - len(message) - 3` characters. Every feedback prompt reuses it:

- Pickup approval: `modes.py:85-86` — "feedback for agent (empty = none): ".
- Reject + retry (plan and review): `modes.py:536` —
  "feedback ({kind} retry, empty=cancel): ".
- Defer note: `modes.py:705`.

Feedback is the primary channel for steering an agent on retry — the
`Runner._append_feedback` path appends it at the tail of the next prompt.
The one-line input makes anything longer than a sentence cramped: no
newlines, no visible wrapping, no editing past the visible slice. Operators
end up writing terse one-liners when the situation warrants a paragraph.

Task: replace `_prompt_text` (at least when called from the feedback-reject
paths) with a multi-line editor.

Scope:

- Use `curses.textpad.Textbox` on a dedicated multi-line `newwin` so the
  operator can type newlines and see wrapping. Submit on a deliberate key
  (e.g. Ctrl-G, the Textbox default) rather than Enter, so Enter inserts a
  newline inside the body.
- Keep a single-line variant available for non-feedback prompts (item id
  prefix, defer note) — only the retry/pickup feedback needs the larger
  field. Either add a `multiline=True` flag to `_prompt_text`, or split into
  a new `_prompt_multiline`.
- Render the editor as an overlay panel, clearly labelled, with the submit
  keystroke visible in the footer so the operator doesn't get stuck.
- Preserve the empty-input = cancel semantics of the reject-retry paths.

Verification: manual test each feedback entry point. Unit tests are awkward
for curses input, but worth adding a smoke test that instantiating the
multi-line widget doesn't crash on a tiny terminal.
