---
title: Replace plan reject/feedback with feedback and delete
state: available
category: polish
---

Plan review currently offers `reject` and `feedback` actions in the dashboard. Operator wants the plan-review actions to mirror the execute-review split: a `feedback` action (requeue the plan with notes) and a `delete` action (drop the item entirely), removing the standalone `reject` path. Audit `agentor/dashboard/modes.py` and the plan-review key bindings to swap the action set, and ensure the store transition for delete matches what the execute-review delete flow does. Preserve existing feedback-append semantics so the next plan run consumes the note once and clears it.
