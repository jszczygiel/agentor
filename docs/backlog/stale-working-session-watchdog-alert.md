---
title: Stale WORKING-session watchdog surfaces as dashboard alert
state: available
category: feature
---

Operators currently have to eyeball the dashboard and realise an item has
been WORKING for too long. The 2026-04-19 stuck-session incident (5 claude
procs idle for 12 min post-`result`) would have been visible in seconds if
the daemon auto-flagged it.

Task: extend the daemon poll loop to check, for every item in WORKING with a
live `session_id`, that its transcript has been written to in the last
window. If not, push a sticky dashboard alert the same way
`_surface_infra_failure` does today, carrying the item id + minutes since
last write. Alert is informational only — do not kill the proc; the
existing `agent.timeout_seconds` path owns that decision.

Scope:

- Signal: transcript mtime for `{item_id}.{phase}.log` under
  `.agentor/transcripts/`.
- Threshold: a new `agent.stale_session_alert_seconds` knob (default ~300s
  — generous enough to not fire on a slow agent thinking, tight enough to
  flag real hangs well before `timeout_seconds` kills).
- Alert storage: reuse `daemon.sticky_alerts` plumbing (or whatever the
  current infra-failure surface is — grep `sticky` in `daemon.py`).
- De-dupe: one alert per (item_id, transcript mtime); clearing it on `u`
  already works via the existing sticky-alert machinery.

Verification:

- New daemon test: seed a WORKING item with a transcript whose mtime is
  older than the threshold, tick the poll, assert a sticky alert was
  recorded naming that item.
- Second test: transcript mtime within threshold → no alert.
- `python3 -m unittest discover tests` passes.

Source reflection: follow-up from the stdin-stays-open hang — fix prevents
the specific bug, but the class of "WORKING item silently not making
progress" will return in other shapes and deserves its own detector.
