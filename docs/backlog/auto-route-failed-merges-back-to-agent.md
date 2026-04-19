---
title: Auto-route failed merges back to agent
state: available
category: feature
---

When auto-merge lands a feature branch in CONFLICTED, the committer already supports `git.auto_resolve_conflicts` to chain `resubmit_conflicted` and re-queue the item for the agent to resolve. Operator wants this to be the default path for merge failures rather than opt-in — failed integrations should automatically bounce back to the agent without operator intervention. Investigate whether to flip the default in `agentor/config.py`, expand the trigger to cover non-conflict merge failures (e.g. rebase aborts, push races), or surface a dashboard hint when the gate is off. Preserve the existing opt-out for operators who want manual control.
