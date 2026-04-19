# Stale WORKING-session watchdog — 2026-04-19

## Gotchas for future runs
- `dashboard/transcript._transcript_path_for` depends on `_phase_for` from
  `dashboard/formatters`, which pulls dashboard-layer helpers. Daemon-side
  callers should inline the `.agentor/transcripts/{id}.{phase}.log` path
  calculation instead of importing the dashboard helper to avoid curses-
  adjacent dependencies leaking into the scheduler.
- `_FakeDaemon` in `tests/test_dashboard_render.py` and
  `tests/test_dashboard_resize.py` is NOT a subclass of `Daemon` — it is a
  hand-rolled stub. Any new attribute the renderer reads must either be
  added to those stubs or the renderer must use `getattr(..., default)`.

## Follow-ups
- Dashboard renders at most three stale-session rows plus a `+N more`
  summary so the table stays usable on narrow terminals. If operators
  routinely hit the cap, consider a dedicated stale-session list view
  behind a key binding rather than expanding the sticky area.
