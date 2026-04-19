# Dashboard stops refreshing, appears hung — 2026-04-18

## Surprises
- The `docs/backlog/dashboard-stops-refreshing-appears-hung.md` source file cited by the task no longer exists in the working tree; the item was already extracted and promoted into the store by a prior run. No `git rm` needed.

## Gotchas for future runs
- Hot dashboard paths must stay O(1) per tick. Any `path.read_text()` on a transcript or any `transitions_for(...)` scan starves `getch` and the UI goes "hung." Use `iter_events(..., tail_bytes=_TAIL_BYTES)` and `Store.latest_transition_at(...)`. Promoted to CLAUDE.md.
- `iter_raw_events` now accepts an optional `tail_bytes` kwarg — call sites that only need the end of a live transcript should pass it.

## Follow-ups
- The `v` (diff) path in `_review_code_curses` now runs `diff_vs_base` on a worker thread via `_run_with_progress`, matching the pattern used for approve+merge and retry+merge. Audit the rest of `dashboard/modes.py` for any remaining synchronous git/subprocess calls on the main curses thread — quick visual scan didn't find more, but didn't exhaustively trace.
- Heartbeat is a log-only signal. If we want "daemon is alive" surfaced in the dashboard footer too, that's a separate UI change — out of scope here.

## Stop if
- A future report of "dashboard hung" comes in and `agentor.log` shows recent `heartbeat: idle ...` lines paired with a `status_line` that shows `workers>0`: daemon is fine, suspect `claude`/`codex` subprocess deadlock or a runner thread blocked on stdout, not the main loop.
