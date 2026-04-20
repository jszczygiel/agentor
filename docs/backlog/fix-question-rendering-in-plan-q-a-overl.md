---
title: Fix question rendering in plan Q&A overlay
state: available
category: polish
---

The plan-phase question/answer interface truncates questions to a single line, cutting off longer prompts. Questions should wrap across multiple lines so the operator can read the full text before answering. The reviewer surface for plan questions lives in the curses dashboard (see `agentor/dashboard/render.py` and `modes.py`, where plan review prompts are rendered). Multi-line reflow already exists for prompt input via `_prompt_multiline`, so the fix likely means applying similar wrapping to the question display path rather than a single-line draw. Verify actual behavior before settling on an approach — confirm whether the truncation is a hard-coded single-line render or a width-calc bug.
