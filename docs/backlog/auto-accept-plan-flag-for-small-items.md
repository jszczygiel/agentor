---
title: Flag to auto-accept plans for small items (skip plan-review gate)
category: feature
state: available
---

Today every non-`single_phase` item stops at AWAITING_PLAN_REVIEW and waits for an operator `a`. For items whose spec is already clear, the plan-review gate is busywork — but flipping `agent.single_phase = true` throws away the plan phase entirely, and we lose the "read + grep + think first" benefit. What we want is a middle path: run the plan phase to get the agent to orient itself, then auto-approve into execute without the human in the loop, but *only for items we're confident are safe to run unattended*.

Proposed shape — two knobs, one new status transition:
- `agent.auto_accept_plan` (`off` | `small` | `always`, default `off`). `always` is the "I trust the agent, stop pestering me" mode. `small` is the interesting case below.
- `agent.auto_accept_verifier` (`none` | `model`, default `none`). When `model`, after the plan phase emits its summary, we run a tiny second Claude call (haiku-tier, cheap) that scores the plan against the task body and either green-lights or kicks back to human review. Gates against the agent gaslighting itself into a destructive plan.

Auto-accept path: on `AWAITING_PLAN_REVIEW` transition, if the gating predicate passes, the daemon calls `approve_plan` immediately instead of waiting. A new `transitions.note` value (`auto-accepted: <reason>`) records the skip so operators can audit via the inspect view.

Criteria worth considering for what counts as "small" (operator picks one or a combo in config):
- **Body size.** e.g. `small_max_body_words = 120`. Cheap, deterministic, but dumb — a 50-word item can still say "rewrite the scheduler".
- **Explicit opt-in tag.** `@auto` in the item frontmatter/inline tag → auto-accept. Puts the decision on whoever wrote the backlog item. Most surgical, no false positives.
- **Category allowlist.** e.g. `auto_accept_categories = ["docs", "test", "cosmetic"]`. Fine-grained but relies on disciplined category field.
- **Path-based risk.** Plan summary mentions only paths matching `auto_accept_paths_allow` (e.g. `tests/**`, `docs/**`) and touches no file in `auto_accept_paths_deny` (e.g. `agentor/store.py`, `agentor/runner.py`, anything under `migrations/`). Needs the plan to enumerate touched files — current plan template doesn't force this, would need a template tweak.
- **Keyword denylist on the body.** Reject auto-accept if body mentions `migration`, `auth`, `security`, `breaking`, `database schema`, `delete`, `drop`. Cheap heuristic, high recall for "probably risky".
- **Iterations-since-rejection floor.** If the last N items from this source file were all merged cleanly, raise trust; if anything was rejected in the last week, require human review. Adapts to how well the backlog is written.
- **Model-verifier vote.** The `auto_accept_verifier = model` path — ask a cheap model "is this plan's blast radius confined to the stated task, yes/no, confidence". Use it as a second gate on top of one of the above, never as the only gate (LLM judge alone is too easy to fool).

Recommendation for first cut: ship `auto_accept_plan = always` as the minimal version (one config flag, easiest to reason about, operator owns the risk globally), then layer `auto_accept_plan = small` with the **explicit `@auto` tag + keyword denylist** as the predicate for v2. Model-verifier is a v3 — worth building only after we see the naive tag path producing false confidence in practice.

Implementation hooks: predicate lives in a new `agentor/auto_accept.py` so it's unit-testable without a daemon; `daemon.py` calls it after any AWAITING_PLAN_REVIEW transition and routes to `committer.approve_plan` on pass. Must survive `recovery.py` — a crash between plan-done and auto-approve should not strand items; recovery should re-run the predicate. The sticky-alert infra (see `daemon.py`) already handles "this infra decision went wrong" surfacing.

Verification: unit tests on each predicate (body-size, @auto tag, category, keyword denylist) with sample items; integration test that spins a stub runner through plan → auto-accept → execute without any operator keypresses; manual test on the self-hosted agentor project with a low-risk `docs/` item tagged `@auto`.
