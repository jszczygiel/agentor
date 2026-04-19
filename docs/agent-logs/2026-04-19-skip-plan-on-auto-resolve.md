# Skip plan phase on auto-resolve conflict resubmit — 2026-04-19

## Surprises
- `docs/backlog/skip-plan-phase-on-conflict-resubmit.md` was already absent from the worktree when execute phase started — no `git rm` needed. Likely removed earlier in the dispatch chain (scan deletes / upstream edits). Source-file-removal instruction is idempotent; a missing file just becomes a no-op.
- Merge-conflict resubmit drove a second planning + execute pass on this same branch: main's `surface-auto-resolve-chain` landed a parallel `note=` kwarg on `resubmit_conflicted`, conflicting with my `force_execute=` kwarg on the same signature. Features are orthogonal — reconciled by accepting both kwargs and passing both on the auto chain.
- Main HEAD was broken at merge time: `agentor/dashboard/render.py:_STATE_GLYPHS` referenced `ItemStatus.BACKLOG` after commit `326ead0` removed the enum member. Two PRs landed conflict-free via auto-merge but interacted destructively. Fixed inline as a prerequisite to running my own tests; logged in `docs/IMPROVEMENTS.md` for a broader audit.

## Gotchas for future runs
- `ClaudeRunner.do_work` routes on `result_json.phase == "plan"` (not session_id presence), so rewriting `phase` in-place is enough to redirect an already-executed item back into execute-only dispatch. Same branch exists in `CodexRunner.do_work` — per-item behavior stays runner-agnostic.
- `Runner.run`'s resume branch needs **both** a live `session_id` AND a worktree that exists on disk — end-to-end runner tests for resumable flows must pre-create the worktree via `git_ops.worktree_add` before `claim_next_queued`, not rely on the runner to create one.
- `_STATE_GLYPHS`-style enum-keyed dicts with hardcoded member names are a silent-break hazard on enum renames/removals — mypy doesn't flag the stale key because the dict type is `dict[ItemStatus, str]` and the offending member was a valid `ItemStatus` at the time of writing. Prefer a loop over `ItemStatus` members or an exhaustiveness check so the next rename surfaces at type-check time rather than import time.

## Follow-ups
- `docs/IMPROVEMENTS.md` entry: audit for other stale `ItemStatus.BACKLOG` references + consider enum exhaustiveness tooling.
