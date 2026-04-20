# Add model switcher window to dashboard — 2026-04-20

## Surprises
- `_invoke_claude` / `_codex_args` already accepted `model_override` from the
  prior "let plan nominate execute tier" feature, so the runtime dashboard
  override plumbs through the same kwarg rather than needing a second
  parallel path. The fresh-vs-resumable gate rides on `item.session_id`
  being unset at entry to `_do_execute`.
- Source backlog markdown (`docs/backlog/add-model-switcher-window-to-dashboard.md`)
  was absent at dispatch — likely a prior scan already promoted + removed it.
  No `git rm` needed; noted per CLAUDE.md's "Backlog source markdown may be
  absent at dispatch" rule.

## Gotchas for future runs
- New `[M]` (shift-M) global key — deliberately capital so it doesn't
  collide with the lowercase `m` that retries merges on CONFLICTED items
  in the inspect view. Keybinding-map audit in `__init__.py` + the
  `ACTIONS*` strings both needed updating (contract tests in
  `test_dashboard_render.TestActionsHint` pin the token set).
- `_MODEL_OVERRIDE_CLEAR` is a module-level sentinel returned by the
  overlay — None means cancel, the sentinel means "revert to
  `agent.model`". Callers that treat a None return as "clear the
  override" would silently wipe the operator's existing choice on every
  cancel keystroke.
- `Runner._model_override_fresh` is set on the instance by the Daemon in
  `_make_runner`, snapshot at dispatch time. A mid-flight flip of
  `daemon.model_override` does NOT retroactively re-target an
  already-handed-off runner — covered by
  `test_make_runner_snapshots_daemon_override`.

## Follow-ups
- `KNOWN_MODELS["codex"]` is currently `[]`. Populate when we have a
  stable list of codex model ids to expose to operators.
- Consider surfacing the override on the `result_json` so post-hoc cost
  attribution can tell a dashboard-flipped run from a plain
  `agent.model` run. `_last_execute_model_source = "dashboard"` is set,
  but the plan-phase path doesn't persist a similar marker (the plan
  phase doesn't record `execute_model_*` at all).

## Outcome
- Files touched: `agentor/config.py`, `agentor/daemon.py`, `agentor/runner.py`,
  `agentor/dashboard/render.py`, `agentor/dashboard/modes.py`,
  `agentor/dashboard/__init__.py` (+ 2 test files: `tests/test_dashboard_render.py`,
  `tests/test_dashboard_model_switcher.py`, and additions to `tests/test_runner.py`).
- Tests added/adjusted: `TestModelSwitcherOverlay` (4 cases) and
  `TestModelOverrideFooterStrip` (2) in `test_dashboard_render.py`;
  whole-file `test_dashboard_model_switcher.py` (5 cases);
  `TestDaemonModelOverrideThreading` (5 cases) in `test_runner.py`;
  extended `TestActionsHint.test_core_actions_present` for `[M]odel`.
- Follow-ups: populate `KNOWN_MODELS["codex"]`; record dashboard-source
  override on `result_json` for the plan phase.
