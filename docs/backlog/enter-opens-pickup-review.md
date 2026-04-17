---
title: Make enter open pickup/review actions for selected item
state: available
category: feature
---

Currently in the main dashboard table, pressing enter on a row calls `_inspect_render` (dashboard.py:143-152), which just shows the read-only inspect view. The operator wants enter to instead surface the same action menu you get via `p` (pickup) for backlog items or `r` (review) for items awaiting review, routed by the selected row's status. Backlog/queued/deferred rows should jump into the pickup flow; awaiting_plan_review and awaiting_review rows should jump into the review flow. Items without a relevant action (e.g. working, merged) can fall back to the current inspect view or be a no-op — decide based on what feels least surprising. Keep `i` bound to inspect so the old behavior is still reachable.
