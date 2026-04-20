---
title: Run agent-log compliance gate on retry_merge path
state: available
category: bug
---

`committer.approve_and_commit` checks for an added `docs/agent-logs/*.md`
file via `git_ops.added_agent_logs` before completing the MERGED transition
(and blocks with `last_error = "agent-log missing"` when
`agent.require_agent_log = true`). `committer.retry_merge` — the `[m]`
dashboard action — does not re-run this check. An operator who manually
writes the missing log and hits `[m]` therefore gets a MERGED transition
whose note lacks the `, no agent-log written` suffix even when no log was
added, and under `require_agent_log = true` the manual-retry path bypasses
the block that `approve_and_commit` would enforce.

Scope: mirror the compliance check at the top of `retry_merge` before the
integration attempt. Reuse `git_ops.added_agent_logs` and the same
soft-note / hard-block branch logic. Confirm via new test in
`tests/test_committer.py::TestAgentLogCompliance` covering the retry path.

Source: `docs/agent-logs/2026-04-20-enforce-agent-log-and-outcome-section.md`
(Follow-ups section).
