# Split approve and feedback actions in review — 2026-04-17

## Surprises
- Ticket says "review mode" but prompt-on-approve lived in `_pickup_one_screen`, not plan/code review. Plan-review approve was already pure; still added `[f]approve+feedback` there for symmetry since execute phase consumes feedback via `_prepend_feedback`.
- Source backlog file (`docs/backlog/split-approve-and-feedback-actions-in-re.md`) was not present on disk and never tracked; task's `git rm` step is a no-op for this run.

## Gotchas for future runs
- `scan_once` with default auto pickup inserts at QUEUED, not BACKLOG — to test `approve_backlog` you have to manually transition back to BACKLOG first.
- mypy already flagged `agentor/dashboard/modes.py` line 760 pre-change (`lambda p: (p("…"), …)[-1]` — ternary-expression-return pattern in `_run_with_progress` callsite). Not introduced here; logged to IMPROVEMENTS.

## Follow-ups
- Pre-existing mypy `func-returns-value` in `_capture_note_for_expansion` (modes.py). Logged to `docs/IMPROVEMENTS.md`.
