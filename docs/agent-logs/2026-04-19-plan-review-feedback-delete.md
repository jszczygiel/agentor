# plan-review feedback/delete split — 2026-04-19

## Surprises
- Source markdown `docs/backlog/replace-plan-reject-feedback-with-feedba.md` was absent on this branch (only `deduplicate-transcript-parsing.md` remained). Assumed already-extracted; skipped `git rm`.

## Gotchas for future runs
- `_ACTION_KEYS_BY_STATUS` is pinned by `tests/test_dashboard_enter.py` as exact sets — any add/remove needs an update there plus any footer-label assertions.
- `approve_plan(store, item, feedback=...)` still accepts the optional `feedback` param even though no dashboard call site passes it now. Kept for API symmetry; future callers (or CLI) may use it.
