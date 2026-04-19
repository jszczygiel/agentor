---
title: Priority bump keybinding inside inspect view
state: available
category: ux
---

Priority bump (`P`/`O`, Shift+Up/Shift+Down) is wired up in the main
dashboard loop (`agentor/dashboard/__init__.py:164-177`) but the inspect
detail view (`_inspect_render` in `agentor/dashboard/modes.py`) never
sees those keys — once you open an item with Enter, you can no longer
adjust priority without closing back to the table.

Task: bind the same keys inside `_inspect_render`, operating on
`item.id` (the currently-rendered row).

Scope:

- `_inspect_render` — before the `_ACTION_KEYS_BY_STATUS` dispatch,
  intercept `curses.KEY_SR` / `ord("P")` and `curses.KEY_SF` / `ord("O")`
  and call `store.bump_priority(item.id, ±1)`. Flash a short confirmation
  (`"priority +1"` / `"priority -1"` or the new value) and `continue` the
  render loop so the rest of the inspect UI stays put.
- No collision: none of the per-status action keys use `p`/`o`/`P`/`O`.
- Add a matching hint in the inspect footer (e.g. `[P/O]priority`) so
  the binding is discoverable.

Verification:

- Manual: open an item via Enter, press `P` a few times, close inspect,
  confirm the `*` glyph appears and the row moved up in claim order.
- Optional: dashboard-render test asserting priority changes are
  reflected on the next tick.
