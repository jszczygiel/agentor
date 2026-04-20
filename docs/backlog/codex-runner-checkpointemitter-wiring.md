---
title: Wire CheckpointEmitter into CodexRunner
state: available
category: feature
---

`CheckpointEmitter` lives in `agentor/runner.py` and is wired into
`ClaudeRunner` to inject mid-run turn-budget checkpoint prompts. Codex
uses its own JSONL event shape and `thread_id` resume semantics, so the
emitter is not yet hooked in. Codex JSONL lacks per-turn
`output_tokens`, so the minimum-viable gate is turn-count-only — the
emitter module is already agnostic enough that the Codex side just needs
a new `_CodexStreamState`-equivalent that counts turns.

Scope: gate behind the existing `agent.checkpoint_*` config knobs; no
new configuration surface. Add coverage in `tests/test_runner.py`
paralleling the Claude checkpoint tests (fake codex CLI emitting JSONL
turn events, assert prompt injected at the configured turn).

Defer note: author flagged this as "out of scope" until codex is run at
scale; no current operator uses `agent.runner = "codex"` in production,
so this sits low priority until a real signal emerges.

Source: retired `docs/IMPROVEMENTS.md` (Open) and
`docs/agent-logs/2026-04-19-inject-turn-budget-checkpoints.md`
(Follow-ups).
