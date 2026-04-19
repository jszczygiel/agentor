# Deduplicate transcript parsing — 2026-04-17

## Surprises
- `dashboard.py` was already split into a `dashboard/` package (see recent commit `4abfdc1`); transcript helpers lived in `dashboard/transcript.py`, not the monolithic file the backlog entry named.
- Byte-identical output from both `tools/analyze_*.py` against `~/StudioProjects/lancelot/.agentor/transcripts` after refactor — shared walker matched the original loops faithfully.

## Gotchas for future runs
- Scripts under `tools/` run as files, not modules, and didn't previously import from `agentor`. Adding a shared module forced a `sys.path.insert(0, <repo_root>)` bootstrap at the top of each script — worth remembering for any future `tools/ → agentor/` dependency.
- Backlog items may reference `agentor/dashboard.py`: the package split means grep for symbols, not filenames.

## Follow-ups
- None.
