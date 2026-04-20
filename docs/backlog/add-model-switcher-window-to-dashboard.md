---
title: Add model switcher window to dashboard
state: available
category: feature
---

Add a dashboard action that opens a modal/overlay window for switching the active agent model/provider at runtime. Current `agent.runner` (`stub` | `claude` | `codex`) and any per-tier model nomination are fixed in `agentor.toml`; operators need an in-dashboard way to flip providers without editing config and restarting the daemon. The overlay should list available providers/models, show the current selection, and persist the choice so subsequent dispatches pick it up. Consider where the setting lives (in-memory override vs. written back to `agentor.toml`) and how it interacts with already-working items — unclear from the note whether the switch should apply only to newly-dispatched items or also re-target resumable ones.
