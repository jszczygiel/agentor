---
title: Visual priority indicator for dashboard rows
state: available
category: ux
---

Priority was added via shift-arrow keybindings (`bump_priority`) and claim
ordering in `claim_next_queued`. The only current visual cue for non-zero
priority is the row's position in the list — operators can't tell which rows
are pinned without comparing to `created_at` order.

Task: add a glyph or narrow column in the main table that renders when
`priority != 0`.

Scope:

- `agentor/dashboard/render.py:_render_table` — render a `*` (or similar
  single-char glyph) adjacent to the title when `item.priority > 0`. Negative
  priority (future use) renders differently or not at all — pick one.
- Keep row width budget intact; the glyph slot is part of the existing
  title column, not a new column, to avoid responsive-layout churn.
- No change to `claim_next_queued` ordering or `bump_priority` behavior.

Verification:

- `tests/test_dashboard_formatters.py` or `test_dashboard_render.py` — add a
  rendering case for priority>0 vs priority=0 items, assert the glyph
  presence.
- Manual: bump a row's priority and confirm the marker appears.

Source reflection: `docs/agent-logs/2026-04-17-prioritize-backlog-items.md`.
