# Integration smoke for ClaudeRunner against fake claude CLI — 2026-04-19

## Surprises
- `_invoke_claude` derives the transcript filename from `had_session` (session presence), not from the `_do_plan` vs `_do_execute` split. So in `single_phase=True` mode, the first run's log is `<id>.plan.log` even though `result_json["phase"] == "execute"`. Test had to assert against `.plan.log`, not `.execute.log`.

## Gotchas for future runs
- When unit-asserting against transcript paths, always derive from `_invoke_claude`'s `phase_tag = "execute" if had_session else "plan"` — not from the semantic plan/execute boundary.
- `/bin/sh` `read foo` returns non-zero on EOF; fake CLI scripts that block waiting for the runner to inject mid-run stdin must use `read foo || true` so the script still exits 0 once the runner closes stdin (otherwise transcript footer is `exit: 1` and tests asserting `exit: 0` flake).
- Manual revert of `runner.py:1020-1021` against the new regression test produced an 8.2s wall-clock vs 5.0s budget — confirms the timing assertion catches the bug with comfortable headroom on local hardware. CI on a busier host should still have margin.

## Follow-ups
- None in scope; the negative case relies on `time.monotonic()` wall-clock. If this gets flaky on shared CI, consider gating with an env var (`AGENTOR_SLOW_CI=1`) that loosens the budget — but defer until a real flake is observed.
