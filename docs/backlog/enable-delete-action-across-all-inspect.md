---
title: Enable delete action across all inspect stages
state: available
category: feature
---

Inspect mode currently exposes delete only in select statuses; operators want a consistent way to remove the current item from any stage (backlog, queued, working, awaiting_plan_review, awaiting_review, conflicted, deferred, errored, merged, rejected, cancelled). Wire a single delete keybinding in `agentor/dashboard` inspect view that works regardless of `ItemStatus`, with confirmation before destructive action. For live stages (working, awaiting_*), ensure the runner session is torn down and the worktree is cleaned up via the existing cancellation path before the row is removed. Update help text so the binding is discoverable in every stage.
