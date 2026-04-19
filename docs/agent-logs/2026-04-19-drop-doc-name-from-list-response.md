# Drop doc name from list response — 2026-04-19

## Surprises
- Backlog source `docs/backlog/drop-doc-name-from-list-response.md` never existed in `git log --all` and was absent from the worktree at dispatch. Item was synthesized without a persisted source file — `git rm` not runnable.
- No code reference anywhere to "doc name" / "list response"; the operator note is paraphrase-only. Landed on dashboard main-table SOURCE column as the "list response" referent and executed candidate #1 of the approved plan.

## Gotchas for future runs
- When a ticket's source markdown is missing at dispatch, skip the mandatory `git rm` step rather than committing a phantom delete — verify presence before staging.
- Column-width constants live in `agentor/dashboard/formatters.py`; removing a column means deleting the constant AND its import in `render.py` or ruff/mypy gate will catch it.

## Follow-ups
- None. Inspect view still shows `source: path:line` (single-item detail, not a list) — unchanged by this pass.

## Stop if
- Operator disambiguates and "list response" means something other than the dashboard main-table SOURCE column — revert this commit and re-plan.
