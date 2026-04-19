# queued reject/feedback actions — 2026-04-19

## Surprises
- Ticket described "pickup mode" offering approve/defer — stale since `28168db` removed pickup. Reality: QUEUED row had only `[x]delete`. Mapped the ask to extending QUEUED's inspect-view action set.
- `StubRunner.do_work` doesn't call `_prepend_feedback`, so an end-to-end "seed → consume → clear" test via StubRunner doesn't work. Swapped the consumption assertion for a same-state transition audit-row check — the consumption path is ClaudeRunner/CodexRunner-specific.

## Gotchas for future runs
- `tests/test_dashboard_enter.py::TestInspectActionMap` hard-codes the expected key set per status. Any `_ACTION_KEYS_BY_STATUS` change must update both `test_view_only_statuses_have_only_delete` and `test_approve_key_is_a_where_an_approve_action_exists`, plus add/update the dedicated per-status test.
- `Store.transition` accepts same-state transitions and writes a history row — useful when persisting operator-provided data (like feedback) without moving the item through the lifecycle.

## Follow-ups
- None — ticket intent captured by reject/feedback/defer on QUEUED.

## Stop if
- `_ACTION_KEYS_BY_STATUS[ItemStatus.QUEUED]` no longer contains `f`/`r`/`s` — regression; the guard in `test_dashboard_enter.test_queued_has_feedback_reject_defer_delete` should catch it.
