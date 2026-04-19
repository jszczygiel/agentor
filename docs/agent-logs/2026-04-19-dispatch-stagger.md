# dispatch stagger — 2026-04-19

## Gotchas for future runs
- `daemon.try_fill_pool` is called from the curses UI thread (see `dashboard/modes.py` approve/pickup handlers). A stagger of N seconds × (dispatches-1) blocks input for that long. Default is 0 so no regression — but operators who bump it should expect UI freezes during bulk-approve bursts.
- `_stagger_wait` wraps `stop_event.wait(seconds)` (not `time.sleep`) so SIGINT/SIGTERM cuts the delay short instead of forcing a full stagger before shutdown.
- Tests stub the wait by reassigning `d._stagger_wait = list.append`; going via a bound method keeps the method-monkeypatch pattern working on 3.11+.

## Follow-ups
- Dashboard blocking during staggered dispatch is a known trade-off. If it becomes painful, move the stagger off the caller's thread (e.g. a queue drained by a dedicated dispatcher thread). Out of scope for this item.
