---
title: Declare provider capabilities (mid-run inject, context window, result source)
state: available
tags: [refactor, multi-provider]
---

Provider differences are currently encoded inline in runner subclasses
as branching on behaviour instead of declared capability. Collect the
flags into a `ProviderCapabilities` dataclass consulted by the base
runner:

- `supports_mid_run_injection: bool` — Claude yes (stream-json stdin,
  see `_invoke_claude_streaming:1064`), Codex no (observer-only
  emitter, see comment at `_invoke_codex_jsonl:1297`).
- `reports_context_window: bool` — Claude yes (`modelUsage[m].contextWindow`
  in `_StreamState:1483`), Codex no (empty `modelUsage`).
- `reports_output_tokens_per_turn: bool` — Claude yes (gates token
  checkpoint emitter), Codex no (threshold dormant,
  `:1301`).
- `result_source: Literal["stdout_json", "output_file"]` — Claude via
  `_extract_result_field`, Codex via `--output-path` + `_read_output_message`.
- `requires_explicit_session_arg: bool` / `resume_arg_name: str` —
  Claude `--resume <id>` vs Codex subcommand `codex exec resume <id>`.

The checkpoint emitter in `checkpoint.py` / `runner.py:1062,:1312`
branches on injection support implicitly by passing/not-passing a
`stdin_holder`. Route through the capability flag instead so the next
provider declares it once.

Dashboard formatters (`dashboard/formatters.py:118+`) can read these
flags to decide whether to render context-window % or `—`.
