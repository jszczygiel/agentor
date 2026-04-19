# Shared stream-json subprocess base — 2026-04-17

## Surprises
- Claude and Codex streaming paths had different transcript headers (Claude: `args:\nstdout:`; Codex: `args:\n\nstdout:`). Standardizing on the Codex form was safe — only `"type":"assistant"` and `"exit: 0"` are asserted in `test_claude_runner_stream_json_live_updates`.
- `_StreamState` vs `_CodexStreamState` stay as siblings on purpose: per-model token accounting vs thread-id + final-message only. Merging would over-couple unrelated provider envelopes. Plan already called this out.

## Gotchas for future runs
- Codex mid-stream session_id persistence requires re-`store.get(item.id)` after `transition`. Implemented as a mutable `[item]` cell captured by the `on_event` closure so the shared helper stays provider-agnostic.
- Shell-script fake CLIs that end with `sleep N` may or may not `exec` sleep depending on the shell. If sleep stays as a child of the shell, `p.kill()` orphans it and the stderr drain thread blocks reading until sleep exits — we already cap this with `stderr_thread.join(timeout=2)` and `p.stdout.close()` in the helper's finally.

## Follow-ups
- None — `_invoke_claude_blocking` retains a near-duplicate subprocess lifecycle, but it's the non-streaming fallback with a different shape (no line loop, no live publish, `communicate()` instead of per-line reads). Folding it in would cost more than it saves; left out of scope.
