---
title: Show suggested execute model in inspect view
state: available
category: feature
---

Inspect mode currently surfaces item metadata, transitions, and transcript tail, but does not display the plan-nominated execute tier. The plan parser extracts `suggested_model` via `runner._parse_execute_tier` and the resolved value lands on `result_json["execute_model"]` / `result_json["execute_model_source"]` after execute dispatch. Render both in the inspect overlay (`agentor/dashboard/render.py` / `modes.py`) so operators can see what the plan suggested and what actually got used (tag / plan / default), especially useful before approving a plan. For AWAITING_PLAN_REVIEW items only the suggestion is available; for post-execute items show both suggestion and resolved `(alias, source)`.
