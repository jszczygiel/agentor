---
title: Remove text from approve mode
state: available
category: polish
---

Approve mode in the dashboard currently renders text that isn't needed for the operator's decision. Strip the extraneous copy so the view is minimal — the item metadata and keybinding hints should be enough context. Verify which strings are removable by inspecting `agentor/dashboard/modes.py` and `agentor/dashboard/render.py` before editing. If the "text" the operator means is ambiguous (prompt body, instructions, status line), flag it in the plan instead of guessing.
