# Out-of-scope improvements

Running log of issues noticed during agentor runs but deferred to stay within
the current task's scope.

## Open

- Stale-session demotion in `recovery.py` clears `worktree_path` on the item
  but leaves the actual worktree directory on disk. `claim_next_queued` will
  overwrite the path with a fresh slug on re-dispatch, so the old worktree
  lingers until external cleanup (or the next conflicting `git worktree add`).
  Consider calling `git_ops.worktree_remove` from the stale-session branch the
  same way the dead-session-revert path does, gated on the worktree dir
  existing. Scope kept narrow for the original task.
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
- Audit for other stale `ItemStatus.BACKLOG` references across the codebase.
  `agentor/dashboard/render.py:_STATE_GLYPHS` carried a `BACKLOG: "B"` entry
  that broke every import after the enum member was removed on main — the
  responsive-dashboard branch and the remove-BACKLOG branch landed
  conflict-free via auto-merge but interacted badly. Fixed inline while
  reconciling the force-execute branch (needed to run the test suite), but a
  wider grep + mypy `strict_optional=True` + per-member enum exhaustiveness
  check should be considered so this class of drift is caught pre-merge.
