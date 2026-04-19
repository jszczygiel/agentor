---
title: Fix top line hidden on narrow screens
state: available
category: bug
---

On narrow terminals the dashboard's top line is not visible to the operator. Likely a rendering/layout issue in `agentor/dashboard/render.py` where the header row gets clipped or scrolled off when the terminal width is below some threshold. Investigate how the header is drawn relative to curses window bounds and ensure it remains pinned and visible at small widths. Reproduce by shrinking the terminal horizontally and confirm the top line (likely the status/header bar) stays on screen.
