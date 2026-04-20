# Allow operator to nominate model for the plan phase — 2026-04-20

## Gotchas for future runs
- `TAG_RE = r"@(\w+):(\S+)"` — `\w` includes underscore, so `@plan_model:opus` parses to tag key `plan_model` automatically. No changes to `extract.py` needed.
- `AgentConfig` loaded via `**_filter_known(...)` in `config.py` — new fields with defaults just work; no migration needed.
- Dispatch smoke test needs `ClaudeRunner` subclass overriding `_invoke_claude`; no mock library or subprocess is required. Pattern: capture kwarg, return fake `_last_usage`.

## Follow-ups
- Dashboard inspect overlay: show resolved `plan_model`/`plan_model_source` alongside existing execute-tier display (sibling backlog item: `show-suggested-execute-model-in-inspect`).
- `## Plan tier` trailer in plan text to re-nominate a different plan model on re-plan after rejection (deferred by task spec).

## Outcome
- Files touched: `agentor/config.py`, `agentor/runner.py`, `tests/test_runner.py`.
- Tests added: `TestResolvePlanTier` (10 cases), `TestPlanTierDispatchSmoke` (1), `TestResultJsonRecordsPlanModel` (3) — all in `tests/test_runner.py`.
