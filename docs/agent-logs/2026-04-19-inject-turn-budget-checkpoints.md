# Inject turn-budget checkpoint prompts mid-run — 2026-04-19

## Surprises
- Delivering mid-run nudges required switching the default `claude` invocation
  from `-p "{prompt}"` to `-p --input-format stream-json` with the prompt
  written as a framed `user` JSONL line to stdin. Kept a legacy branch for
  operators who still carry `{prompt}` in `agent.command` — they get dry-run
  observation markers but no actual injection.

## Gotchas for future runs
- `_run_stream_json_subprocess` now supports `stdin_payload` + `stdin_holder`.
  Writes from the stream-reader thread must go through the holder's lock so
  `p.kill()` on timeout can't race with a partial flush.
- Switching the default command changes what `.format(prompt=…, model=…)`
  substitutes — confirm new default args contain no stray `{…}` lest `.format`
  raise `KeyError`.

## Follow-ups
- Wire `CheckpointEmitter` into `CodexRunner`. Codex's JSONL lacks per-turn
  output_tokens, so the minimum-viable gate is turn-count-only. Logged in
  `docs/IMPROVEMENTS.md`.

## Stop if
- A future run finds that Claude's stream-json stdin doesn't honour
  mid-session user messages on the installed CLI version — disable injection
  via `agent.turn_checkpoint_soft=0` / `hard=0` / `output_token_checkpoint=0`
  in the project `agentor.toml` (all three → emitter isn't even constructed).
