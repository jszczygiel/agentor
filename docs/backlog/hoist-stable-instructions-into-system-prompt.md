---
title: Hoist stable instructions into system prompt for prompt-cache reuse
state: backlog
tags: [runner, token-economy, prompt-cache]
---

## Why

Every `claude -p` invocation currently inlines ~2 KB of instructional text (token-economy rules, plan structure, execute steps, findings-log template) inside the per-run *user* prompt. Because the user prompt also carries `{title}/{body}/{source_file}/{plan}`, the whole prefix is unique per run — Claude's prompt cache almost never hits. Cross-item and plan→execute re-use is zero.

Commit `26666fa` (`feat(daemon): stagger dispatches to share system-prompt cache`) already added `agent.dispatch_stagger_seconds` anticipating a shared `--append-system-prompt-file` cache, but the plumbing to actually materialise and pass that file was never landed. This item closes that loop.

## What

1. `AgentConfig.system_prompt_template: str` (new field)
   - Contains all stable instructional text: token-economy rules, plan structure, execute steps (1-9), findings-log template, commit guidance.
   - No placeholders — fully static so the content hash stays stable across runs and the cache key doesn't rotate.
   - Empty string opts out (back-compat for users who customised prompts).

2. Shrink `AgentConfig.plan_prompt_template` to ~6 lines: just `PLANNING PHASE`, `{title}/{source_file}/{body}`, pointer to the system prompt for structure. Drop the duplicated token-economy rules and plan-structure list.

3. Split `AgentConfig.execute_prompt_template` into two narrower fields:
   - `execute_prompt_template_resume` — used on two-phase resume. No placeholders; title/body/plan are already in the resumed session history, re-sending them burns tokens.
   - `execute_prompt_template_single_phase` — used when `agent.single_phase=true`. Placeholders `{title}/{body}/{source_file}` only.
   Remove the old combined `execute_prompt_template` field. `agentor/config.py` `_WARN_FIELDS` should emit the standard unknown-key warning if a user still has it in their TOML.

4. `ClaudeRunner._ensure_system_prompt_file()` (new method)
   - Materialises `system_prompt_template` to `<project_root>/.agentor/system-prompt.txt`.
   - Writes only on content mismatch (read → compare → write) so the file's inode mtime stays stable and Claude's cache key doesn't churn.
   - Returns `None` when template is empty (opt-out path).

5. `ClaudeRunner._invoke_claude` passes `--append-system-prompt-file <path>` when the helper returns a path. (Also consider: mention in `CLAUDE.md` Design Invariants that this flag is load-bearing for cache hits; `agent.dispatch_stagger_seconds` relies on it populating first.)

6. Feedback placement flip: `_prepend_feedback` → `_append_feedback`. Feedback is volatile (present only on a rejected-attempt retry) — prepending busts the cache on every retry because the prefix changes. Appending keeps the stable prefix cache-eligible; only the tail varies.

7. Mirror changes in `CodexRunner` (`_do_plan`, `_do_execute`, `_append_feedback`). Codex `exec resume` replays prior turns same as Claude's `--resume`, so resume execute prompt stays tiny.

8. `CLAUDE.md` — add a line under Design Invariants: *"Stable instructions live in the system prompt: `AgentConfig.system_prompt_template` is materialised to `.agentor/system-prompt.txt` and passed to `claude -p` via `--append-system-prompt-file`. Per-run user prompts stay minimal … so Claude's prompt cache hits across items/phases/retries."* Also update the feedback-consumed-once invariant to say "appended at the tail of the next prompt (so the stable prefix stays cache-eligible)".

## Verification

- `tests/test_config.py::test_prompt_templates_default_shape` — asserts the system prompt is ≥800 chars, plan/resume user prompts are <400, resume has no placeholders, single-phase has `{body}`. Guards against future drift.
- `tests/test_config.py::test_old_execute_prompt_template_warns` — stale `execute_prompt_template = "..."` in TOML produces the unknown-key warning.
- `tests/test_runner.py` — assert `--append-system-prompt-file` appears in the argv for both plan and execute invocations; assert the file is materialised at `<root>/.agentor/system-prompt.txt`; assert `_append_feedback` places feedback at the tail (not head) of the prompt; assert a no-op re-run doesn't rewrite the file when content matches.
- Manual: run two items back-to-back with `agent.pool_size=1` (serial), inspect the second item's first-turn `usage.cache_creation_input_tokens` vs `cache_read_input_tokens` in the stream-json transcript — cache read should dominate.

## Non-goals

- Not touching the stagger logic (already in `26666fa`).
- Not refactoring the system-prompt content itself — this item is purely about *where* the text lives.
- Not adding a cache-hit-rate dashboard indicator (separate backlog item: `weekly-session-token-indicator.md`).

## Scope flags

- Stub runner unaffected — no `claude` binary in its path.
- `test_cmd` / `build_cmd` untouched.
- File lives at `.agentor/system-prompt.txt` (already a gitignored dir for daemon state). Don't commit the materialised file.

## Risks

- If the template ever contains dynamic content (env vars, timestamps), the cache key rotates every run — defeats the whole point. Keep it static.
- `claude --append-system-prompt-file` is an unstable CLI flag; if Anthropic renames it, the opt-out path (`system_prompt_template = ""`) must let operators fall back to the pre-refactor behaviour. Confirm `claude -p --help` still lists the flag on any Claude Code upgrade.
- Codex's equivalent flag (if any) may differ — verify by running the stub-free test suite against CodexRunner before claiming parity.

## Provenance note

This refactor was live in `/Users/szczygiel/StudioProjects/agentor/`'s working tree for several days (unstaged) before being filed as a backlog item. Several landed commits and downstream backlog items already reference the feature as if it existed (e.g. `enforce-head-limit-on-content-greps.md:7-8` cites `agentor/config.py:68-70` line numbers that match the WIP, not HEAD; `weekly-session-token-indicator.md:27` mentions `--append-system-prompt-file` as live; the stagger commit `26666fa` was written assuming the shared cache exists). Route this through the normal backlog → worktree → review → merge flow so provenance is recoverable from git history.
