# Fix F401 unused imports in tests/test_dashboard_resize.py — 2026-04-20

## Outcome
- Files touched: `tests/test_dashboard_resize.py`, `docs/backlog/fix-f401-in-test-dashboard-resize.md` (deleted), `docs/agent-logs/2026-04-20-fix-f401-test-dashboard-resize.md` (this file).
- Tests added/adjusted: none — lint-only cleanup. Re-ran `tests.test_dashboard_resize` (5/5 pass) to confirm no regression.
- Follow-ups: `ruff check tests/` still reports 6 other errors (F401s across `test_daemon.py`, `test_dashboard_progress.py`, `test_dashboard_transcript.py`, `test_fold.py`, plus F541 in `test_runner.py`). Out of scope for this item; worth a sweep backlog entry.
