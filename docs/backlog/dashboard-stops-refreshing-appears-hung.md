---
title: Dashboard stops refreshing, appears hung
state: available
category: bug
---

Operator reports the app appears stuck with nothing refreshing. The curses dashboard is expected to auto-refresh at `REFRESH_MS=500` (main table) and every 1s in inspect mode per `agentor/dashboard.py`, so a freeze suggests the refresh loop or daemon poll is blocked. Investigate whether the daemon thread is deadlocked, the SQLite connection is wedged, or a runner subprocess is holding a lock that prevents status transitions. Reproduction steps and timing are unclear from the note — capture what the operator was doing (pickup, review, merge retry?) and whether any sticky alert was showing before the hang.
