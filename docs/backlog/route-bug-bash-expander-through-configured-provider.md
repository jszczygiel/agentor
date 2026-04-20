---
title: Route bug-bash note expander through configured provider
state: available
tags: [refactor, multi-provider, dashboard]
---

`dashboard/modes.py:811` `_expand_note_via_claude` shells to the
`claude` CLI verbatim — argv at `:821` is
`["claude", "-p", prompt, "--dangerously-skip-permissions"]`. The
project's `agent.runner` / `agent.model` is ignored.

For a project running `runner = "codex"`, bug-bash note expansion
still requires `claude` installed on PATH (or the action errors with
"claude CLI not found"). Same concern if/when a third provider lands.

Fix: route through the active provider's one-shot invocation:

```python
class Provider:
    def invoke_one_shot(self, prompt: str, timeout: float) -> str:
        """Run provider, return final message. No session, no worktree,
        no transcript — used for ephemeral tasks like note expansion."""
```

Claude implementation reuses `claude -p` + `--dangerously-skip-permissions`.
Codex implementation uses `codex exec` with a tmp output-path. Rename
`_expand_note_via_claude` → `_expand_note`.

Small ticket (~60 LOC change) but gated on the Provider interface
ticket landing first.
