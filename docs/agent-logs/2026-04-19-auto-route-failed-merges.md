# Auto-route failed merges back to agent — 2026-04-19

## Surprises
- `git_ops.merge_feature_into_base` already routes every non-zero failure (true merge/rebase conflicts, rebase aborts, CAS `update-ref` races) through the same `(None, summary)` return — no trigger-expansion code change was needed to cover "non-conflict merge failures" from the backlog brief.

## Gotchas for future runs
- `tests/test_committer.py::_mk_config` is a fixture helper shared across ~10 test classes; flipping the production `GitConfig.auto_resolve_conflicts` default silently broke every class that asserts a terminal CONFLICTED state via `_to_conflicted()`. Pin fixture behavior (`auto_resolve_conflicts=False`) at the fixture level rather than in each class setUp, and document the pin inline so the next default flip doesn't chase it test-by-test.
- Production defaults are now covered by `tests/test_config.py::test_auto_resolve_conflicts_default_on` and `test_minimal_config_uses_defaults` — assert production defaults against `GitConfig()` / `load()`, not via the committer-test fixture.
