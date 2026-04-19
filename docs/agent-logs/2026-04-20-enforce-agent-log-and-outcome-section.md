# Enforce per-run findings log and enrich with outcome section — 2026-04-20

## Surprises
- StubRunner had to grow an agent-log writer so existing `test_committer.py` MERGED-note assertions stayed green — otherwise the default knob would have injected `, no agent-log written` into unrelated tests.
- The compliance gate runs BEFORE `_INTEGRATION_LOCK`: the block path short-circuits without ever acquiring the lock or spawning a merge worktree, keeping the serialisation window unchanged.

## Gotchas for future runs
- `git diff --name-only --diff-filter=A base...feature` (three-dot form) is the right primitive for "files added on feature side since merge-base" — two-dot `base..feature` is history-order dependent and will include base-side deletions as adds.
- `resubmit_conflicted` feedback is hardcoded for merge-conflict resolution; when `require_agent_log=True` blocks an item, a naive `[e]` resubmit will push "resolve the merge conflict" feedback even though the cause is a missing log. Operator must edit before firing.

## Follow-ups
- `retry_merge` doesn't re-run the compliance gate, so an operator who manually adds a log and hits `[m]` gets a MERGED note without the skip suffix. Coherent extension would mirror the check at the retry path — small, but out of scope here.
- `resubmit_conflicted` could branch on `last_error == "agent-log missing"` to emit log-generation feedback instead of merge-conflict feedback.

## Outcome
- Files touched: `agentor/config.py`, `agentor/git_ops.py`, `agentor/committer.py`, `agentor/runner.py`, `CLAUDE.md`, `tests/test_committer.py`, `tests/test_runner.py` (7 total, cap reached at 6 significant — CLAUDE.md listed because the gotchas entry is load-bearing context for future agents).
- Tests added/adjusted: new `TestAgentLogCompliance` (4 cases) in `tests/test_committer.py`; new `test_stub_runner_writes_agent_log_with_outcome_header` in `tests/test_runner.py`. No existing tests modified — StubRunner change kept backwards compatibility.
- Follow-ups: gate-at-retry_merge and resubmit-feedback-branching listed above.
