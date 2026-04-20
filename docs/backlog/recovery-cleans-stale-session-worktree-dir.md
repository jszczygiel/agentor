---
title: Recovery sweep removes worktree dir on stale-session demote
state: available
category: cleanup
---

`agentor/recovery.py:145-152` (stale-session branch of `recover_working`)
transitions the item back to QUEUED with `worktree_path=None` but leaves
the actual worktree directory on disk. `claim_next_queued` overwrites
`worktree_path` with a fresh slug on re-dispatch, so the old directory
lingers until external cleanup (or a conflicting `git worktree add`).

The adjacent revert branch at `recovery.py:165-168` already shows the
pattern: `git_ops.worktree_remove(repo, wt, force=True)` followed by a
`shutil.rmtree(..., ignore_errors=True)` fallback if the dir still
exists. Mirror that pre-transition in the stale-session branch, gated on
`wt is not None and wt.exists()`.

Verification: add a test to `tests/test_recovery.py` seeding a WORKING
item with a real temp worktree dir + a dead-session failure, then
asserting the dir is absent after `recover_working`.

Source: `docs/IMPROVEMENTS.md` (Open) and
`docs/agent-logs/2026-04-19-fast-fail-stale-session.md` (Follow-ups).
