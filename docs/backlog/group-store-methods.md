---
title: Group Store methods into logical sections
state: available
category: refactor
---

`agentor/store.py` exposes 40+ methods on a single `Store` class —
queue ops, transitions, history, usage, feedback, alerts. Either split
into mixins (`QueueMixin`, `HistoryMixin`, `UsageMixin`) composed into
`Store`, or at minimum add `# --- queue ---` / `# --- history ---` section
headers and reorder methods so related ones cluster. No behavior change;
existing call sites in daemon/dashboard/runner must keep working.
