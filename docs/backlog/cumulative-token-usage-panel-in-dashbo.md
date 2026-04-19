---
title: Add cumulative token usage panel to dashboard
category: feature
state: available
---

`_StreamState.envelope()` in `agentor/runner.py:1039-1075` already captures per-run `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` from claude's stream-json events, and those numbers land in each item's `result_json` + the `iterations` array. Nothing aggregates them, so the operator can't see cumulative token spend at a glance. Add a dashboard panel that surfaces rolling token totals so we can spot regressions (e.g. when a template change balloons input tokens) and see whether the system-prompt caching is landing as expected (`cache_read` should dominate once the cache is warm).

Scope — tokens only, no cost/$ conversion. Compute three views: (a) since-daemon-start totals, (b) today (midnight-local), and (c) last-7-days. Break each down into `input` / `output` / `cache_read` / `cache_creation`. Query from the `items` table + per-item `iterations` (or from the `failures` table for errored runs) so the totals survive daemon restarts. Render as a new panel in the main curses dashboard — minimal, one line per category — and refresh on the same `REFRESH_MS=500` tick as the main table.

Verification: unit tests on the aggregation query (fake out store with known items + iterations, assert totals). Manual test: run a few items, check cache_read_input_tokens climbs steeply after the first run (proving the `--append-system-prompt-file` cache is hitting).
