# Break token usage panel down by model tier — 2026-04-20

## Surprises
- `_fmt_token_row` returning a multi-line string would have broken the single `_safe_addstr` call in `render.py`; added a separate `_fmt_tier_row` function instead.
- Two additional `_FakeStore` stubs in `test_dashboard_resize.py` and `test_dashboard_render.py` needed the `classifier` kwarg — caught by full suite run.

## Gotchas for future runs
- Any test-only `_FakeStore` that stubs `aggregate_token_usage` must accept `**kwargs` or the explicit `classifier=None` kwarg or it will break when the formatter calls it with `classifier=`.

## Outcome
- Files touched: `agentor/store.py`, `agentor/dashboard/formatters.py`, `agentor/dashboard/render.py`, `tests/test_store.py`, `tests/test_dashboard_formatters.py`, `tests/test_dashboard_resize.py`, `tests/test_dashboard_render.py`.
- Tests added: `TestAggregateTokenUsage.test_classifier_*` (4 new), `TestTokenWindowsCache.test_provider_*` (3 new), `TestFmtTierRow` (7 new cases).
