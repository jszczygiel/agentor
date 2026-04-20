# Abstract dead-session detection onto Provider — 2026-04-20

## Surprises
- `agent.session_max_age_hours` was already renamed (no longer `claude_session_max_age_hours`) in the current tree — backlog description predated that commit. Only the generic-knob doc-comment needed updating.
- `runner._is_dead_session_error` already carried Codex needles (`thread not found`, `thread/start failed`, `session not found`) as a union. Kept it as a retry-wrapper-only disqualifier; Recovery and the `runner.run()` session-kill demote now route through the active `Provider` instead, so a Claude item never trips on a Codex signature and vice-versa.

## Gotchas for future runs
- `tests/test_recovery.py::_mk_config` previously built an `AgentConfig()` with the default `runner="stub"` — once `StubProvider.session_max_age_hours()` returns `None`, every test that depends on the age gate or the Claude dead-session needle silently stops demoting. Bumped the helper to `runner="claude"` by default; stub-specific behaviour now lives in its own test class.
- New providers module imports only `Config` via `TYPE_CHECKING` — safe to import from `runner` and `recovery` without cycle risk. Don't promote the import to runtime without first confirming `config.py` never grows a reverse dep on providers/runner.

## Follow-ups
- `dashboard/render.py` still references `cfg.agent.runner` in strips/headers but has no provider-aware branches; if a future provider's status/activity display diverges from the current "runner name" label, add a `Provider.display_label()` rather than re-doing string branching at the render site.

## Outcome
- Files touched: `agentor/providers.py` (new), `agentor/runner.py`, `agentor/recovery.py`, `agentor/config.py`, `tests/test_recovery.py`, `tests/test_runner.py`.
- Tests added/adjusted:
  - `TestProviders` in `tests/test_runner.py` — Claude/Codex/Stub predicate + `session_max_age_hours` + factory + runner wiring.
  - `TestRecoveryStaleSessionCodex` and `TestRecoveryStubProviderOptsOut` in `tests/test_recovery.py`.
  - Extended `TestErrorClassifiers.test_dead_session_classifier` with Codex needles.
- Full suite: `python3 -m unittest discover tests` → 625 passed.
