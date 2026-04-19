# Token-panel budget percentage — 2026-04-20

## Surprises
- Budget knobs (`session_token_budget`, `weekly_token_budget`) and `_fmt_pct_of_budget` already existed for the compact status-line indicator. Scope shrank to threading the same helper into the three panel-row formatters + caller.

## Gotchas for future runs
- `_fmt_pct_of_budget` is defined *after* `_fmt_token_line` in `formatters.py`. Python resolves names at call time, so the forward reference works — but moving the helpers around naively could trip a `NameError` if someone flips the imports to `from … import _fmt_token_line` at module load. Keep both in the same module.
- `_render_token_panel` is invoked directly in tests with `SimpleNamespace` daemons — the new `agent_cfg` kwarg must stay optional (default `None`) or every panel test call site needs updating.

## Outcome
- Files touched: `agentor/dashboard/formatters.py`, `agentor/dashboard/render.py`, `tests/test_dashboard_formatters.py`, `tests/test_dashboard_render.py`, `docs/backlog/show-percentage-of-consumed-session-and.md` (deleted).
- Tests added: `TestFmtTokenLineBudget` (8 cases covering wide/mid/narrow × {zero-budget, half-budget, overbudget, narrow-width-fit}); `TestRenderTokenPanel::test_panel_shows_percent_suffix_when_budgets_set`; extended the no-budget panel test with a `%` absence assertion.
