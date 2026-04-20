---
title: Per-provider model alias tables (haiku/sonnet/opus are Claude-only)
state: available
tags: [refactor, multi-provider, config]
---

`_ALIAS_TO_MODEL` in `agentor/config.py:26-28` hardcodes
`haiku/sonnet/opus → claude-*` ids. `CodexRunner` reuses these aliases
via `_resolve_execute_tier` (`runner.py:1195`), so Codex dispatches
resolve to Claude model ids. `@model:haiku` on a Codex-routed item
silently pins a Claude model string that the Codex CLI then rejects
(or worse, accepts literally).

Move alias tables onto the provider:

```python
class ClaudeProvider(Provider):
    model_aliases = {"haiku": "claude-haiku-4-5", "sonnet": ..., "opus": ...}

class CodexProvider(Provider):
    model_aliases = {"mini": "o4-mini", "full": "gpt-5", ...}  # example
```

`_resolve_execute_tier` (`runner.py:1708`) takes the active provider's
alias map; `execute_model_whitelist` defaults to that map's keys.
`_parse_execute_tier` (`:1680`) gets the whitelist from the same
source.

`_model_to_alias` reverse lookup (`runner.py:1754`) moves to the
provider too — the `claude-(haiku|sonnet|opus)` regex is obviously
Claude-specific.

CLAUDE.md §"Plan can nominate the execute-phase model tier" needs the
caveat that alias vocabulary is per-provider.
