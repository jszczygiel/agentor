---
title: Skip plan phase on auto-resolve conflict resubmit
state: available
category: perf
---

When `git.auto_resolve_conflicts=true`, `approve_and_commit` chains
`resubmit_conflicted` after a CONFLICTED transition. The item re-enters the
queue and runs the **full plan + execute cycle** with the conflict summary as
feedback. The plan phase is wasted work — conflict resolution is pure execute
(open worktree, resolve markers, re-run tests, commit).

Token analysis 2026-04-17 → 2026-04-19 (see
`.agentor/analyses/2026-04-19-agent-logs-review.md`): 6 conflicted transitions
in window. At plan-phase avg ~16 turns × ~$0.50 that's roughly $3–6 of
avoidable spend per review window. Also spends ~10 extra minutes of
wall-clock per conflict.

Task: force single-phase execute on the auto-resolve resubmit path.

Scope:

- `agentor/committer.py:resubmit_conflicted` (or wherever the resubmit chain
  originates) passes a per-run override that makes the next dispatch run
  execute-only.
- Simplest: add a `single_phase: bool` override on the item (transient, not
  persisted to config) that `runner.do_work` honours for the next run only,
  then clears. Or plumb through the dispatch path as an explicit kwarg.
- First-pass runs and manual `[e]resubmit` are unchanged — only the
  `auto_resolve_conflicts=true` chain triggers this.

Verification:

- `tests/test_committer.py` — extend auto-resolve coverage to assert the
  resubmitted run runs execute only (no `plan.log` created, feedback lands in
  execute prompt).
- Manual: trigger a conflict with auto-resolve enabled, observe single
  transcript and a faster turnaround.

Source: token usage analysis flagged retry tax (90% of spend on retried
items); conflict resubmits are a subset with a known waste shape.
