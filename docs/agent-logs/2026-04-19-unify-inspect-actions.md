# Unify item detail actions across modes — 2026-04-19

## Surprises
- Source markdown `docs/backlog/unify-item-detail-actions-across-modes.md` was never committed to git (not in history on any branch). The item lives only as a row in `.agentor/state.db`. The `git rm` step in the task template is a no-op here; nothing to delete.
- `mypy agentor` reports two pre-existing `func-returns-value` errors on the `_run_with_progress` lambda-tuple pattern (`lambda p: (p("…"), work(...))[-1]`). Present on plain `main`, unrelated to this change. CI must already be tolerating them.

## Gotchas for future runs
- The unified inspect view reuses a single keymap (`_ACTION_KEYS_BY_STATUS`) for both enter-from-table and cycle walks (review/deferred). Adding a new status-gated action means extending that table AND the dispatch switch in `_inspect_dispatch` — the table is the contract, the switch is the implementation.
- `_inspect_render` sets `stdscr.timeout(1000)` on entry and resets to `REFRESH_MS` on exit. That's intentional — the 1s tick drives the live transcript feed. Don't hoist the timeout into the main loop; it would slow every key in the table from 500ms back to 1s.
- Cycle callers pass `cycle=True, remaining=<int>` to the render helper; the render loop treats `n`/`enter`/`esc` as "advance to next" while `q` exits the whole cycle. Non-cycle calls (enter from the table, prefix-prompt inspect) return on any of those keys.

## Stop if
- A future run sees `_enter_route`/`_ENTER_ROUTES` referenced again: they were intentionally removed. Grep the item body — if the ticket was written against the pre-unify layout, the plan is stale.
