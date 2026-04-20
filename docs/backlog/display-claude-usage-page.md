---
title: Display claude.ai/settings/usage as usage page
state: available
category: feature
---

Expose `https://claude.ai/settings/usage` as the canonical usage/cost view surfaced from agentor (e.g. a dashboard keybinding or a `tools/` helper that opens the URL in the operator's default browser). The in-dashboard token panel aggregates per-run cache/in/out counts from transcript `result_json`, but it does not reflect billable spend against Anthropic's account — the authoritative dollars-and-quota view lives at that URL.

Scope:
- Add a keybinding on the main dashboard (suggestion: `U`) that shells out to `open https://claude.ai/settings/usage` on macOS (`xdg-open` on Linux). Guard behind a short-lived flash confirming the action, consistent with other dashboard actions.
- Optionally surface the URL in the help overlay (`?`) so operators discover it without reading source.
- Decide whether to embed it as an iframe/webview (no — adds a heavy dep, violates the no-deps policy) or stick with an external-browser handoff (yes — stdlib `webbrowser.open` is fine).

Touch points:
- `agentor/dashboard/modes.py` — add action function, lazy-import any new helpers per the existing pattern.
- `agentor/dashboard/render.py` — register the key in `_ACTION_KEYS_BY_STATUS` (or a new always-available action bucket), update `ACTIONS` / `ACTIONS_WIDE` / `ACTIONS_MID` strings and the matching regression tests in `tests/test_dashboard_render.py::TestActionsHint`.
- `tests/test_dashboard_enter.py` — update the pinned key set if the action is status-scoped.

Verification: pressing the key on macOS opens the URL in the default browser; dashboard flashes a confirmation; no regression in other actions.
