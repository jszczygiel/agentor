# Default main view to working/queued/active — 2026-04-20

## Surprises
- Pre-existing `FILTERS[0]` labelled itself `"all"` but actually excluded APPROVED. Swapped for a genuine sentinel `("all", None)` that falls through the `list(ItemStatus)` branch at `render.py:212`.
- Source backlog markdown `docs/backlog/default-main-view-to-working-queued-back.md` never existed in the worktree (untracked at dispatch) — `git rm` skipped per the CLAUDE.md best-effort rule.

## Gotchas for future runs
- `_render` needs `daemon.workers` (set) and `daemon.stats.completed` in addition to `system_alert`/`started_at`. Existing `TestRenderStatusLineTokenIndicator._render_once` is the template — copy its fake stdscr + SimpleNamespace wiring verbatim when adding new `_render`-driven tests.
- Remember `_token_windows_invalidate()` in every `_render` test setup, else a prior test's cached totals mask the seed.

## Outcome
- Files touched: `agentor/dashboard/render.py`, `tests/test_dashboard_render.py`, `docs/agent-logs/2026-04-20-default-main-view-active.md`.
- Tests added: `TestDefaultFilter` (4 cases: default-is-active, all-covers-every-status, terminal-hidden-by-default, all-reveals-hidden).
- Follow-ups: none.
