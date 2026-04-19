# Serialize auto-merge to prevent base-branch races — 2026-04-19

## Surprises
- Initial Edit of the module header accidentally dropped the `_noop` helper; first test run failed with `NameError: name '_noop' is not defined`. Moral: when editing a tight header block, re-read surrounding lines rather than trusting the Edit's old_string boundary.

## Gotchas for future runs
- `tests/test_committer.py` uses `StubRunner` which writes a per-item `.agentor-note-<id[:8]>.md`, so two stubbed items land on disjoint files and produce naturally-clean merges — no extra scaffolding needed to exercise concurrent merges.
- Monkeypatching `git_ops.merge_feature_into_base` must patch both the `git_ops` module AND `agentor.committer.git_ops.merge_feature_into_base` — the committer does `from . import git_ops` and then `git_ops.merge_feature_into_base(...)`, so rebinding only at the module root wouldn't be enough if the committer had done `from .git_ops import merge_feature_into_base`. Current shape means the module-level rebind on `_git_ops` is sufficient, but patching both defensively makes the test robust against that import style changing.

## Follow-ups
- Module-level `_INTEGRATION_LOCK` is process-wide. Two daemon processes on the same repo still race on `update-ref`. Out of scope; kick to IMPROVEMENTS if it ever matters.

## Stop if
- Any new integration code path starts calling `git_ops.merge_feature_into_base` outside `committer.py` — it must either route through the committer or acquire `_INTEGRATION_LOCK` itself, otherwise the race is back.
