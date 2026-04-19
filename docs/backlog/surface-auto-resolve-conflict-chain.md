---
title: Surface CONFLICTED auto-resolve chain in dashboard
state: available
category: ux
---

When `git.auto_resolve_conflicts=true`, `approve_and_commit` chains
`resubmit_conflicted` immediately after a CONFLICTED transition, so the item
re-enters the queue with the conflict summary as feedback (same worktree,
branch, session). From the operator's seat this looks like silent
re-enqueue — the dashboard offers no signal distinguishing an auto-resolve
chain from a manual `[m]` retry or fresh dispatch.

Task: add a visible marker on the transition history and/or the inspect
header for auto-resolved chains.

Scope:

- `Store.transition` already accepts a `note` kwarg that lands on the row in
  the `transitions` table. Have `approve_and_commit` tag the
  CONFLICTED → QUEUED transition with `note="auto-resolve"` when the
  auto-resolve path fires.
- Inspect view surfaces the note (badge, prefix, or a line in the transition
  list — whichever fits the current layout).
- No behavior change in dispatch, commit, or merge logic.

Verification:

- `tests/test_committer.py` — extend the auto-resolve coverage to assert the
  note lands on the expected transition row.
- Manual dashboard smoke: trigger a conflict with auto-resolve enabled,
  observe the marker.

Source reflection: `docs/agent-logs/2026-04-17-auto-resolve-conflicts.md`.
