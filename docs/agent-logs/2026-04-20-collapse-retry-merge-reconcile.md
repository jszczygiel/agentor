# Collapse retry-merge/resubmit — reconcile with main — 2026-04-20

## Surprises
- Main landed `auto_resolve_conflicts = True` default (commit `0f3de38`) while this branch was in review, collapsing the two concerns onto the same comment block in `agentor/config.py`. The semantic conflict (flip-on vs `[e]` removal) is compatible: default-on makes `[m]` a fallback for opt-out operators, which matches the collapse intent.

## Gotchas for future runs
- `_inspect_dispatch`'s lazy committer imports auto-merge cleanly when main doesn't touch the same lines — even if the surrounding action map changed under the merge. Trust the three-way on action-table rows; re-apply the small comment diff by hand.
- Stale `[m]` / `[e]` doc references live in CLAUDE.md (module intro line) and in `tests/test_committer.py` test-case docstrings — easy to miss because they sit far from the dashboard code the collapse actually removes. Sweep with `grep -n '\[e\]' CLAUDE.md tests/` after any future UI binding change.

## Follow-ups
- None in scope.
