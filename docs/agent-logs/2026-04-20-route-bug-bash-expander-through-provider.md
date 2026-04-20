# Route bug-bash note expander through configured provider — 2026-04-20

## Surprises
- `SimpleNamespace` test fixtures break `dataclasses.replace` (the new override path in `_new_issue_mode`). One added test had to switch to real `Config`/`AgentConfig` dataclasses.
- Test fakes mimicking `_expand_note` must accept the `timeout=` kwarg (or `**_kw`) — naming a positional `_timeout` is silently swallowed as a TypeError by `_new_issue_mode`'s try/except around `_run_with_progress`, which masked the fake's mis-signature as an empty `got_providers` list.

## Gotchas for future runs
- `_run_with_progress` swallows EVERY exception from the wrapped callable into a `_flash("expand failed: …")` path, so a bad test fake fails silently. Any future assertion on "was X called inside the progress wrapper?" should mock `_flash` to a list-appender so you can surface TypeErrors from fakes.
- `Codex exec` writes the "final message" to the `-o <path>` file; its stdout is noise. For ephemeral one-shot calls, skip `--json`/`-m` and read the file directly — much simpler than JSONL parsing.

## Follow-ups
- `StubProvider.invoke_one_shot` raises `NotImplementedError`. If a future test harness runs the dashboard under `runner="stub"` end-to-end, it will need to patch at `_expand_note` (same pattern the existing prompt tests use) or stub gets exercised and blows up.

## Outcome
- Files touched: `agentor/providers.py`, `agentor/dashboard/modes.py`, `tests/test_providers.py`, `tests/test_dashboard_prompt.py`, `docs/backlog/route-bug-bash-expander-through-configured-provider.md` (removed).
- Tests added/adjusted: `TestInvokeOneShot` (11 cases in `tests/test_providers.py`); renamed `_expand_note_via_claude` patches in `tests/test_dashboard_prompt.py::TestNewIssueNoteIsMultiline`; new `test_new_issue_mode_routes_through_configured_provider` and `test_new_issue_mode_honours_provider_override` in the same class.
- Full suite: `python3 -m unittest discover tests` — 676 passed.
