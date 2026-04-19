# Skip plan phase on auto-resolve conflict resubmit — 2026-04-19

## Surprises
- `docs/backlog/skip-plan-phase-on-conflict-resubmit.md` was already absent from the worktree when execute phase started — no `git rm` needed. Likely removed earlier in the dispatch chain (scan deletes / upstream edits). Source-file-removal instruction is idempotent; a missing file just becomes a no-op.

## Gotchas for future runs
- `ClaudeRunner.do_work` routes on `result_json.phase == "plan"` (not session_id presence), so rewriting `phase` in-place is enough to redirect an already-executed item back into execute-only dispatch. Same branch exists in `CodexRunner.do_work` — per-item behavior stays runner-agnostic.
- `Runner.run`'s resume branch needs **both** a live `session_id` AND a worktree that exists on disk — end-to-end runner tests for resumable flows must pre-create the worktree via `git_ops.worktree_add` before `claim_next_queued`, not rely on the runner to create one.
