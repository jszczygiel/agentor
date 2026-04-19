---
title: Multiline feedback popup leaks main table behind it on wide terminals
category: bug
state: available
---

On wide terminals (≳120 cols) the `_prompt_multiline` overlay in `agentor/dashboard/render.py` (search for `_prompt_multiline`) renders a centered frame at `box_w = min(80, w - 4)`. Nothing repaints the screen outside that frame, so the pre-existing dashboard content — main item table on the left, token/log panels on the right — bleeds through around the popup while the operator is typing. The visual is garbled rows interleaved with the text being edited, e.g.:

```
c7e83ebc  merged   —   5%   Document prior-run gotchas in CLAUDE.md  │ plan to execution. maybe we can
b2be532d  merged   —  11%   Add ability to prioritize backlog items  │ an you ask min plan what i smal
a707b9d2  merged   —   7%   Auto-queue conflict resolution           │ uld be small and considered for
```

The typed text is inside the popup; the rows on the left are stale pixels from the main table. On narrow terminals this doesn't repro because the popup width saturates `w - 4` and covers everything.

Fix options — pick one:
- **Dim-wash the backdrop.** Before drawing the popup frame, fill `stdscr` with spaces (or a single-char pattern) at `curses.A_DIM`. Cheapest, preserves the "modal over screen" feel.
- **Full-width popup.** Drop the 80-col cap so the frame extends edge to edge. Simplest, but 200-col-wide feedback fields look silly and the validator/Textbox word-wrapping wasn't designed for it.
- **Shadow rectangle.** Blank a padding rectangle (say `box_h + 2` × `box_w + 4`) centered on the frame before drawing. Nicer visual, more code.

Recommendation: dim-wash backdrop. One extra loop over `stdscr.addch` with `A_DIM`, no layout change, and it's a clear visual signal the dashboard is inert while the modal is up.

Verification: unit test is hard (the bug is visual). Manual test — open the self-hosted agentor dashboard on a ≥160-col terminal, press `f` on an AWAITING_REVIEW item to open the feedback popup, confirm no table rows are visible outside the popup frame. Check the feedback popup, the new-issue popup, and any other `_prompt_multiline` caller.
