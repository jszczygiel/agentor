---
title: Audit remaining sync subprocess calls on the curses thread
state: available
category: bug
---

`dashboard-stops-refreshing-appears-hung` fix only wrapped `diff_vs_base` in
`_run_with_progress`. The hang class — synchronous subprocess / filesystem
work on the main curses thread starving `getch` — applies to any other sync
git/subprocess/filesystem call in the modes or render layer.

Task: grep-audit `agentor/dashboard/` for synchronous work on the main
curses thread and either wrap it in `_run_with_progress` or justify leaving
it inline (cheap, bounded, O(1) per tick).

Scope:

- Grep targets: `subprocess.`, `git_ops.`, `.read_text(`, `.read_bytes(`,
  `Path(...).open(`, any `_invoke_*` or `runner.*` call.
- For each hit: confirm it runs on a background thread or wrap it.
- Hot-path budget (per the dashboard-hang gotcha promoted to CLAUDE.md): each
  dashboard tick must be O(1). No full transcript reads; no
  `transitions_for` scans in the render loop.

Verification:

- Audit log: list of call sites examined, with disposition (wrapped,
  justified-inline). Land it in the agent-log for this task.
- `python3 -m unittest discover tests` passes (no new tests required unless a
  fix is applied).

Source reflection: `docs/agent-logs/2026-04-18-dashboard-hang.md`
(follow-up: "didn't exhaustively trace").
