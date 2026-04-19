---
title: Show actual session / weekly rate-limit % in status line
category: feature
state: available
---

The dashboard status line currently prints `tok sess=1.5M  wk=10M` — absolute token sums aggregated from claude stream-json envelopes (see `agentor/dashboard/formatters.py:305-311` `_fmt_token_compact`). Operators want actual **%** of the Claude plan's session (5h) and weekly quota so they can tell at a glance how close they are to being throttled, rather than eyeballing raw totals against a mental budget.

Scope — investigate whether claude's stream-json events already carry `anthropic-ratelimit-*` hints (tokens-remaining, requests-remaining, reset-at) in the response headers / system init event / result event. `_StreamState.envelope()` in `agentor/runner.py` is the extraction point; `agentor/dashboard/transcript.py` is the reader. If the data is there, harvest it (latest-wins per run), persist the most recent sample on each item (`result_json.rate_limits = {session_pct, weekly_pct, session_reset_at, weekly_reset_at}`), and surface the max of the newest samples in the status line as `tok sess=1.5M (42%)  wk=10M (71%)`. If the CLI strips these headers, fall back to the operator-configured budget option (`agent.session_token_budget` / `agent.weekly_token_budget` in `agentor.toml`, compute `total / budget`) and document the fallback mode in the commit.

Verification: unit tests on the envelope extractor with a synthetic stream-json transcript that includes rate-limit fields; snapshot test on `_fmt_token_compact` that asserts the `(NN%)` suffix only renders when a sample exists; manual test by running one item against claude and confirming the % updates.
