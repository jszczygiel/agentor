---
title: Auto-queue conflict resolution after merge failure
category: feature
state: available
---

When `approve_and_commit` transitions an item to CONFLICTED, the operator currently must manually re-engage the agent via the dashboard to resolve conflicts. Extend the committer/daemon so a merge failure automatically enqueues a follow-up execution against the same feature worktree, prompting the agent to resolve the conflicts in-place. The conflict summary from `last_error` should be fed into the agent as feedback so it has context on which files diverged. Preserve the existing `[m] retry_merge` path so operators can still intervene manually, and make the auto-queue behavior opt-in via a config knob (e.g. `git.auto_resolve_conflicts`) to avoid surprising existing workflows.
