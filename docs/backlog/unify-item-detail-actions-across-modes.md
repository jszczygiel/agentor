---
title: Unify item detail actions across modes
state: available
category: feature
---

Pressing Enter on an item in the main dashboard table should open the same detail view with the same action set that the user sees when entering an item from pickup, review, or deferred modes. Today the available keys differ depending on which mode the user arrived from, which forces operators to back out and re-enter through a specific mode to reach an action. The inspect view in `agentor/dashboard/` should expose the full action set (approve, reject, retry merge, defer, feedback, etc.) gated only by the item's current status, not by the mode used to open it. Goal: one consistent detail view regardless of entry point.
