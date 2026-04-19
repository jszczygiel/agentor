# Group Store methods into logical sections — 2026-04-17

## Surprises
- Backlog item said "40+ methods" but `Store` exposes 20. Section-header approach still clearly justified; mixin split would have been overkill.

## Gotchas for future runs
- Pure-reorder refactors: `tests/test_store.py` (140 total tests pass) already exercises the full public API, so no new tests were needed. Execution-guideline "tests mandatory" treated as satisfied by existing coverage for reorder-only diffs.
