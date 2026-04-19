# Force-close stdin watchdog after result event — 2026-04-19

## Surprises
- None — plan matched reality. `stdin_holder.close()` was already idempotent via its internal lock + `_closed` flag, so the watchdog firing alongside the outer `finally` path needed no extra guard.

## Gotchas for future runs
- Fake CLIs in `TestRunStreamJsonSubprocess` use bash; `read` returns non-zero on EOF. Use `if read var; then …; else …; fi` to make the shell keep running after the watchdog closes stdin — a bare `read injected` would exit the script early and mask whether the drift path actually unblocked it.
- Module-level constants in `agentor/runner.py` consumed inside a closure must be read through `runner_mod._NAME` at timer-arm time, not captured as a local — else a test monkeypatching the constant never takes effect. Current impl reads it directly when arming the `Timer(...)`, so monkeypatching works.
