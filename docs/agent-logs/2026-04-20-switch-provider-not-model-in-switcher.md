# Switch provider (not model) in [M] switcher overlay — 2026-04-20

## Surprises
- `approve_plan` routes AWAITING_PLAN_REVIEW items back through QUEUED, which means resumed executes land in `_make_runner` fresh — mid-session provider flip re-targets them automatically without extra plumbing.
- `_model_override_fresh` plumbing only ever shipped as dashboard-writable; removing both the dashboard path and the runner branches left a clean diff because nothing else in the codebase wrote to the attribute.

## Gotchas for future runs
- `dataclasses.replace(cfg, agent=replace(cfg.agent, runner=override))` is the right shape to shadow a nested field without mutating the Config other threads read. Mutating `cfg.agent.runner` directly would race the dashboard's status-line reads.
- Renaming a dashboard render-layer primitive (`_prompt_model_switcher` → `_prompt_provider_switcher`) requires touching both the overlay AND its mode-layer caller in modes.py AND the `__init__.py` keybinding dispatch — three files, not two.

## Follow-ups
- Codex runner currently shares `agent.model` with Claude; if operators toggle to codex via the switcher with a claude-shaped model id set, the codex subprocess will happily pass the claude model name through. Per-provider model defaults (or a hint when the model id looks foreign) is a future backlog entry.
- `_make_runner` allocates a fresh replaced Config per dispatch; trivial cost, but if pool sizes scale into the dozens a memoised shadow config keyed by override kind would be cheap.

## Outcome
- Files touched: agentor/config.py, agentor/daemon.py, agentor/runner.py, agentor/dashboard/render.py, agentor/dashboard/modes.py, agentor/dashboard/__init__.py (plus CLAUDE.md, tests).
- Tests added/adjusted: `tests/test_dashboard_provider_switcher.py` (rewritten — 5 cases covering pick/cancel/clear/current-passthrough/stub-excluded), `tests/test_dashboard_render.py::TestProviderSwitcherOverlay` (5 cases: preselected/nav/esc/clear/configured-tag/empty), `tests/test_runner.py::TestDaemonProviderOverrideThreading` (4 cases: override honoured, shared config not mutated, mid-flight isolation, @model tag regression).
- Follow-ups: see above.
