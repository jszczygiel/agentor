---
title: Fix F401 unused imports in tests/test_dashboard_resize.py
state: available
category: cleanup
---

`ruff check tests/test_dashboard_resize.py` reports two F401 errors
introduced in `6cde420`:

- line 9: `from types import SimpleNamespace` — unused
- line 174: `from agentor.models import Item, ItemStatus` — `ItemStatus`
  unused

The sibling `tests/test_config.py` F401 entry already logged in
`docs/IMPROVEMENTS.md` has been resolved (ruff check on that file passes
cleanly). Only the `test_dashboard_resize.py` pair remains.

Scope: delete both imports, re-run `ruff check tests/`. No-remote CI means
the regression has been silent.

Source: `docs/IMPROVEMENTS.md` (Open).
