---
title: Adjust item priority with plus and minus in inspect view
state: available
category: feature
---

In the dashboard's inspect view (opened with `i` on a selected item), add keybindings so `+` raises the item's priority and `-` lowers it. Priority is already surfaced elsewhere in the dashboard (see `agentor/dashboard/` priority indicator work), so this extends the same concept into the per-item inspect screen rather than introducing a new field. The handler should persist the change through `Store.transition` or an equivalent update path and refresh the inspect panel so the new value shows immediately. If the current priority model only supports a fixed set of levels, clamp at the ends rather than wrapping.
