# Simplify token panel to a single compact line — 2026-04-20

## Surprises
- Status-line `tok sess=… wk=…` indicator and the new one-liner coexist intentionally — the status tail is orthogonal to the panel (wide tier only), so the backlog's scope bite stayed on panel rows rather than deduping everything.

## Gotchas for future runs
- `_FakeAgentCfg` used to be defined mid-file in `tests/test_dashboard_formatters.py` (after `TestFmtTokenCompact`); moving it above `TestFmtTokenRow` avoids a forward-reference crash. If you add more tests before `TestFmtTokenCompactPct`, keep the helper at or above their first use.
- Narrow-tier token row is size-budgeted: `tok s=X t=Y w=Z` + two `(NN%)` suffixes must fit <50 cols. The pinned `test_narrow_fits_50_cols_with_m_scale_totals` catches it.

## Outcome
- Files touched: `agentor/dashboard/formatters.py`, `agentor/dashboard/render.py`, `tests/test_dashboard_formatters.py`, `tests/test_dashboard_render.py`, `CLAUDE.md`, `docs/backlog/simplify-token-panel-to-single-line.md` (deleted).
- Tests added: `TestFmtTokenRow` (7 cases) in `test_dashboard_formatters.py`; `TestRenderTokenRow` (4 cases) in `test_dashboard_render.py`.
- Tests removed: `TestFmtTokenLine`, `TestFmtTokenLineTiers` (formatters gone); `TestRenderTokenPanel` → replaced by `TestRenderTokenRow`.
- Net row reclaim: 4 rows → 1 row for the main table; ~3 extra items visible per refresh.
