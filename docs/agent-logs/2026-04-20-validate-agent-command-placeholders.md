# Validate agent.command placeholders per provider — 2026-04-20

## Surprises
- Claude's `default_command()` doesn't include `{prompt}` (stream-json stdin path), so the default template itself triggers one "missing optional" soft warning. Kept the behaviour and asserted it in tests — the warning correctly signals "the legacy `-p {prompt}` opt-in isn't in use."

## Gotchas for future runs
- `providers.py` imports `config` only as a `TYPE_CHECKING` forward ref, but `config.py` must do the validator import inside `__post_init__` (not at module top) to avoid the reverse cycle — `Config.__post_init__` calls `validate_agent_command`, and `providers` already references `Config`.
- Hard-deleting `_default_claude_command` / `_default_codex_command` / `_default_codex_resume_command` from `runner.py` changes the public import surface — any test that pulled them directly has to be updated. `test_runner.py` had two usages.

## Follow-ups
- None — scope was exactly the backlog item.

## Stop if
- A new provider is added but forgets to declare `command_placeholders` / `resume_command_placeholders`. The base-class `PlaceholderSchema()` default is empty, so the validator will reject every placeholder as foreign — that's the intended "fail loud" behaviour but can look confusing. Populate both ClassVars on the subclass.

## Outcome
- Files touched: `agentor/providers.py`, `agentor/config.py`, `agentor/runner.py`, `tests/test_runner.py`, `tests/test_command_placeholders.py` (new), `docs/backlog/validate-agent-command-placeholders.md` (deleted).
- Tests added/adjusted: `tests/test_command_placeholders.py` — 27 cases across `TestPerProviderSchemas`, `TestValidatorHardErrors`, `TestValidatorSoftWarnings`, `TestConfigPostInit`, `TestLoaderEmitsWarnings`, `TestDefaultCommandsOnProviders`, `TestPlaceholderSchemaDataclass`. Two existing assertions in `tests/test_runner.py::TestPreToolUseHookWiring` updated to call `ClaudeProvider.default_command()` after the static-method move.
- Follow-ups: none.
