# Let the plan phase choose the execute-phase model tier — 2026-04-20

## Surprises
- `_default_claude_command()` formatted `{model}` but never passed `--model` to claude CLI — the `agent.model` config value was silently unused on the Claude runner path until now. Fixed by adding `--model {model}` to the default argv.

## Gotchas for future runs
- Runner-subclass state passed to `Runner.run` via `self._last_*` attributes is the existing pattern (same mechanism as `_last_phase`, `_last_usage`, `_last_questions`). When adding a new per-run field, set it in BOTH `_do_plan` (often to `None`) and `_do_execute`; forgetting the plan-side reset leaks the previous execute's value into the next plan cycle on the same runner instance.
- `_invoke_claude`'s `args = [a.format(...) for a in template]` silently ignores any `{placeholder}` that the template doesn't reference. Same for `_codex_args`. That's the "opt-out" pattern for `{settings_path}` and now `{model}`: a custom `agent.command` that drops the placeholder gets no per-invocation override — no error, just the global default.

## Follow-ups
- Dashboard indicator: no UI surface for the nominated tier yet. A future backlog item could show the plan's chosen tier in the review screen so the operator sees "this one will run on Haiku" before approving.
- `tools/analyze_transcripts.py` can now join transcripts against `result_json["execute_model_source"]` to measure actual savings — a follow-up could surface fallback-rate and by-tier cost rollups.
- Consider a `test_aliases_cover_all_current_claude_families` shape check every time Anthropic ships a new family.

## Outcome
- Files touched: `agentor/config.py`, `agentor/runner.py`, `CLAUDE.md`, `tests/test_runner.py`, `docs/backlog/let-plan-choose-execute-model.md` (deleted).
- Tests added/adjusted: `TestParseExecuteTier` (8 cases), `TestResolveExecuteTier` (7 cases), `TestAliasMapShape` (1 case), `TestResultJsonRecordsExecuteModel` (2 cases), `TestPlanPromptIncludesExecuteTierSection` (1 case) — all in `tests/test_runner.py`. Full suite: 591 tests passing.
- Follow-ups: see above.
