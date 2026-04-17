# Second merge of main into enter-opens-pickup-review — 2026-04-17

## Surprises
- Branch needed a second merge pass because main kept advancing during review (`081df82` feat: row cursor nav / `[e]` resubmit, `9e6d9be` refactor: committer summary). Conflict surface was small this time — two markers in `__init__.py`, both textual overlap with our enter-handler rewrite.

## Gotchas for future runs
- Each time our branch sits in review, rebased-or-merged integration tries and fails: main's main file of interest (`agentor/dashboard/__init__.py`) is a hot file that attracts keybinding edits. Expect conflicts on every round-trip; keep the enter-handler diff minimal so resolution stays trivial.

## Follow-ups
- Pre-existing mypy error `agentor/dashboard/modes.py:760` (`_run_with_progress` lambda tuple-subscript returning None) continues to exist on main; out of scope here.
