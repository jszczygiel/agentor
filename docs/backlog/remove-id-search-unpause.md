---
title: Remove id-search and unpause from dashboard footer
state: available
category: polish
---

The dashboard footer hint row in `agentor/dashboard.py` advertises `[i]d-search` and `[u]npause` as available keybindings. Drop both from the displayed hint string (around line 19-20) so the footer only lists actions the operator actually reaches for. If the underlying handlers for these keys are now dead code, remove them as well — but confirm no other mode (e.g. the sticky PAUSED banner at line 275) still depends on the `u` resume path before deleting. Keep the rest of the hint layout intact.
