---
title: Fast-fail stale-session on recovery sweep
state: available
category: bug
---

When the daemon restarts, `recovery.py` treats any WORKING item with a live
`session_id` + worktree as resumable and re-queues it. If the Claude CLI's
session has expired (5-hour TTL), the next dispatch launches
`claude --resume <id>` which exits 1 with
`No conversation found with session ID …`. The failure bubbles through
`do_work`, the item errors, operator ends up manually re-queueing, and the
agent runs the plan phase again anyway — but we paid for the original plan
and a resume-and-fail round-trip.

Token analysis 2026-04-17 → 2026-04-19: **6 `No conversation found` failures**
in the window (statistically the largest non-shutdown failure class). At
~$0.50 per wasted resume attempt, ~$3 avoidable spend plus operator friction.

Task: recognize stale-session on the recovery sweep and demote to a fresh
plan run instead of a resume attempt.

Scope:

- `agentor/recovery.py` — on startup sweep, if a WORKING item has
  `session_id` set and its stored age exceeds a conservative threshold
  (default 4 hours; configurable), or if a prior `failures` row for the item
  has `error_sig` matching `do_work:claudeexited:noconversationfoundwithsessionid:*`,
  demote: `session_id = NULL`, status → QUEUED, `last_error` set to a benign
  marker ("session expired; restarting plan").
- First-attempt stale-session failures still happen (session TTL mid-run is
  outside recovery's scope). Those are the signal that drives the threshold.
- Leave the existing "benign last_error" recovery path intact — this just
  adds one more auto-recoverable class.

Verification:

- `tests/test_runner.py` or `tests/test_recovery.py` — seed a WORKING item
  with a 5h-old `session_id` and a stubbed failure row matching the pattern;
  assert recovery re-queues it with `session_id=NULL` and a benign
  `last_error` marker.
- Log line in daemon startup: `auto-recovered N items with stale claude
  session`.

Source: token usage analysis flagged session-loss as the dominant failure
class; `.agentor/analyses/2026-04-19-agent-logs-review.md`.
