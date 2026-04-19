# Fold agent log lessons — 2026-04-20

## Surprises
- 55 listed logs plus 2 unlisted co-residents in `docs/agent-logs/` at dispatch: `2026-04-19-integration-smoke-fake-claude.md` (landed in commit `b4756a9`) and `2026-04-19-stale-working-session-watchdog.md` (merged via `b51fbf4`) — both post-date the fold backlog's generation, so left intact. Post-fold count drops from 57 → 2, well under threshold.
- Source backlog `docs/backlog/fold-agent-lessons-2026-04-20.md` was absent on this branch (neither working tree nor history). The execute-phase mandate `git rm <source>` became a no-op — textbook case of the newly-folded "Backlog source markdown may be absent at dispatch" gotcha applied recursively to the fold item itself.

## Gotchas for future runs
- Two logs in `docs/agent-logs/` post-dated the fold backlog at execute time. Future fold runs should re-read the backlog's "Logs to consider" list against the current directory rather than folding everything present; unlisted later arrivals stay.

## Stop if
- A future fold item's "Logs to consider" omits files that clearly predate the backlog's generation date — likely the daemon's fold-queue generator regressed. Check `agentor/fold.py` and the most recent commit touching `docs/agent-logs/`.
