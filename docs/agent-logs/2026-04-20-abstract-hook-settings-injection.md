# Abstract Read/Grep hook injection — 2026-04-20

## Surprises
- `agent.command.format(**guardrails)` is the natural splice point — no churn at callsites that don't need a placeholder, since `.format` ignores unused kwargs. Codex command template picks up the new `{}` cleanly.

## Gotchas for future runs
- New providers adding `write_tool_guardrails` MUST namespace their placeholder key (Claude reserves `settings_path`). Collisions would silently swap meanings between Claude and Codex command templates.
- `Daemon.run()` startup warning uses a throwaway runner built via `runner_factory(config, store)` — NOT `_make_runner()` — so `proc_registry` / `stop_event` / provider-override plumbing stays unattached. A future refactor that moves essential setup into `Runner.__init__` would silently trip this throwaway instance.

## Follow-ups
- The warning phrasing assumes "non-default" means "operator tuned" — setting `enforce_grep_head_limit=False` (explicit opt-out) also triggers the warning. Harmless, but could be tightened to "only when a guardrail would have enforced" if noise becomes an issue.

## Outcome
- Files touched: agentor/runner.py, agentor/daemon.py, tests/test_runner.py, tests/test_daemon.py, docs/backlog/abstract-hook-settings-injection.md (removed).
- Tests added: `TestGuardrailAbstraction` (8 cases) in tests/test_runner.py; `TestStartupGuardrailWarning` (2 cases) in tests/test_daemon.py.
- Follow-ups: see above.
