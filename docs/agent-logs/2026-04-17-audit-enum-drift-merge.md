# Audit enum drift — merge resolution — 2026-04-17

## Surprises
- Git's recursive merge produced a malformed `agentor/store.py` even outside
  the `<<<<<<<` markers: `ids_with_errors` landed under the failures section,
  `recent_failure_notes` was duplicated, and methods were out of order
  relative to main's new `# --- section ---` layout. Resolving hunk-by-hunk
  would have left a mess — rewrote the file from main's layout + re-applied
  the enum-serialization diffs.

## Gotchas for future runs
- When main grows a cosmetic refactor (method reordering, banners) and your
  branch edits the same functions, prefer `git checkout --theirs -- file`
  then re-apply semantic deltas over `git merge`'s hunk resolver. It avoids
  the silent scramble.
- `mypy agentor` is clean; `ruff check agentor tests` is NOT — three F401s in
  `tests/test_config.py` predate this branch. See `docs/IMPROVEMENTS.md`.

## Follow-ups
- Unused-import ruff failures on main's `tests/test_config.py` logged to
  `docs/IMPROVEMENTS.md`. Out of scope for this branch.
