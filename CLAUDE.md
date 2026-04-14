# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Agentor orchestrates Claude Code agents that consume work items from a target project's markdown files (backlog, ideas), develop each item in its own git worktree, and ping the user for review+approval before committing. It is configured **per target project** — agentor itself is the orchestrator, the work happens in some other repo pointed at by `[project].root` in the config.

## Commands

```bash
# Run tests (stdlib unittest, no deps)
python3 -m unittest discover tests -v

# Run a single test module / class / method
python3 -m unittest tests.test_extract -v
python3 -m unittest tests.test_extract.TestCheckboxMode -v
python3 -m unittest tests.test_extract.TestCheckboxMode.test_unchecked_item_extracted -v
```

Python 3.11+ is required (uses `tomllib`). No third-party dependencies in the MVP — stdlib only (`tomllib`, `sqlite3`, `pathlib`, `hashlib`, `re`).

## Architecture

Pipeline: **watched markdown files → extracted Items → SQLite queue → agent pool → worktree → awaiting_review → user approve/reject → commit in worktree → cleanup**.

Key modules:

- `agentor/models.py` — `Item` (immutable, identified by sha1 of `source_file+title+body`) and `ItemStatus` lifecycle: `queued → working → awaiting_review → approved|rejected → merged|cancelled`.
- `agentor/config.py` — loads `agentor.toml`. Project root is resolved relative to the config file's directory unless absolute. Important knobs: `agent.pool_size` (max concurrent agents, default 1 — strict serial), `sources.watch` (glob list of markdown files), `parsing.mode` (`checkbox` or `heading`).
- `agentor/extract.py` — parses markdown into Items. Two modes:
  - **checkbox**: each `- [ ]` at any indent is an item; indented continuation lines until the next checkbox at same-or-shallower indent form the body. `- [x]` items are skipped.
  - **heading**: each `#`..`######` is an item; body extends until the next heading of same-or-higher level (so subsections are included).
  - Inline `@key:value` tags are stripped from title/body and collected into `item.tags`. Title tags win on conflict.

Design invariants to preserve:

- **Item IDs must be stable across runs** — the daemon diffs parsed items against SQLite to detect new/removed work. Changing the hash input breaks deduplication.
- **Path resolution**: `extract_items` calls `.resolve()` on both the source file and project root before `relative_to` to handle macOS `/tmp` → `/private/tmp` symlinks. Don't drop this.
- **Agent pool is enforced at the scheduler, not in config**: pool_size just caps `COUNT(status='working')`. Bumping the number requires no code changes.
- **Per-project scope**: agentor is invoked against one project dir; it never spans multiple repos in a single run.

## Status of the MVP

Built: models, config loader, extractor, extractor tests. Not yet built: SQLite state store, file watcher daemon, agent runner (Claude Agent SDK or headless CLI), review web UI, committer. When adding these, keep stdlib-only unless a dep pulls real weight (e.g. `watchdog` for fs events, `fastapi` for review UI).
