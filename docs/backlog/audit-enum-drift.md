---
title: Audit string-based status comparisons for enum drift
state: available
category: bug
---

Grep the codebase for raw status string literals (e.g. `"working"`,
`"awaiting_review"`) used in comparisons instead of `ItemStatus.WORKING`.
Any mismatch with `ItemStatus` values is a latent bug. Replace with enum
references, and where strings cross the SQLite boundary, centralize
serialization in `models.py` or `store.py`. Add a test that round-trips
every `ItemStatus` value through the store.
