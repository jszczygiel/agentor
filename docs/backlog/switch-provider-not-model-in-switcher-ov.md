---
title: Switch provider not model in switcher overlay
state: available
category: feature
---

The `[M]` switcher overlay currently swaps between Claude model aliases (haiku/sonnet/opus), but the operator wants to toggle between providers — Claude vs OpenAI — instead of model tiers within one provider. Agentor already has both `ClaudeRunner` and `CodexRunner` in `agentor/runner.py` selected via `agent.runner` in config, so the provider axis exists, it's just not exposed live. Rework the dashboard overlay (and its in-memory override) so the operator picks provider (claude | codex/openai), with model tier remaining a separate concern. Unclear whether the override should persist for the session, per-item, or write back to config — surface this as an open question in the plan phase.
