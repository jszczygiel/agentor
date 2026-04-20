---
title: Branch resubmit_conflicted feedback on agent-log missing cause
state: available
category: ux
---

When `agent.require_agent_log = true` and `approve_and_commit` blocks the
MERGED transition with `last_error = "agent-log missing"`, the subsequent
auto-chained (or operator-driven) `resubmit_conflicted` call emits the
hardcoded merge-conflict feedback in `agentor/committer.py:440-454`
(instructs the agent to run `git merge <base>`, resolve markers, etc.).
That prompt is wrong for a log-absence cause — the agent should be told to
write `docs/agent-logs/<YYYY-MM-DD>-<slug>.md` capturing Surprises /
Gotchas / What worked, not to resolve a merge conflict that never
happened.

Scope: branch `resubmit_conflicted`'s feedback construction on
`item.last_error == "agent-log missing"`. Emit a dedicated
log-generation prompt (template: "Write a per-run findings log under
docs/agent-logs/..." matching the StubRunner writer pattern). Keep the
default merge-conflict feedback for everything else. Add a test in
`tests/test_committer.py` asserting the feedback text differs per cause.

Source: `docs/agent-logs/2026-04-20-enforce-agent-log-and-outcome-section.md`
(Follow-ups section).
