---
title: Throttle `_publish_live` — coalesce result_json writes to ≤1/sec
state: available
tags: [perf, runner, sqlite]
---

`ClaudeRunner._publish_live` (`agentor/runner.py:1206`) fires on every
`assistant` and `result` stream-json event (`runner.py:1144`). Each call:

1. `json.dumps(envelope)` — envelope contains `iterations` which grows
   per turn, so the serialized blob gets longer every event.
2. `Store.update_result_json` — SQLite UPDATE, which in WAL mode appends
   a page and (eventually) fsyncs.

During active agent work this fires many times per second per worker,
and the dashboard only samples it every 500ms anyway. CodexRunner's
`_publish_live` (`runner.py:1463`) has the same pattern via
`runner.py:1427`.

Fix: add a `_last_publish_ns` attribute on `_StreamState` /
`_CodexStreamState` and short-circuit `_publish_live` when
`time.monotonic_ns() - last < 1_000_000_000`. Always publish on the
terminal `result` event so the final envelope lands regardless.

Keep it simple — no background thread, no batching queue. One instance
attribute + one guard.

Validate: existing `tests/test_claude_runner_streaming.py` tests (if
any) need updating to account for the throttle — use a zero threshold
in tests or patch `_PUBLISH_INTERVAL_S` at class level.
