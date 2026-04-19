---
title: Responsive dashboard for narrow terminals (phone tmux)
state: available
category: feature
---

Operator runs the dashboard from tmux on a Pixel (≈40–80 cols wide) and content is silently cut off. The curses UI in `agentor/dashboard/` has several hardcoded widths that assume ≥80 cols, so critical info disappears on narrow terminals.

Concrete breakage points (from research):

- `agentor/dashboard/formatters.py:8-13` — fixed column widths (ID=10, STATE=18, ELAPSED=9, CTX=6, SOURCE=26) sum to 69 cols before TITLE. At `w < 69` the dynamic TITLE width in `agentor/dashboard/render.py:250-251` goes to 0 and titles vanish.
- `agentor/dashboard/render.py:24-25` / hint bar around `render.py:107` — the ~150-char `[↑/↓]nav [enter]open …` string is silently truncated; the user never sees most of the key hints.
- Status line in `render.py:132-146` — long `pool=X mode=Y workers=Z done=A …` enumeration. At 60 cols only the first 2–3 fields fit.
- `agentor/dashboard/modes.py:332-335` — token breakdown table uses a hardcoded 36-char model column, so the detail view overflows/wraps badly <80 cols.
- Alert banner in `render.py:118-119` truncates at `w - 30`, which breaks when `w < 33`.

Proposed tiered layout:

- **≥80 cols**: current layout.
- **60–79 cols**: drop SOURCE column, expand TITLE rightward; shorten status line to `p=X w=Y d=Z e=A`; abbreviate hint bar.
- **<60 cols**: also drop STATE label to a 1-char glyph, collapse hint bar to `↑↓ ⏎ q  [?]help` with a dedicated help screen for the full legend; vertical-format the token breakdown table (one field per line).

Implementation notes:

- Centralise the width-tier decision (e.g. `_layout_tier(w)`) so all renderers agree.
- Keep `getmaxyx()` calls per-render — terminal resize should reflow immediately.
- Add tests in `tests/test_dashboard_render.py` / `test_dashboard_formatters.py` that assert rendered output fits within a given width at 40 / 60 / 80 cols.
- Guard the alert banner truncation for `w < 33` so it doesn't overflow.

Non-goals: no horizontal scrolling; no separate "mobile mode" flag — width-based auto-switch only.
