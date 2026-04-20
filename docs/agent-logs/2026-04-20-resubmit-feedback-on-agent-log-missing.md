# Branch resubmit_conflicted feedback on agent-log missing cause — 2026-04-20

## Surprises
- The initial `assertNotIn("git merge", fb)` failed because the
  log-generation prompt intentionally mentions "do NOT run `git merge`" —
  the bare substring is present by design. Tightened the assertion to the
  exact merge-invocation form (`git merge main`) and to the
  merge-conflict-only section header (`Conflict summary`).

## Gotchas for future runs
- `resubmit_conflicted` asserts `status == CONFLICTED` AND requires
  `worktree_path` and `branch` to be non-empty — the quickest way to seed
  it in a test without a real merge clash is to let `StubRunner` land at
  AWAITING_REVIEW, then `store.transition(... CONFLICTED, last_error=...)`
  directly with the desired cause. Skipping the runner leaves worktree
  empty and trips the asserts.
- The `last_error == "agent-log missing"` sentinel is a shared string
  between `approve_and_commit` (writer) and `resubmit_conflicted`
  (reader). Consolidated behind `_AGENT_LOG_MISSING_CAUSE` — future
  changes to the gate's error text must update one place, not two.

## Outcome
- Files touched: `agentor/committer.py`, `tests/test_committer.py`,
  `docs/backlog/branch-resubmit-feedback-on-agent-log-missing.md` (deleted),
  `docs/agent-logs/2026-04-20-resubmit-feedback-on-agent-log-missing.md`.
- Tests added/adjusted: new class `TestResubmitConflictedFeedback` in
  `tests/test_committer.py` with three cases —
  `test_feedback_is_log_generation_when_cause_is_agent_log_missing`,
  `test_feedback_is_merge_conflict_when_cause_is_generic`,
  `test_feedback_falls_back_to_merge_conflict_when_last_error_is_none`.
- Follow-ups: none.
