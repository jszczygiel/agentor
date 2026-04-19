# Out-of-scope improvements

Running log of issues noticed during agentor runs but deferred to stay within
the current task's scope.

## Open

- `CodexRunner` does not yet wire `CheckpointEmitter`. Codex uses its own
  JSONL event shape and `thread_id` resume semantics — the emitter module
  is runner-agnostic so a follow-up PR can gate on `_CodexStreamState`
  (no per-turn `output_tokens`, so a turn-count-only gate is the
  minimum-viable wiring). Scope kept to Claude for this task.
- `tests/test_config.py` has three unused-import F401 ruff errors (`ReviewConfig`,
  `ParsingConfig`, `SourcesConfig` on lines 9-10). CI runs `ruff check` so these
  should already be failing the workflow — check whether the CI config ignores
  these or whether the suite was pre-broken before ruff was wired in.
- When `git.auto_resolve_conflicts` chains a CONFLICTED item back into QUEUED,
  the dashboard inspect view shows no explicit signal that the re-queue was
  automatic. Consider tagging the transition note (or surfacing an auto-resolve
  badge in the main table) so operators can distinguish a human `[e]` resubmit
  from a committer-driven one.
- mypy reports `func-returns-value` in `agentor/dashboard/modes.py` around the
  `_capture_note_for_expansion` callsite — a `(p("…"), call())[-1]` tuple trick
  trips the checker because `p` returns None. Pre-existing; switch to an inner
  helper that calls `p(...)` then returns the real value to clear the warning.
