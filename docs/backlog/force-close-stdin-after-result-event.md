---
title: Force-close stdin watchdog after `result` event in stream-json helper
state: available
category: bug
---

Belt-and-suspenders for the class of bug that left 5 claude procs idle for
12 min on 2026-04-19. `ClaudeRunner._stream_claude_run.on_event` now closes
`stdin_holder` when it sees `type:"result"`, but `_run_stream_json_subprocess`
itself is still willing to block indefinitely on `p.stdout.readline()` if any
future caller forgets that closing step. Add a generic watchdog inside the
helper so a protocol change can't deadlock the daemon again.

Task: after `on_event` has observed a `type:"result"` event, if
`p.stdout.readline()` hasn't returned within N seconds (N small, e.g. 5-10s),
force-close `stdin_holder` and `p.stdin`, and log
`"closed stdin after result — protocol drift"`. If the proc still doesn't
exit within the existing timeout, let the timer-based kill fire as today.

Scope:

- `agentor/runner.py::_run_stream_json_subprocess` only. Caller-level
  `on_event` semantics stay the same.
- Threshold should be configurable via a module-level constant so a test can
  dial it down without touching `agent.timeout_seconds`.
- Emit the transcript line via the same append path as existing markers so
  operators can grep for the drift note.

Verification:

- New test under `TestRunStreamJsonSubprocess`: fake CLI emits
  `{"type":"result"}` then hangs reading stdin. Helper returns within
  `constant + slop` seconds without the caller having closed the holder.
- `python3 -m unittest discover tests` passes.

Source reflection: follow-up from the stdin-stays-open hang investigated in
this session — fix landed in `ClaudeRunner.on_event`, but the underlying
helper should fail safe on its own.
