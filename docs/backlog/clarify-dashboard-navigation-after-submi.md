---
title: Clarify dashboard navigation after submitting merge
state: available
category: bug
---

Operator reports that after submitting a merge from the dashboard they "get back to" somewhere unexpected, but the note is truncated ("when i submit merge i get bsvk to") and the destination is unclear. Investigate what view the dashboard returns to after a merge action (e.g. CONFLICTED retry via `[m]`, or AWAITING_REVIEW approve flow in `agentor/dashboard/modes.py` and `agentor/committer.py`) and confirm whether the post-merge focus lands on the intended mode/item. If the current behaviour is wrong, decide whether to stay on the merged item, advance to the next review candidate, or return to the main table. Re-confirm intent with the operator before changing behaviour, since the original note was incomplete.
