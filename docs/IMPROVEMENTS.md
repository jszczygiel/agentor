# Out-of-scope improvements

Running log of issues noticed during agentor runs but deferred to stay within
the current task's scope.

## Open

- `tests/test_config.py` has three unused-import F401 ruff errors (`ReviewConfig`,
  `ParsingConfig`, `SourcesConfig` on lines 9-10). CI runs `ruff check` so these
  should already be failing the workflow — check whether the CI config ignores
  these or whether the suite was pre-broken before ruff was wired in.
