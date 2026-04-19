# cumulative token usage panel — 2026-04-19

## Surprises
- Source-file `docs/backlog/cumulative-token-usage-panel-in-dashbo.md` was absent from the worktree (and not in git history). Delete step was a no-op.
- `failures` table stores only turns/duration/files/transcript — no tokens. Per-item `result_json` survives across retries and errored runs, so aggregating off `items.result_json` alone covers the "errored runs" requirement without touching `failures`.

## Gotchas for future runs
- `items.result_json` is overwritten per retry; aggregate is "current state across items", not a historical sum. Document this when a future ticket asks for per-attempt accounting.
- `items.updated_at` is bumped by live streaming (`update_result_json`), so long-running WORKING items with old token spend still count in today/7d. Good enough for operator spot-checks; matters if strict per-day bucketing is requested later.

## Follow-ups
- Cache the aggregation per-tick if large projects (>~1000 items with result_json) show lag on the 500ms tick — SELECT + N JSON parses currently runs unconditionally.
- Consider a keybind to toggle the panel for small terminals where 4 extra header rows eat useful body.
