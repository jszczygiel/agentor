---
title: Integration smoke for ClaudeRunner against a fake claude CLI
state: available
category: test
---

Existing runner tests exercise pieces separately — the stream-json helper
in `TestRunStreamJsonSubprocess`, checkpoint injection in
`TestClaudeRunnerCheckpointInjection` (which stubs the helper), single-shot
invocation elsewhere. No test wires `ClaudeRunner.run` end-to-end against a
real subprocess that speaks stream-json. The 2026-04-19 hang
(`result` event emitted but stdin held open → deadlock) slipped through
because each slice was green in isolation.

Task: add at least one integration test that launches `ClaudeRunner.run`
against a `_write_fake_cli(...)` script mimicking the claude stream-json
protocol — emits `system`, one `assistant` block, and a `result` event
with `terminal_reason:"completed"` — and asserts the runner returns
cleanly (proc exits, transcript has `exit: 0` footer, store transitions to
AWAITING_PLAN_REVIEW for plan phase / AWAITING_REVIEW for single-phase).

Scope:

- Reuse `_write_fake_cli` and `_fake_claude_cli` helpers in
  `tests/test_runner.py`.
- Cover both the stream-json stdin path (new default) and the legacy
  `{prompt}` template path so regressions in either don't slip.
- Include a negative case: fake CLI emits `result` then sleeps reading
  stdin. With the current fix, test must return within
  `agent.timeout_seconds * 0.5`. Without the fix, it would have timed
  out → this is the explicit regression guard for the stdin-close bug.

Verification:

- New tests pass.
- `python3 -m unittest discover tests` stays green.

Source reflection: follow-up from the stdin-stays-open hang — protocol
wiring wasn't tested at the layer where the bug lived.
