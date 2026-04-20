---
title: Validate `agent.command` placeholders per provider
state: available
tags: [refactor, multi-provider, config]
---

`agent.command` / `agent.resume_command` are a single list-of-strings
knob shared across providers. Supported placeholders —
`{prompt}`, `{model}`, `{output_path}`, `{session_id}`,
`{settings_path}` — are a de-facto union of what Claude and Codex each
accept:

- `{settings_path}` — Claude only (the per-item PreToolUse hook file).
- `{output_path}` — Codex only (`--output-path` / `-o`).
- `{session_id}` — Codex resume-template only (Claude's session id
  goes via `--resume {id}` appended at runtime, not templated in).

An operator copying a Claude command template and swapping
`runner = "codex"` gets a silently-broken invocation because the
placeholder set is wrong. Inverse also true.

Give each `Provider` a declared placeholder schema:

```python
class Provider:
    required_placeholders: frozenset[str]
    optional_placeholders: frozenset[str]
```

`Config.__post_init__` validates `agent.command` against the active
provider's schema: every required placeholder present, no foreign
placeholders (error with a clear "`{settings_path}` is Claude-only"
message). Soft-warn when optional placeholders are missing (matches
the existing `{settings_path}`/`{model}` opt-out pattern — the
override still works, but the operator sees what they gave up).

Touch points: `config.py` (validation), `runner.py:1832+` default
template functions (move to per-provider static methods).
