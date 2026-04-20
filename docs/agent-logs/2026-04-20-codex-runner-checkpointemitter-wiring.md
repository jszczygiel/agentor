# Wire CheckpointEmitter into CodexRunner — 2026-04-20

## Surprises
- Codex CLI has no stream-json stdin mode (prompt is baked into argv at spawn),
  so mid-run *injection* is impossible. Implementation matches Claude's legacy-
  command fallback: emitter observes, `_write_checkpoint_marker` writes a
  `checkpoint-observed-dry-run` marker. Acceptance phrase "assert prompt
  injected at the configured turn" from the backlog was honored as "marker
  appears at the configured turn" — no real injection channel exists.

## Gotchas for future runs
- `_note_checkpoint` was a ClaudeRunner method taking a `_StreamState`; lifted
  to module-level `_write_checkpoint_marker(transcript_path, num_turns,
  output_tokens, nudge, injected)` so CodexRunner (using `_CodexStreamState`)
  can share it. Preserve the marker byte shape — Claude's
  `test_legacy_prompt_template_skips_injection` pins the substring exactly.
- `_CodexStreamState.ingest` increments `num_turns` on `turn.started`, so the
  emitter call must happen AFTER `state.ingest(ev)` to reflect the current
  turn (matches Claude's post-ingest observe order).
- Codex emitter always passes `output_tokens=0` — codex JSONL exposes no
  per-turn tokens. Tokens threshold stays dormant as long as operators keep
  `output_token_checkpoint > 0` (the emitter requires both `output_tokens >=
  cfg.output_tokens` AND `cfg.output_tokens > 0`, so a 0 current never fires
  an enabled threshold).

## Outcome
- Files touched: `agentor/runner.py`, `tests/test_runner.py`,
  `docs/backlog/codex-runner-checkpointemitter-wiring.md` (removed),
  `docs/agent-logs/2026-04-20-codex-runner-checkpointemitter-wiring.md`.
- Tests added: `TestCodexRunnerCheckpointObservation` in
  `tests/test_runner.py` (3 cases: soft-threshold marker, all-disabled skip,
  no-stdin-injection contract).
- Follow-ups: none.
