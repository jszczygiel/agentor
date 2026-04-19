---
title: Document prior-run gotchas in CLAUDE.md
state: available
category: docs
---

`docs/agent-logs/2026-04-17-*.md` contain hard-won lessons that future
agents keep rediscovering. Promote the durable subset into `CLAUDE.md`
under a new "Gotchas from prior runs" section so they survive log
rotation and land in fresh-context agent runs automatically.

Candidate items (verify each is still accurate before adding):

- Backlog `file:line` refs may be stale — `dashboard.py` is now a
  package; grep symbols, not filenames.
- `last_error` is capped at 4000 chars in `Store.transition`. When
  composing structured summaries in `committer.py`, cap each section
  independently — the outer cap silently chops the trailing section.
- Status SQLite boundary is centralized via `_encode_status` /
  `_decode_status` in `store.py`. Don't sprinkle `.value` at new
  callsites. `Store.transitions_for` returns `Transition` dataclasses
  (`from_status`, `to_status`, `note`, `at`) — not dicts.
- `tools/` scripts run as files, not modules. Importing from `agentor`
  requires `sys.path.insert(0, <repo_root>)` at the top of the script.
- Mode functions in `dashboard/modes.py` use lazy
  `from ..committer import …` inside function bodies to sidestep a
  circular import via `store`. Preserve the pattern in new actions.
- `requires-python = ">=3.11"` — nested f-strings with the same quote
  char are PEP 701 (3.12+) only. Pin ruff `target-version = "py311"`
  and mypy `python_version = "3.11"` so devs on 3.13 don't ship code
  CI can't parse.
- Merge shortcut: when main grows a cosmetic refactor (reorders,
  banners) and the branch edits the same fns, prefer
  `git checkout --theirs -- <file>` then re-apply semantic deltas. Git's
  recursive merge has corrupted `store.py` beyond the conflict markers.

Keep the new section telegraphic to match the rest of `CLAUDE.md` style.
