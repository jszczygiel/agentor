# merge main into queued-actions branch — 2026-04-19

## Surprises
- Only real conflict was a test-method-name collision in `tests/test_dashboard_enter.py` — main had renamed `test_awaiting_plan_review_has_approve_feedback_reject_defer` → `test_awaiting_plan_review_has_approve_feedback_defer_delete` and dropped `[f]approve+feedback` from the plan-review action set. Auto-merge resolved `modes.py` cleanly by keeping main's trimmed plan-review set and this branch's new QUEUED entries.
- Result: `r` means different things per status — QUEUED `r` → terminal REJECTED (mine); AWAITING_PLAN_REVIEW `r` → requeue with feedback (main's rework). Footer labels (`[r]eject+feedback` vs `[r]feedback`) already differentiate, so no code change needed.

## Gotchas for future runs
- When main reshuffles `_ACTION_KEYS_BY_STATUS` entries, the test-method-name guards in `tests/test_dashboard_enter.py::TestInspectActionMap` are the primary merge-conflict surface. Expect to adopt main's renamed method signature and keep the shared assertion body.

## Stop if
- A future merge also pulls a change that reuses `r` on QUEUED with a non-terminal meaning. That would collapse the QUEUED vs plan-review asymmetry and require rethinking the label-by-status convention.
