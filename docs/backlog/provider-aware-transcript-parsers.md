---
title: Provider-aware transcript parsers (primer, activity feed)
state: available
tags: [refactor, multi-provider, dashboard]
---

Transcript walkers are written to Claude's stream-json vocabulary:

- `agentor/transcript.py` iter_events header comment: "The claude-code
  CLI emits one JSON event per line".
- `dashboard/transcript.py:92` `_session_activity` matches event types
  `assistant` / `tool_use` / `result`.
- `agentor/resume_primer.py` `build_primer` reads Claude Read/Grep tool
  names to build the "don't re-fetch these files" primer.

Codex emits `thread.started` / `turn.started` / plain message events
— none of this vocabulary matches, so Codex items:
- get no primer on resume (re-pays discovery cost on every kill-resume),
- show an empty activity feed in the inspect view.

Move parser ownership onto the provider:

```python
class Provider:
    def parse_events(self, path: Path, tail_bytes: int) -> Iterator[Event]: ...
    def build_primer(self, transcript: Path) -> str | None: ...
    def activity_feed(self, events: Iterable[Event]) -> list[FeedLine]: ...
```

`Event` is a provider-agnostic dataclass
(`kind: Literal["turn_start","tool_use","message","result"]`,
`tool: str | None`, `content: str | None`, `usage: dict | None`).
Dashboard renders feed lines without knowing which provider produced
them. `resume_primer.build_primer` relocates to the Claude provider
implementation; a Codex-side primer can be built from `turn.started`
sequences once the transcript format is settled.

Related: "Deduplicate transcript parsing between dashboard and
tools/" — both can consume the new `Event` stream.
