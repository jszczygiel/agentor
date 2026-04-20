---
title: Let the plan phase choose the execute-phase model tier
state: available
tags: [runner, token-economy, cost]
---

## Why

Every item currently runs plan+execute at the same model tier (whatever the operator configured globally, typically Opus). A lot of backlog items are mechanical: "rename X", "add a test for Y", "delete obsolete file Z". Paying Opus rates for execute when Haiku would trivially succeed burns money without improving quality. Conversely, some items look cheap in the title but reveal hidden complexity during plan — auto-downgrading blindly by heuristic misclassifies both directions.

The plan phase already produces the most informed view of an item: it has read the code, surveyed the edit surface, and drafted a strategy. Letting the plan *itself* nominate the execute-tier turns that context into a cost decision instead of discarding it.

A manual `@model:` inline tag escape hatch already composes naturally with this (same dispatch path), so operators stay in control when they disagree with the model.

## What

1. `AgentConfig` — two new fields:
   - `agent.auto_execute_model: bool = False` — master opt-in. Default off so existing installs are unchanged.
   - `agent.execute_model_whitelist: list[str] = ["haiku", "sonnet", "opus"]` — plan's suggestion is rejected (fallback to global default) unless the string is in this list. Prevents a prompt-injected plan from nominating a nonexistent or unauthorised model.

2. Plan prompt addendum — extend `AgentConfig.plan_prompt_template` (or the new `system_prompt_template` if `hoist-stable-instructions-into-system-prompt.md` has landed) to instruct the plan phase to end its output with a structured trailer:

   ```
   ## Execute tier

   suggested_model: haiku | sonnet | opus
   reason: <one sentence>
   ```

   Tier names are short aliases, not full model IDs, so the mapping from alias → current best model (`claude-haiku-4-5`, `claude-sonnet-4-6`, `claude-opus-4-7`) stays in `AgentConfig` and rotates with model releases without touching every historic plan.

3. `ClaudeRunner` — new helper `_parse_execute_tier(plan_text: str) -> str | None`:
   - Regex-extracts `suggested_model:\s*(haiku|sonnet|opus)` from the plan trailer.
   - Returns `None` if missing or malformed. Runner logs a soft warning (not an error) and falls through to the global default.
   - Validates against `execute_model_whitelist` — returns `None` on miss.

4. Dispatch flow in `_do_execute`:
   - `@model:` tag on the item wins unconditionally (operator override).
   - Else if `auto_execute_model=True` and the stored plan has a parseable suggestion: use that.
   - Else: global default (current behaviour).
   - Resolved tier → full model ID via a small `_ALIAS_TO_MODEL` dict on `AgentConfig`. Pass via `--model` to `claude -p` (and the Codex equivalent — verify flag name).

5. Persistence — record the resolved tier on the `result_json` payload so the dashboard / transcripts can attribute token spend by chosen tier later:

   ```python
   result_json["execute_model"] = resolved_tier  # "haiku" | "sonnet" | "opus"
   result_json["execute_model_source"] = source  # "tag" | "plan" | "default"
   ```

6. `CodexRunner` parity — mirror `_parse_execute_tier` and the dispatch branch. Codex `exec` takes `--model` too; confirm current codex CLI still accepts it.

7. `CLAUDE.md` — add a Design Invariant bullet: *"Plan can nominate execute tier via `suggested_model:` trailer. Resolution order at execute dispatch: item `@model:` tag > plan suggestion (when `agent.auto_execute_model=true`) > global default. Invalid/missing suggestion is a soft fallback, not an error."*

## Verification

- `tests/test_runner.py::test_parse_execute_tier_extracts_haiku` — canonical trailer parses.
- `tests/test_runner.py::test_parse_execute_tier_rejects_unlisted` — a plan suggesting `"gpt-4"` returns `None` (whitelist gate).
- `tests/test_runner.py::test_execute_dispatch_honours_tag_over_plan` — item with `@model:opus` tag + plan suggesting `haiku` → runner uses Opus.
- `tests/test_runner.py::test_execute_dispatch_falls_back_when_opt_in_off` — `auto_execute_model=False` → plan suggestion ignored even when present.
- `tests/test_runner.py::test_result_json_records_model_source` — `result_json["execute_model_source"]` is one of `{"tag", "plan", "default"}` after every execute invocation.
- Manual: run one trivial item (e.g. a docs typo fix) and one complex one (e.g. a migration) with `auto_execute_model=True`; inspect `result_json` and the claude stream-json transcript to confirm the tier and token spend match intent.

## Non-goals

- Not letting the plan choose its *own* tier — plan always runs at the configured default. (Pre-flight classifier is a separate backlog item if we ever want that.)
- Not implementing an escalation ladder (Haiku fails → retry Sonnet). Orthogonal. If we add one later it sits on top of this mechanism, not underneath.
- Not touching pricing telemetry. `tools/analyze_transcripts.py` already derives cost from transcript `usage` blocks — once `execute_model_source` lands on `result_json`, an operator can join the two offline to see actual savings without new infra.
- Not changing the dashboard. A future backlog item can surface the chosen tier in the inspect view; this one keeps scope tight.

## Scope flags

- Stub runner unaffected.
- `test_cmd` / `build_cmd` untouched.
- `agent.single_phase=true` items skip plan → no suggestion possible → always fall through to tag-or-default. Document this in the config comment so operators don't expect auto-tiering to work with single-phase.

## Risks

- **Plan-phase prompt injection** — if the item body contains an attacker-controlled string like `suggested_model: haiku`, a malicious ticket could force Haiku execute on a security-sensitive change. The whitelist limits damage to "valid model, wrong tier". Operator review gate still catches the actual diff before merge. Acceptable given the threat model (backlog files are already trusted input, we don't run anonymous submissions).
- **Plan output format drift** — if the plan ignores the trailer instruction, the fallback kicks in silently and we get no savings. Track the "fallback-to-default" rate in `result_json["execute_model_source"]`; if it stays high, rework the prompt. Don't hard-fail on missing trailer — half the legacy plans in `transitions` history would become un-resumable.
- **Model alias rot** — `_ALIAS_TO_MODEL` needs updating when Anthropic ships a new Sonnet/Opus. Low-frequency maintenance, but easy to forget. A test `test_aliases_map_to_valid_model_ids` that just asserts each value matches the `claude-(haiku|sonnet|opus)-\d+-\d+` shape catches typos.
- **Haiku plan-quality gap on borderline items** — this refactor doesn't change plan quality (plan still runs at default tier), but it does mean an overconfident plan can cheapify an execute that needed more horsepower. The review gate is the backstop; if rejection rate on auto-downgraded items climbs above baseline, flip `auto_execute_model` off and investigate.

## Open questions

- Does the dashboard need an indicator that an item's execute will run at a non-default tier before the operator approves the plan? Probably yes — surfaces the decision at review time. Defer to a follow-up item unless trivial to piggyback.
- Should `@model:` tag values be aliases (`haiku`) or full IDs (`claude-haiku-4-5`)? Aliases match the plan trailer and survive model rotation — lean alias, reject full IDs with a clear error.
