---
title: Consolidate token display and highlight provider override
state: available
category: polish
---

Clean up the dashboard header so token usage appears only in the dedicated token row below the pool/workers status area. Right now the main view renders both the inline wide-header summary from `_fmt_token_compact` and the separate token row from `_fmt_token_row`, which duplicates the same information and pushes noise to the end of the line.

Also make the active provider override easier to read at a glance. The dashboard already supports a session-only `provider_override` and shows the configured runner on the main status line plus a separate override strip; rework that presentation so an override such as configured `claude` but active `codex` is visually obvious, preferably through color/state styling rather than relying on the operator to parse multiple text cues. If color is the only signal, keep a non-color fallback for terminals without curses color support.
