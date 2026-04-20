---
title: Allow operator to nominate model for the plan phase
state: available
category: feature
---

Execute phase already has a three-tier resolution chain in `runner._resolve_execute_tier` (item `@model:<alias>` tag > plan's `## Execute tier` trailer > `agent.model`), stashed on `result_json["execute_model"]` / `result_json["execute_model_source"]` for post-hoc attribution. The plan phase has no equivalent — `_do_plan` (ClaudeRunner and CodexRunner both) always dispatches on `agent.model`, so an operator who wants "plan on opus, execute on sonnet" or vice versa has no channel to express that at the idea-review stage (the operator's review of the backlog item before it's claimed).

Add a symmetric plan-tier resolver:

- **Config**: new `agent.plan_model: str | None = None` in `agentor/config.py` (empty/unset → falls back to `agent.model`). New `agent.plan_model_whitelist: list[str] = []` for parity with the execute-side knob (empty → active provider's full `model_aliases` map).
- **Tag**: new item-level `@plan_model:<alias>` tag parsed out of the backlog markdown by the existing `@key:value` machinery in `agentor/extract.py`. Keep `@model:` execute-only — don't overload it. Both may coexist on one item (`@plan_model: opus`, `@model: haiku`).
- **Resolver**: new `_resolve_plan_tier(config, provider, item) -> (alias, source)` in `agentor/runner.py`. Precedence (first match wins): `@plan_model:<alias>` tag > `agent.plan_model` > `agent.model`. No self-nomination possible (plan hasn't run yet — nothing upstream to parse a trailer out of). Whitelist-gate the tag same as execute: typo/unknown alias logs a soft warning and falls through, never raises. Per-provider vocab via `provider.model_aliases` — `@plan_model: haiku` on a Codex-routed item falls through with a warning (Codex ships `mini/full`).
- **Dispatch wiring**: `_do_plan` in both `ClaudeRunner` and `CodexRunner` (`agentor/runner.py:898`, `agentor/runner.py:1301`) calls the resolver, stashes `self._last_plan_model` / `self._last_plan_model_source`, and passes `model_override=self.provider.model_aliases.get(alias)` into `_invoke_claude` / `_invoke_codex` (both already accept the kwarg). Custom `agent.command` templates that drop `{model}` silently skip the override — same pattern as the execute side.
- **Attribution**: surface `result_json["plan_model"]` / `result_json["plan_model_source"]` on the AWAITING_PLAN_REVIEW transition, analogous to the execute-side fields on AWAITING_REVIEW. `tools/analyze_transcripts.py` already groups by model id, but an alias+source pair lets operators grep the plan/execute tier split directly.

Constraints:

- `agent.single_phase = true` skips plan entirely, so the new knob has no effect there — keep it silent, no warning.
- `StubRunner` has no model concept; leave the attrs unset on it (matches the existing execute-side behaviour — `StubRunner` doesn't populate `_last_execute_model` either).
- Legacy config files without `agent.plan_model` must keep working — default `None` falls through to `agent.model`, no migration needed.
- The three status-column enum heal rule (`_migrate` in `store.py`) doesn't apply here: no enum members added/removed.

Tests to add (mirror `tests/test_runner.py::TestResolveExecuteTier`):

- Tag beats `agent.plan_model`; `agent.plan_model` beats `agent.model`.
- Invalid alias on tag falls through with a warning, source becomes `"default"` (or `"config"` if `agent.plan_model` is set).
- Case-normalised tag (`@plan_model: Opus` → `opus`).
- Per-provider: Codex rejects a Claude alias tag; Claude's whitelist is `haiku/sonnet/opus`; empty whitelist means "all aliases for this provider".
- Dispatch smoke test: `_do_plan` on a stub Claude runner with `@plan_model: haiku` passes `claude-haiku-*` via `model_override` to `_invoke_claude`.

Out of scope (possible follow-ups):

- Dashboard inspect overlay showing the resolved plan-tier pair alongside the existing execute-tier display (see the sibling `show-suggested-execute-model-in-inspect` backlog item — extend it to include plan once this lands).
- Letting an earlier-round plan nominate a DIFFERENT plan model on re-plan after rejection (would need plan-text parsing + a `## Plan tier` trailer — punt until someone actually asks).
