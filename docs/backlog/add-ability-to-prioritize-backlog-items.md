---
title: Add ability to prioritize backlog items
state: available
category: feature
---

Operator needs a way to mark items as higher priority so the daemon picks them up before others. Currently `claim_next_queued` in `agentor/store.py` pulls queued items FIFO with no priority signal. Add a priority field (e.g. int or enum) surfaced in the dashboard and respected by the claim query, plus a keybinding in pickup/review modes to bump/lower an item. Unclear whether priority should also reorder BACKLOG items awaiting manual pickup or only QUEUED items — operator to clarify. Consider how this interacts with `pickup_mode = "manual"` where ordering is already operator-driven.
