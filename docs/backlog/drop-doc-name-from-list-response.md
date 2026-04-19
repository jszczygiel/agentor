---
title: Drop doc name from list response
state: available
category: polish
---

Operator note suggests trimming a redundant "doc name" field from a list response payload. The exact endpoint is unclear from the note — it could refer to a dashboard listing, a store query result, or an agent-facing response, and agentor's codebase does not have an obvious single "list response" matching this description. Investigate where item/document names are duplicated in list-style output (likely `dashboard/` renderers or `store.py` query results), confirm the field is genuinely redundant with another column/key, and remove it. Flag back to the operator if no clear match surfaces rather than guessing.
