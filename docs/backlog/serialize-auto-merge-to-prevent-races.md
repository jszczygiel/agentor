---
title: Serialize auto-merge to prevent base-branch races
category: bug
state: available
---

`approve_and_commit` in `agentor/committer.py` has no locking (confirmed by grep — no `Lock`, no `threading.` imports in that module). Under parallel operation, two items reviewed and approved close together will both enter the integration step concurrently: each spawns a `--detach`ed ephemeral worktree, rebases/merges its feature branch against the current tip of `git.base_branch`, and CAS-advances the base ref via `update-ref OLD NEW`. The CAS guards against lost updates, but a race there drops one of the two integrations into CONFLICTED with a spurious "ref changed under us" error rather than a real merge conflict. The operator then has to retry via the dashboard for work that was actually fine.

Add a process-wide lock that serialises the integration phase so only one `approve_and_commit` (or `retry_merge`) is touching `base_branch` at a time. A `threading.Lock` in the daemon, passed to the committer via the existing hand-off path, is enough — integration is fast relative to agent runtime so the serialisation cost is negligible. Keep the per-feature-worktree work (commit, rebase-in-place) outside the lock; only wrap the ephemeral-worktree step that advances the base ref. Also consider whether the lock should cover `retry_merge` when two operators fire `[m]` on different conflicted items simultaneously — it should.

Verification: a concurrency test that kicks off two `approve_and_commit` calls on two different feature branches from threads and asserts both reach MERGED with distinct commit SHAs on base. Without the lock the test should flake on CAS errors; with the lock it should be deterministic.
