# Remove id-search/unpause from dashboard footer — 2026-04-17

## Surprises
- Backlog referenced `agentor/dashboard.py:19-20` and `[i]d-search`, but the
  module is now the `agentor/dashboard/` package and the footer never contained
  `[i]d-search` — only `[i]nspect`. Treated the `[i]d-search` clause as stale
  and only removed `[u]npause`.
- The `u` handler and `daemon.clear_alert()` stay — the sticky PAUSED banner
  in `render.py` still prompts `(press [u] to resume)`, so the key is still
  the only dismissal path even though the footer no longer advertises it.

## Gotchas for future runs
- When a backlog item cites a file path, check whether the target has been
  restructured before trusting the line numbers.

## Follow-ups
- `tests/test_config.py` has three pre-existing F401 unused-import errors
  (logged to `docs/IMPROVEMENTS.md`). CI presumably fails on those already.
