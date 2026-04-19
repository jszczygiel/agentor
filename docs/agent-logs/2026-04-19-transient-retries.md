# Retry transient claude errors with exponential backoff — 2026-04-19

## Gotchas for future runs
- `_run_stream_json_subprocess` and `_invoke_claude_blocking` previously
  overwrote the transcript with `write_text`. Any wrapper that writes to
  the transcript across invocations (RETRY markers, staged headers) must
  clear once in the outer dispatch and have inner writers use append mode.
- `_codex_args` reads `item.session_id`, so a retry loop around codex must
  re-fetch the `StoredItem` before rebuilding args — otherwise a thread
  persisted mid-stream on attempt N-1 gets orphaned when attempt N starts
  a fresh session.
- Module-level `_sleep` for test-patchability: call via the module global
  (`runner._sleep(d)`) so tests can replace it with a no-op without
  touching `time.sleep`.

## Stop if
- Retry budget exhausts on a stream that already produced `system/init`
  and the next attempt errors with "session already exists" — that's a
  separate edge case the existing dead-session path doesn't cover.
