---
title: Surface auto-resolve chain in main dashboard table
state: available
category: ux
---

`d9fecf0` added an "auto-resolve chain" line to the inspect view
(`agentor/dashboard/modes.py:462-463` — `_is_auto_resolve_chain` fires
when the most recent `→ QUEUED` transition note starts with
`AUTO_RESOLVE_NOTE_PREFIX`). The main table itself still renders an
auto-resubmitted QUEUED item identically to a fresh human-approved
resubmit, so the operator can't distinguish committer-driven chains
from `[e]` resubmits without drilling into inspect.

Scope: add a compact glyph/badge in the main-table row for QUEUED
items whose most-recent transition was auto-resolve. Reuse
`_is_auto_resolve_chain(store, item)`. Keep the glyph narrow enough to
fit the existing row-tier layouts (wide / mid / narrow). Add coverage
to `tests/test_dashboard_render.py` pinning the glyph presence.

Source: retired `docs/IMPROVEMENTS.md` (Open). Partial scope already
shipped via `d9fecf0` (inspect view); main-table badge is what's left.
