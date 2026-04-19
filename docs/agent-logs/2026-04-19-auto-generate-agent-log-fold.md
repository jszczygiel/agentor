# Auto-generate agent-log fold backlog items — 2026-04-19

## Surprises
- The ticket cites "human periodically greps `docs/agent-logs/`" as living in `CLAUDE.md`, but the actual source of that phrase is `AgentConfig.execute_prompt_template` in `agentor/config.py` (step 8 of the findings-log instructions). Updated both the prompt template AND the "Gotchas from prior runs" preamble in CLAUDE.md so the new auto-queue behaviour is documented in both places.
- The originating backlog file `docs/backlog/auto-generate-agent-log-fold-backlog-i.md` did NOT exist in the working tree (a prior scan had already extracted and promoted the item into the store — same pattern as `2026-04-18-dashboard-hang.md`). `git rm` was not needed; the delete already happened in a prior commit. Mentioning here so the next agent handling a similar "delete the source" mandate knows absence is an acceptable state.

## Gotchas for future runs
- `maybe_enqueue_fold_item` returns `None` when the non-terminal guard blocks — callers relying on "returns the existing path on same-day replay" only get the path when the file is on disk AND no matching item has reached the store yet. Tests must distinguish "guard blocked → None" from "file present but pre-scan → path."
- `Store.upsert_discovered` inserts items at `QUEUED`, so a test seeding a "prior non-terminal item" needs to `transition()` to any other status explicitly — there is no skip when `status == QUEUED`.
- Integration-testing the Daemon's main loop directly would have required real git worktrees (StubRunner/plan_worktree assume a git repo). Calling `maybe_enqueue_fold_item` + `scan_once` in sequence tests the same surface with none of that cost.

## Follow-ups
- None. Pre-existing mypy errors in `agentor/dashboard/modes.py` (lines 331 and 794, `func-returns-value`) are unrelated to this change — left alone per scope-guard rule.

## Stop if
- Symptoms of runaway fold-item churn (multiple `fold-agent-lessons-*.md` files on the same day, or a new one each tick): the title-prefix guard is either mis-scoped or not covering the status you'd expect. Check `_NON_TERMINAL_STATUSES` in `agentor/fold.py` first.
