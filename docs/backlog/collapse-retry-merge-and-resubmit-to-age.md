---
title: Collapse retry-merge and resubmit-to-agent actions
state: available
category: polish
---

In the inspect view for CONFLICTED items, `[m]` retry merge and `[e]` resubmit to agent expose two keys for what the operator considers the same outcome: re-attempting integration. Pick one binding and remove the other, or merge their semantics so a single key handles both the local re-merge attempt and the auto-resubmit path (`git.auto_resolve_conflicts`). Update the inspect-mode key legend in `agentor/dashboard/modes.py` and any tests asserting both bindings. Confirm with the operator which key/label survives before deleting either action.
