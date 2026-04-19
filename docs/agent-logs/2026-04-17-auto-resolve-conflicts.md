# Auto-queue conflict resolution after merge failure — 2026-04-17

## Surprises
- `resubmit_conflicted` already existed end-to-end (feedback builder, state transition, worktree/branch/session preservation). Task was purely wiring: one config knob + one conditional call inside `approve_and_commit`.

## Gotchas for future runs
- `Store.transition(item.id, item.status, session_id=…)` can be used in tests to patch fields without changing status — handy for seeding `session_id` on stub-driven items. No dedicated setter exists.

## Follow-ups
- Dashboard inspect view does not yet signal that a CONFLICTED → QUEUED chain happened automatically; operators see the item silently re-enter the queue. A one-line `auto-resolve` marker in the transition history or inspect header would make the chain legible. Logged for later.
