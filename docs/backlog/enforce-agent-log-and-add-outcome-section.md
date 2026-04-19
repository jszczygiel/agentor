---
title: Enforce per-run findings log and enrich with outcome section
category: feature
state: available
---

The execute prompt in `agentor/config.py` (step 8) tells the agent to
write `docs/agent-logs/<YYYY-MM-DD>-<slug>.md` before committing, but
nothing checks compliance. In practice some runs skip the log entirely,
which starves the auto-fold pipeline of the Surprises/Gotchas it exists
to cluster. Tighten this in two moves:

1. **Verify at the merge gate.** In `committer.approve_and_commit`,
   after it commits any uncommitted work but before the detached merge,
   diff the feature branch against `git.base_branch` and check that at
   least one file matching `docs/agent-logs/*.md` was added. If none,
   append `, no agent-log written` to the MERGED transition note so
   operators can grep history for the skip rate (mirroring the existing
   `, checkout advanced` / `, checkout skipped: <reason>` convention).
   Add an opt-in knob `agent.require_agent_log` (default `false`) that
   upgrades the miss to a block: transition to CONFLICTED with
   `last_error = "agent-log missing"`, so the agent can be re-queued
   via `resubmit_conflicted` to generate the log, or the operator can
   override via the normal retry path.

2. **Enrich the template.** Extend the log template in `config.py` with
   an `## Outcome` section containing: files touched (relative paths,
   cap 6), tests added/adjusted, follow-ups that didn't fit scope. This
   gives the fold step structured recurring-followup data to cluster
   into fresh backlog items, and gives `agentor-review` a consistent
   field to aggregate token spend vs. shipped scope.

Verification: unit test the committer check against a fake git repo
(branch that did vs. did not add a log under `docs/agent-logs/`);
extend the existing execute-flow smoke with an assertion that the
stub-runner output includes the `## Outcome` header. Update the
"Gotchas from prior runs" section in `CLAUDE.md` to reference the
compliance note and the `require_agent_log` knob.
