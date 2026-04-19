---
title: Retry transient claude errors with exponential backoff
category: feature
state: available
---

The runner currently has one narrow retry path — `_is_dead_session_error()` in `agentor/runner.py:115-124` detects a lost session, clears `session_id`, and bounces the item back to QUEUED so the next dispatch starts fresh. Other transient failures (Anthropic 429 rate limit, 5xx, TCP/DNS hiccups, `claude` CLI returning a timeout exit code on a momentary stall) fall through to the generic error path and burn an attempt — with `max_attempts=3` a flaky afternoon can eat all three attempts before the user sees a real problem.

Add a classifier that recognises retryable errors (HTTP 429, 500-504, common network strings like "Connection reset", "Temporary failure in name resolution", `subprocess.TimeoutExpired` when total elapsed was under a fraction of `timeout_seconds`) and loops with exponential backoff inside the same dispatch, refunding the attempt on success. Suggested cadence: 3 retries at 2s, 8s, 30s with small jitter; on the final failure let the error propagate so the existing failure-alert sticky UI fires. Make the retry budget configurable (`agent.transient_retries`, default 3) and log each backoff to the transcript so operators can see why a run took longer than expected. Critically: don't retry non-transient errors — auth failures, quota-exhausted, syntax errors in the prompt should fail fast and land in the dashboard, not silently retry until timeout.

Verification: unit tests feeding each error shape into the classifier and asserting retryable vs fatal, plus an integration test with a fake `claude` that fails twice then succeeds — attempt count unchanged, status transitions land where expected.
