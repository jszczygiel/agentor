# Merge main into enter-opens-pickup-review — 2026-04-17

## Surprises
- Reviewer's conflict summary named only `agentor/dashboard/render.py`, but `git merge main` actually surfaced two conflict files (`render.py` + `__init__.py`) and auto-merged a third (`modes.py`). The summary was stale — main had landed an entire ID-based row-selection scaffold (`_idx_of`, scroll viewport, `selected_id` state, cursor-snap on status change) plus an unrelated `_new_issue_mode` feature (`n` key) between when the conflict summary was generated and the merge ran.
- Main already implemented the "enter opens the selected row" half of this task — its enter handler just always opened `_inspect_render`. Our unique contribution after merge is only the status-based routing (`_enter_route` + `_enter_action` replacing the unconditional inspect call).

## Gotchas for future runs
- When the reviewer cites a single conflicting file, still run `git merge main` and read the full output — orthogonal base changes may have layered without conflict but still change your surface area (e.g. new keybindings you must not clobber).
- Main's dashboard selection uses `selected_id: str | None` (stable across list reorderings), not the index-based model our original branch introduced. Signatures of helpers called from `_loop` should take a `StoredItem` or id, not `(items, idx)`.

## Follow-ups
- Pre-existing mypy error in `agentor/dashboard/modes.py:760` (`_run_with_progress` lambda returning a tuple subscript — `func-returns-value`) exists on main as well; out of scope for this merge.
