---
title: Abstract Read/Grep hook injection; today it's Claude-only
state: available
tags: [refactor, multi-provider, hooks]
---

`write_claude_settings` (`runner.py:1897`) builds a per-item JSON
registering `PreToolUse` hooks for `Read` and `Grep`, pointed at via
`--settings {settings_path}` in the default Claude command
(`:1863`). The settings JSON shape is Claude-CLI-specific.

`agent.large_file_line_threshold` and `agent.enforce_grep_head_limit`
are silently dead knobs when `agent.runner = "codex"`. Operators have
no feedback that their guardrails aren't applied.

Abstract as a provider hook:

```python
class Provider:
    def write_tool_guardrails(self, config, item_id) -> dict[str, str]:
        """Return {placeholder_name: value} to splice into the command
        template. Empty dict when the provider has no guardrail channel."""
```

Claude returns `{"settings_path": "<path>"}`. Codex returns `{}` AND
logs a warning once per daemon startup if either guardrail knob is set
with a non-default value — operators opt into Codex knowing that
guardrails are silent, but shouldn't discover it mid-review.

The bundled `read_hook.py` / `grep_hook.py` scripts are reusable — they
speak Claude's hook protocol but the script is just
stdin-JSON-in/JSON-out, so future providers with a hook channel can
adopt them by providing a different "register this hook" wrapper.
