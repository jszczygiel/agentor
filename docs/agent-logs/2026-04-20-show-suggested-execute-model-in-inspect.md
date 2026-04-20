# Show suggested execute model in inspect view — 2026-04-20

## Outcome
- Files touched: `agentor/dashboard/modes.py`, `tests/test_dashboard_inspect_narrow.py`
- Tests added: `TestExecuteModelInspectLine` (5 cases) in `tests/test_dashboard_inspect_narrow.py`
- `_Agent.auto_execute_model = True` added to shared stub so new tests can verify the advisory suffix path without overriding per-test.
