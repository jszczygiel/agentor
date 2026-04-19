# Approve cycles through review queue — 2026-04-18

## Surprises
- `r` review mode was already iterating via a for-loop; the "exits after single approval" symptom actually reproduces on the `enter`-on-row path (`_enter_action` → single screen → back to list). The fix covers both: rescanning cycle plus routing `enter` through the same cycle.

## Gotchas for future runs
- `store.list_by_status` orders by `priority DESC, created_at` — tests that rely on insertion order should assume oldest-first within equal priority.
- `_review_plan_curses` / `_review_code_curses` re-fetch the item inside the keystroke loop; keep that pattern when adding new review actions so status transitions mid-screen stay correct.

## Follow-ups
- None.

## Stop if
- Tests in `tests/test_dashboard_review_cycle.py` fail: the `_next_review_item` contract (plan-before-code, seen-id dedupe, rescans pick up newly awaiting items) is the cycle's invariant — breaking it re-introduces the single-approval exit behavior.
