# Declare provider capabilities — 2026-04-20

## Surprises
- `agentor/providers.py` already existed as an orthogonal abstraction (dead-session substrings + session-age gate). The new `agentor/capabilities.py` was kept distinct — different concern, different consumers — rather than piled onto `Provider`. Sibling ticket `extract-provider-interface-from-runners.md` is the right place to unify them.

## Gotchas for future runs
- When touching `_ctx_fill_pct`, remember it is called from `_render_table` at 500ms tick. Capability lookup is hoisted once per render (in `_render` at the `_render_table` callsite), not per row.
- `resume_arg_name` / `requires_explicit_session_arg` on `ProviderCapabilities` are declared but NOT yet consumed by command construction — Codex uses a separate `resume_command` template, so collapsing argv building behind a single flag-name is out of scope until the `Provider` interface refactor lands.

## Follow-ups
- The `Provider` in `agentor/providers.py` and `ProviderCapabilities` in `agentor/capabilities.py` are two slices of the same concept (per-CLI behaviour). Sibling ticket `docs/backlog/extract-provider-interface-from-runners.md` proposes consolidating them; this ticket intentionally did not.

## Outcome
- Files touched: agentor/capabilities.py (new), agentor/runner.py, agentor/dashboard/formatters.py, agentor/dashboard/render.py, tests/test_capabilities.py (new), tests/test_runner.py, tests/test_dashboard_formatters.py.
- Tests added/adjusted: new tests/test_capabilities.py (11 cases — per-provider flag pins, dispatch, unknown-raises, runner-binding); tests/test_runner.py::TestClaudeRunnerCheckpointInjection::test_capability_flag_gates_injection (new); tests/test_dashboard_formatters.py::TestCtxFillPct::test_codex_caps_short_circuits_to_emdash (new).
- Follow-ups: unify `Provider` (dead-session / age gate) with `ProviderCapabilities` (static flags) via the sibling interface refactor ticket.
