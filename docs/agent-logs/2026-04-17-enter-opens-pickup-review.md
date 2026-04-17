# Enter opens pickup/review actions — 2026-04-17

## Surprises
- Task description claimed enter was already bound to `_inspect_render` at `dashboard.py:143-152`. That file no longer exists (split in commit `4abfdc1` into `agentor/dashboard/{render,modes,formatters}.py`) and enter had no binding at all — implementation therefore introduced both row selection and the enter dispatch from scratch.
- Main table previously had no row-selection concept; `_render_table` iterated statuses internally and the caller never saw the item list. Had to lift the flatten step into `_render` so `_loop` can map `selected_idx` → item.

## Gotchas for future runs
- Selection clamp must run *after* `_render` returns the fresh items list, not before: items move between status buckets each tick, so any cursor index computed before render is already stale.
- A_REVERSE for the selection highlight is XORed onto the existing attr (not OR'd) so pre-reversed rows (none today, but future-proof) toggle cleanly and the status color survives.
- `_pickup_one_screen` returns `"quit"` / `""` — same contract as `_review_plan_curses` / `_review_code_curses` — so the top-level `_pickup_mode` walker can honor `q` without re-implementing the loop.

## Follow-ups
- None required for this task. QUEUED rows currently route to inspect (noted in the plan's open question); reviewer can request a QUEUED-specific screen later if wanted.
