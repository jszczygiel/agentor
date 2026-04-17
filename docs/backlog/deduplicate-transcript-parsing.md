---
title: Deduplicate transcript parsing between dashboard and tools/
state: available
category: refactor
---

`agentor/dashboard.py` (`_session_activity`, `_brief_tool_input`,
`_tool_result_preview`) and `tools/analyze_transcripts.py` both walk the
claude stream-json transcript independently. Extract a shared
`agentor/transcript.py` module that yields structured events; dashboard and
tools consume it. Add tests covering the common event shapes.
