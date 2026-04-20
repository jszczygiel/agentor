---
title: Break token usage panel down by model tier
state: available
category: feature
---

The dashboard currently aggregates token usage across all models in the 5h / today / last-7-days windows (see `_render_token_panel` and `aggregate_token_usage` in `agentor/dashboard/formatters.py`). Split each window into per-tier rows — small (haiku), medium (sonnet), large (opus) — so operators can see where spend concentrates. Tier resolution should go through the active provider's alias map (`Provider.model_to_alias` in `agentor/providers.py`) rather than hardcoding claude model id prefixes, since Codex contributes its own mini/full tiers. Models that don't resolve to a known alias should fall into an "other" bucket rather than being dropped. Note that `_token_windows` is a 2s TTL cache keyed on `(id(store), daemon_started_at)` — the per-tier breakdown must be computed inside the cached aggregate, not layered on top, to avoid re-scanning `items` per tick.
