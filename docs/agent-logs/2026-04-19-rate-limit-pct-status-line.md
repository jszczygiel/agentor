# Show session/weekly rate-limit % in status line — 2026-04-19

## Surprises
- Claude stream-json feed emits zero rate-limit data. Full-repo grep for `rate_limit|ratelimit|tokens_remaining|quota|anthropic-ratelimit|reset_at|retry_after` across `agentor/`, `tests/`, `docs/`, `tools/` returned no hits outside the error-signature classifier. No fixture or captured transcript exists either. Headers are stripped by the CLI — budget-fallback path is the real implementation, harvester is speculative future-proofing.
- `_fmt_tokens` always formats `500_000 → "500.0k"` (not `"500k"`); first round of snapshot tests asserted the wrong string and failed. Fixed by matching actual output.

## Gotchas for future runs
- The `_harvest_rate_limits` scan inspects four nested points — `ev`, `ev["message"]`, `ev["usage"]`, `ev["message"]["usage"]`. If a future CLI plants the hint anywhere deeper, extend the scope list rather than making it recursive (recursion adds per-event cost on the hot dashboard tick path).
- Token-budget config lives on `AgentConfig`, not a dedicated `RateLimitConfig`. Keep both budgets there unless a larger redesign moves everything; `_fmt_token_compact` reads them via `getattr(..., default=0)` so other callsites stay robust to future config splits.

## Follow-ups
- If Anthropic ever surfaces `anthropic-ratelimit-*` in stream-json, flip `_fmt_token_compact` to prefer harvested samples over budget math (fields already persist on `result_json["rate_limits"]`). No new config needed.
- True 5h session window (vs daemon-started session) would need the CLI to emit `reset_at` or a sibling signal; no path today.

## Stop if
- `_StreamState.envelope()` starts growing a `rate_limits` key in production runs — that means the CLI changed its contract and this budget-fallback logic may be silently stale. Inspect the sample shape before assuming it matches the speculative test fixtures.
