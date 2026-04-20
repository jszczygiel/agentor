# Per-provider model alias tables — 2026-04-20

## Surprises
- `_parse_execute_tier(plan)` (no whitelist) previously defaulted to Claude's `haiku|sonnet|opus`. Had to relax the default to pass-through and push the whitelist requirement to `_resolve_execute_tier`; one test (`test_rejects_unlisted_alias`) needed an explicit whitelist to keep asserting the gate.
- Mypy's existing 5 errors in `runner.py` (`_last_execute_model` None-to-str + `_claude_initial_stdin_payload` arg-type) sit on `main` unchanged — confirmed via `git stash`.

## Gotchas for future runs
- `StubProvider.model_aliases = dict(ClaudeProvider.model_aliases)` is deliberate test ergonomics: `AgentConfig.runner` default is "stub", so tests that expect `@model:haiku` to resolve without pinning `runner="claude"` silently rely on this mirror. If a future refactor makes the stub's alias map empty, every `TestResolveExecuteTier` case that uses `_mk_cfg` without `runner=` breaks.
- `agent.execute_model_whitelist = []` is a sentinel for "full active-provider map", not a hard gate — `_resolve_execute_tier` substitutes `list(provider.model_aliases.keys())` when the config list is empty. Keep this in mind when adding a new provider with no aliases declared: the default path then disables the `@model:` channel entirely (by design — the base class ships `{}`).

## Follow-ups
- Codex's concrete `model_aliases = {"mini": "gpt-5.4-mini", "full": "gpt-5.4"}` still needs periodic verification against current Codex CLI availability. The ids rotate with OpenAI releases and should eventually be codified with the same shape-regex test Claude gets.
- `_last_execute_model` / `_last_execute_model_source` are typed as `None` at `__init__` then reassigned `str` later — the pre-existing mypy errors would clear with a `str | None` annotation. Out of scope here; worth a dedicated ticket.

## Outcome
- Files touched: `agentor/providers.py`, `agentor/config.py`, `agentor/runner.py`, `tests/test_runner.py`, `tests/test_providers.py` (new), `CLAUDE.md`.
- Tests added: `tests/test_providers.py` (`TestProviderModelAliases`, `TestModelToAlias`, `TestMakeProvider`); `tests/test_runner.py::TestResolveExecuteTier::{test_codex_rejects_claude_alias_tag, test_codex_plan_nomination_scoped_to_codex_aliases, test_empty_whitelist_falls_through_to_provider_map}`; `tests/test_runner.py::TestAliasMapShape::{test_codex_aliases_shape, test_codex_aliases_disjoint_from_claude}`. Full suite: 662 tests pass.
