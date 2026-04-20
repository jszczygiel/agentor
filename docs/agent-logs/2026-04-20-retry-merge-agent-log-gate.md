# Run agent-log compliance gate on retry_merge path — 2026-04-20

## Gotchas for future runs
- `committer.retry_merge` and `committer.approve_and_commit` now share the
  same agent-log compliance gate shape: `git_ops.added_agent_logs` fires
  before `_integration_lock`, hard-block short-circuits without touching
  the merge machinery, soft-miss appends `, no agent-log written` to the
  MERGED note. Keep the suffix order `{checkout_suffix}{log_suffix}` in
  sync across both functions — `TestAgentLogCompliance` pins the exact
  substring and `test_retry_merge_note_records_advance` pins the
  advance suffix position.
- Hard-block on the retry path returns `(False, "agent-log missing")` so
  dashboard callers surface the reason identically to approve's note.
  Contrast with the `retry blocked: …` / `blocked: …` transition-note
  prefix split (approve writes `blocked: agent-log missing`, retry
  writes `retry blocked: agent-log missing`) — operators grep the retry
  prefix to distinguish manual-retry blocks from first-approval blocks.

## Outcome
- Files touched: `agentor/committer.py`, `tests/test_committer.py`,
  `docs/backlog/retry-merge-runs-agent-log-compliance-gate.md` (removed).
- Tests added: `TestAgentLogCompliance.test_retry_missing_log_appends_suffix`,
  `test_retry_require_agent_log_blocks_when_missing`,
  `test_retry_require_agent_log_allows_when_log_added` in
  `tests/test_committer.py`.
