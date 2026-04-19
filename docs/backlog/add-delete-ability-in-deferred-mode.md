---
title: Add delete ability in deferred mode
state: available
category: feature
---

Deferred mode in the dashboard currently lists deferred items but offers no way to remove them outright — the operator can only re-queue or inspect. Add a delete keybinding (e.g. `x` or `D`) within deferred mode that permanently removes the selected item from the store, with a confirmation prompt to guard against accidental deletion. Ensure the transition is recorded in the `transitions` table for audit, and that the row is removed cleanly from `items` plus any dependent rows (`failures`, feedback). Note: the raw note mentions "deferred mode ability" — interpreting as a delete action scoped to that mode; clarify with operator if a broader delete (across all statuses) was intended.
