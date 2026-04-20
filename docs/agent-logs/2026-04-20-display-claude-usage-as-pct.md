# Display claude.ai/settings/usage as % — 2026-04-20

## Surprises
- Initial plan added a keybinding to open the URL in a browser; reviewer rejected and asked for the existing token panel to mirror the Anthropic page's % display instead. Pivoted to reframing the panel windows themselves rather than handing the operator off to claude.ai.

## Gotchas for future runs
- `_token_windows` cache key includes `daemon_started_at` purely for cache invalidation when the daemon restarts; the value no longer drives windowing semantics now that both windows are rolling against `now()`. Don't reintroduce a `since=daemon_started_at` branch — it would diverge from claude.ai/settings/usage's rolling-window semantics.
- `session_token_budget` config name is misleading post-refactor: it caps the rolling 5h window, not a "session". Renaming was tempting but breaks existing `agentor.toml` files; settled for a docstring clarification at `agentor/config.py:216-228`.

## Outcome
- Files touched: `agentor/dashboard/formatters.py`, `agentor/config.py`, `tests/test_dashboard_formatters.py`, `tests/test_dashboard_render.py`, `docs/backlog/display-claude-usage-page.md` (deleted), `docs/agent-logs/2026-04-20-display-claude-usage-as-pct.md`.
- Tests added/adjusted: `TestFmtTokenRow` and `TestFmtTokenCompact*` rewritten for `5h`/`wk` cell shape with leading `NN%`; new `TestTokenWindowsCache.test_5h_since_threshold_is_rolling` pins the `since=now-5*3600` boundary; `TestTokenWindows.test_windows_are_rolling_not_session_anchored` proves `daemon_started_at` no longer gates the 5h window; `TestRenderTokenRow` and `TestRenderStatusLineTokenIndicator` updated to assert the new `usage` / `tok 5h=… wk=…` substrings.
- Follow-ups: none in scope; renaming `session_token_budget` → `usage_5h_token_budget` would be cleaner but breaks config back-compat — left for a deliberate migration if/when other config-key renames bundle.
