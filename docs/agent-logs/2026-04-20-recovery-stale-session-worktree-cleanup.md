# Recovery sweep removes worktree dir on stale-session demote — 2026-04-20

## Gotchas for future runs
- `recover_on_startup` has two places that demote a WORKING item to QUEUED (stale-session branch and revert branch) — worktree-cleanup pattern must be mirrored in both. Easy to miss the stale-session one since it sits above the `can_resume` check.

## Outcome
- Files touched: `agentor/recovery.py`, `tests/test_recovery.py`, `docs/backlog/recovery-cleans-stale-session-worktree-dir.md` (deleted).
- Tests added: `TestRecoveryStaleSession::test_stale_session_removes_worktree_dir` in `tests/test_recovery.py`.
