---
title: Abstract dead-session detection; recovery hardcodes Claude signature
state: available
tags: [refactor, multi-provider, recovery]
---

`recovery.py:12` defines a lowercased substring
(`"no conversation found"`) that identifies a dead Claude session.
`_is_dead_session_error` in `runner.py:183` is similarly Claude-only.
`agent.claude_session_max_age_hours` (`config.py`) is a Claude-
lifetime assumption.

Codex thread expiry — which DOES happen, threads aren't immortal — hits
the same code path but never matches the signature, so Codex dead
sessions stay stuck in QUEUED-with-ref and fail on every retry until
`max_attempts` exhausts them instead of refreshing the ref and
starting clean.

Move detection onto the provider:

```python
class Provider:
    def is_dead_session_error(self, msg: str) -> bool: ...
    def session_max_age_hours(self) -> int | None: ...
```

Recovery sweep (`recovery.py:136-158`) calls into the active
provider's predicate instead of the module-level substring.
`agent.claude_session_max_age_hours` becomes
`agent.session_max_age_hours` (generic knob, provider ignores if its
sessions don't expire on wall-clock).

Also sweep `_is_dead_session_error` callsites in `runner.py:501` so
the QUEUED-with-fresh-session demotion still fires for Codex.
