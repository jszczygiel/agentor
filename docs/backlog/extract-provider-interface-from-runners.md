---
title: Extract `Provider` interface; collapse ClaudeRunner/CodexRunner duplication
state: available
tags: [refactor, multi-provider, architecture]
---

`Runner` base class in `agentor/runner.py:358` only abstracts `do_work`. Both
subclasses duplicate `_do_plan` / `_do_execute` / `_prepend_feedback`
(literally copy-pasted — compare `:869-898` vs `:1209-1229`) plus the
feedback → plan-answers → tier-resolution wiring.

Refactor: make `Runner` concrete, owns plan/execute orchestration, feedback
consumption, tier resolution, `_list_changes`, and per-phase transition
bookkeeping. Delegate to a `Provider` interface for the narrow
provider-specific ops:

```python
class Provider(Protocol):
    name: str
    capabilities: ProviderCapabilities
    def invoke(self, ctx: InvokeCtx) -> InvokeResult: ...
    def default_command(self, resume: bool) -> list[str]: ...
    def is_dead_session_error(self, msg: str) -> bool: ...
    def build_primer(self, transcript: Path) -> str | None: ...
```

`InvokeCtx` = `(prompt, worktree, phase, model, session_ref,
transcript_path, proc_registry, stop_event, timeout_s)`. `InvokeResult` =
`(result_text, envelope, session_ref)`.

Also replace `make_runner`'s string switch (`runner.py:1991`) with a
`ProviderRegistry.register(name, factory)` so adding a third provider is
one registration call, not a case in `make_runner`.

Out of scope for this ticket: envelope unification, alias tables,
capabilities flag work — tracked separately.
