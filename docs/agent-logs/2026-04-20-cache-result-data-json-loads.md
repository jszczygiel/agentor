# Cache `_result_data` json.loads per item.updated_at — 2026-04-20

## Surprises
- Backlog source `docs/backlog/cache-result-data-json-loads-per-render.md` absent at dispatch — no `git rm` to include.
- Two test fixtures in different modules (`tests/test_dashboard_formatters.py::_item`, `tests/test_dashboard_inspect_narrow.py::_mk_item`) independently baked in a fixed `id="abc12345"` + `updated_at=0.0`; one stale payload leaked across methods once the cache landed. Fixed by threading a unique per-call `updated_at` counter into both factories.

## Gotchas for future runs
- Any test that builds `StoredItem` fixtures with a fixed `(id, updated_at)` pair and mutates `result_json` across subtests will now cache-collide via `_result_data`. The cache is keyed on `(item.id, item.updated_at)` — either bump `updated_at` per fixture call, or call `_result_data_invalidate()` in `setUp`. Grep for `_result_data` readers (`formatters.py`, `dashboard/modes.py`) when adding new StoredItem factories.

## Outcome
- Files touched: `agentor/dashboard/formatters.py`, `tests/test_dashboard_formatters.py`, `tests/test_dashboard_inspect_narrow.py`.
- Tests added: `TestResultDataCache` (6 cases) covering cache-hit, eviction, invalidate, invalid JSON non-poisoning, empty `result_json`, distinct-id isolation.
