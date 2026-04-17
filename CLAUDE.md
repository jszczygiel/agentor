# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Agentor orchestrates Claude Code agents that consume work items from a target project's markdown files (backlog, ideas), develop each item in its own git worktree, and ping the user for review+approval before committing. It is configured **per target project** — agentor itself is the orchestrator, the work happens in some other repo pointed at by `[project].root` in the config.

## Projects using agentor

- **lancelot** — `/Users/szczygiel/StudioProjects/lancelot` (Godot project; `agentor.toml` lives at repo root).

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

Pipeline: **watched markdown files → extracted Items → SQLite queue → daemon dispatches to agent pool → per-item worktree → plan phase → awaiting_plan_review → execute phase → awaiting_review → user approve/reject → commit (and optionally merge) → cleanup**.

Key modules:

- `agentor/models.py` — `Item` (immutable, sha1 of `source_file+title+body`) and `ItemStatus` lifecycle: `backlog → queued → working → awaiting_plan_review → working → awaiting_review → merged | rejected | errored | conflicted | cancelled | deferred`.
- `agentor/config.py` — loads `agentor.toml`. Project root resolves relative to the config file's directory unless absolute. Knobs worth knowing: `agent.pool_size` (caps concurrent `working` items; default 1), `agent.runner` (`stub` | `claude` | `codex`), `agent.pickup_mode` (`auto` | `manual`), `agent.single_phase` (skip the plan phase), `sources.watch` (glob list), `parsing.mode` (`checkbox` | `heading` | `frontmatter`), `git.merge_mode` (`merge` | `rebase`).
- `agentor/extract.py` — parses markdown into Items. Modes:
  - **checkbox**: each `- [ ]` is an item; continuation lines form the body; `- [x]` skipped.
  - **heading**: each `#`..`######` is an item; body runs until the next heading of same-or-higher level.
  - **frontmatter**: one item per file; title/state/tags taken from YAML frontmatter.
  - Inline `@key:value` tags are stripped from title/body into `item.tags`. Title tags win on conflict.
- `agentor/store.py` — SQLite state store. Atomic `claim_next_queued`, `pool_has_slot`, history via the `transitions` table, failure rows via `failures`. All transitions go through `Store.transition(to, **fields)`.
- `agentor/watcher.py` — `scan_once` diffs the markdown-extracted items against the DB and inserts new ones at BACKLOG (manual pickup) or QUEUED (auto pickup).
- `agentor/daemon.py` — main loop. Polls the watcher, dispatches queued items into a thread pool capped by `pool_size`, surfaces infra failures as sticky alerts on the dashboard until the user presses `u`.
- `agentor/runner.py` — `Runner` base class + `StubRunner`, `ClaudeRunner`, `CodexRunner`. Two-phase flow: `plan` (read-only; stops at AWAITING_PLAN_REVIEW) → human approves via `approve_plan` → `execute` resumes the same session (`--resume <session_id>` for claude, `thread_id` for codex) and commits. `single_phase=true` skips plan and goes straight to execute.
- `agentor/committer.py` — handles AWAITING_REVIEW → MERGED. `approve_and_commit` commits any uncommitted work on the feature branch, then integrates into `git.base_branch` via an ephemeral detached worktree (`merge` → `--no-ff`, `rebase` → `rebase <base>`-then-CAS-fast-forward). Conflicts transition to CONFLICTED with the summary in `last_error`; `retry_merge` re-runs the integration after the user resolves in the feature worktree.
- `agentor/recovery.py` — runs at daemon startup. WORKING items with a live `session_id` + worktree go back into resumable; everything else reverts to its previous settled status. Also clears benign stale `last_error` markers.
- `agentor/dashboard.py` — curses UI. Main table auto-refreshes at `REFRESH_MS=500`; modes: pickup `p`, review `r`, deferred `d`, inspect `i`. Inspect auto-refreshes every 1s and parses the claude stream-json transcript into a session-activity feed; `[m]` retries merge for CONFLICTED items.

Design invariants to preserve:

- **Item IDs must be stable across runs** — the daemon diffs parsed items against SQLite to detect new/removed work. Changing the hash input breaks deduplication.
- **Path resolution**: `extract_items` calls `.resolve()` on both the source file and project root before `relative_to` to handle macOS `/tmp` → `/private/tmp` symlinks. Don't drop this.
- **Agent pool is enforced at the scheduler, not in config**: pool_size just caps `COUNT(status='working')`. Bumping the number requires no code changes.
- **Per-project scope**: agentor runs against one project dir; it never spans multiple repos in a single run.
- **Worktrees start from the current tip of `git.base_branch`** — `worktree_add` passes the branch name, so git resolves the sha at dispatch time. Resumed worktrees (plan → review → execute) run `fast_forward_to_base` before `do_work` to pull in any base-branch commits that landed during the review gap; if the feature has diverged (agent committed during plan), ff refuses and we fall through silently so the final integration step handles the divergence.
- **Auto-merge never touches the user's checkout of base_branch** — `merge_feature_into_base` always works in a `--detach`ed temp worktree and CAS-advances the ref via `update-ref OLD NEW`.
- **Feedback is consumed once**: the runner's `_prepend_feedback` reads `item.feedback`, injects it into the next prompt, and clears the column so a future run starts clean.

## No-deps policy

Stdlib only (`tomllib`, `sqlite3`, `pathlib`, `hashlib`, `re`, `curses`, `threading`, `json`). Adding a third-party dep should be a considered decision — the current shape works without any.
