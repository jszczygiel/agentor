---
title: Compact weekly/session token indicator on header
state: available
category: ux
---

The token panel (`_render_token_panel` in `agentor/dashboard/render.py:181-196`)
already shows three full breakdown rows (session / today / 7d), each with
input / output / cache_read / cache_create. Good for deep inspection, but
noisy at a glance — the operator has to read four numbers across two rows to
know "am I burning tokens faster than last week?".

Task: add a compact, always-visible indicator for **session** and **weekly**
totals so cumulative spend is readable in one glance without scanning the
breakdown panel.

Scope suggestions (pick one, or propose another in the PR):

- Inline indicator on the existing status line in `_render` (render.py:132-147)
  — e.g. `│ tokens sess=123.4k wk=2.3M` appended after the status counts.
  Single line, no extra vertical real estate. Could live alongside the existing
  panel, which stays as the detailed breakdown.
- Or: collapse the panel to two labelled rows (session + weekly) and drop
  "today", if the operator considers `today` redundant next to `session`. This
  is a scope-down and reduces vertical space.
- Either way, keep `cache_read` visible — the whole point of the panel is
  verifying that the `--append-system-prompt-file` cache is warming up.

Verification: extend `tests/test_dashboard_render.py` to assert the compact
indicator renders the expected substring for a store populated with known
totals across session/weekly windows. Manual: run a few items and confirm
numbers match the full panel.
