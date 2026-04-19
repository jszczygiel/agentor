---
title: Remove BACKLOG from ItemStatus enum
state: available
category: cleanup
---

`pickup_mode` was removed — dispatch is always automatic — and
`Store.upsert_discovered` now inserts new items at QUEUED directly. The
BACKLOG state is dead except for one inline fallback in
`_pickup_one_screen` that walks DEFERRED items' history back to BACKLOG.

Task: remove `ItemStatus.BACKLOG` entirely.

Scope:

- `agentor/models.py` — drop the enum member.
- `agentor/dashboard/modes.py:_pickup_one_screen` — remove the DEFERRED →
  BACKLOG history-walk fallback (DEFERRED items just skip the branch).
- `agentor/recovery.py` — if any recovery path maps to BACKLOG as a prior
  settled status, remap.
- Legacy rows: existing DBs may contain `status='backlog'` TEXT. Add a one-shot
  migration in `Store.__init__` (schema-heal pattern already used for
  `priority`) that updates any `backlog` rows to `queued`.
- Purge BACKLOG from `_encode_status` / `_decode_status` helpers and any
  remaining enum-value references in dashboard filters.

Verification:

- `tests/test_store.py` — status round-trip drops BACKLOG; new case asserts
  legacy DB with a `backlog` row heals to `queued` on open.
- `tests/test_watcher.py::test_scan_lands_at_queued_not_backlog` still passes
  (regression guard).
- `tests/test_dashboard_render.py` — filter list no longer shows a BACKLOG
  bucket.
- Full suite: `python3 -m unittest discover tests` passes.

Source reflection: `docs/agent-logs/2026-04-18-remove-pickup-mode-toggle.md`
(stop-if: item stuck at BACKLOG in a fresh DB).
