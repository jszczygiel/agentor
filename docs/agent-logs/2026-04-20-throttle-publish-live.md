# Throttle `_publish_live` — coalesce result_json writes — 2026-04-20

## Surprises
- Backlog source markdown already absent at dispatch — no `git rm` to perform. Noted in commit body per CLAUDE.md gotchas guidance.

## Gotchas for future runs
- Codex JSONL has no single canonical terminal event reachable inside `on_event` (the `result`-shaped fields land as plain keys on any message event, not a dedicated type). Belt-and-suspenders final publish sits in `_invoke_codex_jsonl` after `_run_stream_json_subprocess` returns — that's the only reliable "the stream is done" signal for Codex.

## Outcome
- Files touched: `agentor/runner.py`, `tests/test_runner.py`, `docs/agent-logs/2026-04-20-throttle-publish-live.md`.
- Tests added: `TestPublishLiveThrottle` (4 cases) in `tests/test_runner.py` — claude throttle, claude final-bypass, codex throttle+final, cursor-advance-prevents-reopen.
