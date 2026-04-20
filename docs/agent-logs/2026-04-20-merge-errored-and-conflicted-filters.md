# Merge errored and conflicted dashboard filter tabs — 2026-04-20

## Gotchas for future runs
- `_build_status_line` test fixture `_counts()` in `tests/test_dashboard_render.py::TestStatusLineTier` is shared across every tier test; adding a status count there flows into all siblings. Existing assertions only grep for `queued=2`/`rejected=0`/`R=4`, so a new status slot is safe — but any future refactor must re-audit the tier sibling tests.
- Filter names with embedded whitespace (`"needs attention"`) are rendered only via f-strings in the header bar and indexed by position in `__init__.py`; no codepath splits on whitespace. Safe pattern for future multi-word filters.

## Outcome
- Files touched: `agentor/dashboard/render.py`, `tests/test_dashboard_render.py`, `docs/backlog/merge-errored-and-conflicted-dashboard-f.md` (deleted).
- Tests added: `TestNeedsAttentionFilter` (4 cases), plus `TestStatusLineTier.test_wide_collapses_errored_and_conflicted_into_needs_attention`, `test_mid_collapses_errored_and_conflicted_into_bang_token`, `test_narrow_collapses_errored_into_bang_token`.
