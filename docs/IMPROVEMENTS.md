# Out-of-scope improvements

Running log of issues noticed during agentor runs but deferred to stay within
the current task's scope.

## Open

- `tests/test_config.py` has three unused-import F401 ruff errors (`ReviewConfig`,
  `ParsingConfig`, `SourcesConfig` on lines 9-10). CI runs `ruff check` so these
  should already be failing the workflow — check whether the CI config ignores
  these or whether the suite was pre-broken before ruff was wired in.
- When `git.auto_resolve_conflicts` chains a CONFLICTED item back into QUEUED,
  the dashboard inspect view shows no explicit signal that the re-queue was
  automatic. Consider tagging the transition note (or surfacing an auto-resolve
  badge in the main table) so operators can distinguish a human `[e]` resubmit
  from a committer-driven one.
