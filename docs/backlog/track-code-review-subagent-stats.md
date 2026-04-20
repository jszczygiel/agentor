---
title: Track code-review subagent stats and add config section
state: available
category: feature
---

## Context

Today agentor tracks **no stats about the code review step**. The execute-phase prompt in `agentor/config.py:154-157` suggests agents may *optionally* delegate to a `code-reviewer` subagent when the diff touches ≥3 files, but nothing enforces it, measures it, or records whether it happened:

- `ReviewConfig` in `agentor/config.py:334` only holds dashboard `port` / `notify` (unrelated).
- `awaiting_review` is the human approval gate — a different concept.
- No SQLite columns, transition notes, dashboard panels, or `result_json` fields capture code-review activity.
- `tools/analyze_transcripts.py` can count tool uses but has no code-review-specific extraction.

Goal: passively observe whether each execute run invoked the `code-reviewer` subagent, count findings, surface it on the dashboard and in transition notes. Opt-in enforcement (hard-block merge when review is expected but missing) lives behind a flag so existing runs are undisturbed.

## Approach

Four-part change, smallest-first so each lands testable on its own.

### 1. Transcript → stats extractor (`agentor/review_stats.py`, NEW)

```python
def extract_review_stats(transcript_path: Path, subagent_name: str) -> dict | None
```

- Walks `iter_events(transcript_path, tail_bytes=None)` (bounded per-run transcripts; whole-file read is fine here unlike the 500ms dashboard tick).
- Filters `ToolCall` where `name == "Task"` and `input["subagent_type"] == subagent_name`.
- Pairs each with its matching `ToolResult` via `tool_use_id` — same idiom as `agentor/dashboard/transcript.py:170,207`.
- Returns:
  ```python
  {
      "calls": int,
      "findings_total": int,       # lines matching "Must-Fix:" or "Suggestion:"
      "must_fix": int,             # subset starting with Must-Fix
      "duration_ms": int,          # summed across calls
      "tokens": {input, output, cache_read, cache_creation},
  }
  ```
- Returns `None` when no calls detected (dashboard aggregator skips → row shows as "no review run").

Findings parsing deliberately matches the two markers the `code-reviewer` subagent prompt uses — no JSON contract to break; worst case `findings_total=0` and we still record that a review happened.

### 2. Runner hook → `result_json["review_stats"]`

In `agentor/runner.py:640-651` (where `result_json` is assembled post-execute):

- Call `extract_review_stats(transcript_path, config.agent.review.subagent_name)` on successful execute only.
- Stash under `result_json["review_stats"]` when non-None.
- Plan-phase transcripts skipped — plan is read-only, `code-reviewer` wouldn't be invoked there by convention.
- `_publish_live` (runner.py:1260-1276) NOT touched — stats are terminal-only.

### 3. Config section (`agentor/config.py`)

New nested dataclass under `AgentConfig`:

```toml
[agent.review]
enabled = true                    # parse execute transcripts for subagent calls
subagent_name = "code-reviewer"   # match Task.input.subagent_type
min_files_for_review = 3          # threshold where review is "expected"
require = false                   # opt-in hard-block when expected but missing
```

- `enabled = false` → skip extraction entirely (zero overhead).
- `require = true` adds a gate in `committer.approve_and_commit` next to the existing `require_agent_log` gate: when `len(files_changed) >= min_files_for_review` AND (`review_stats is None` or `calls == 0`), transition to CONFLICTED with `last_error = "code review missing"` and chain through `resubmit_conflicted(force_execute=True)` — same pattern documented in the `require_agent_log` gotcha.
- Default `require = false` mirrors `require_agent_log` default so adoption is opt-in.
- Keep the existing top-level `[review]` block (port/notify) unchanged — new knobs scoped under `[agent.review]` to avoid overloading the name.

### 4. Dashboard panel

Clone the token-panel idiom:

- Add `_review_windows()` alongside `_token_windows()` in `agentor/dashboard/formatters.py:310`. Same 2s TTL cache, same `(id(store), daemon_started_at)` key. Aggregate across MERGED items in the current daemon session:
  - `runs_with_review / runs_total` (and percentage)
  - `avg_findings`, `total_must_fix`
  - `review_token_cost` (sum of `review_stats.tokens`)
- Add `_fmt_review_row()` next to `_fmt_token_row()` (formatters.py:396-413):
  ```
  review  12/18 runs (67%)  41 findings  3 must-fix
  ```
  Narrow tier: `rev 12/18 67%  3MF`.
- Render in `agentor/dashboard/render.py:203` directly under the token panel. Thread pre-computed windows through positionally — same contract pattern as `_render_token_panel` (flagged in CLAUDE.md gotchas).

Transition-note suffix (parallel to `, no agent-log written`): when `enabled=true` and `review_stats is None`, append `, no review run` to the MERGED note so `git log` / `store.transitions_for` history is greppable for skip rate.

## Critical files

- `agentor/review_stats.py` — NEW; stdlib only.
- `agentor/config.py:306,334` — add `ReviewPolicyConfig` dataclass + `[agent.review]` parse.
- `agentor/runner.py:640-651` — populate `result_json["review_stats"]` on execute success.
- `agentor/committer.py` — opt-in hard-block next to `require_agent_log` gate; append `, no review run` note suffix on missing review.
- `agentor/dashboard/formatters.py:310,396` — aggregator + row formatter with 2s TTL cache.
- `agentor/dashboard/render.py:203` — panel render under the token panel.
- `tests/test_review_stats.py` — NEW. Fixture transcripts: no-call, single-call-clean, multi-call-with-must-fix, malformed.
- `tests/test_config.py` — cover `[agent.review]` parse + defaults.
- `tests/test_dashboard_render.py` — panel render fixture; patch `agentor.dashboard.render.curses.color_pair` (return `0`) and `curses.napms`; add a `_review_windows_invalidate()` helper to mirror the existing `_token_windows_invalidate()` setUp pattern.

## Reuse

- `agentor.dashboard.transcript.iter_events` — whole-file walk for bounded transcripts.
- `ToolCall.id` ↔ `ToolResult.tool_use_id` pairing — identical to `transcript.py:170,207`.
- `Store.aggregate_token_usage` / `_token_windows` cache pattern — copy shape for review windows.
- `committer.approve_and_commit` agent-log gate — copy the `require_agent_log` branch structure verbatim.
- `tools/analyze_transcripts.py:47` — already walks `iter_events` counting tool use; verify the Task-tool name assumption by running it against a recent transcript before shipping.

## Verification

1. Unit tests:
   ```
   python3 -m unittest tests.test_review_stats -v
   python3 -m unittest tests.test_config -v
   python3 -m unittest tests.test_dashboard_render -v
   ```
2. Live fixture: find a recent `.agentor/transcripts/*.log` that contains `Task` subagent calls (use `tools/analyze_transcripts.py` to locate one) and feed it to `extract_review_stats` directly — assert non-None with correct counts.
3. End-to-end dogfood: flip `[agent.review] enabled = true` in the self-hosted `agentor.toml`, queue a trivial ≥3-file backlog item ("rename a widely-used helper"), prompt the agent to delegate to `code-reviewer`, observe the new dashboard row populate after MERGED.
4. Opt-in enforcement: set `require = true`, queue an item that skips review, confirm CONFLICTED transition with `last_error = "code review missing"` and chain-through into QUEUED with `force_execute=True` (same invariant as existing conflict path).
5. Regression: `python3 -m unittest discover tests -v` — watch headless curses tests don't regress from the new panel row.

## Gotchas surfaced by prior runs

- `_render_token_panel` and the new review panel must both receive pre-computed windows positionally to avoid re-hitting the TTL cache twice per tick (token-panel gotcha in CLAUDE.md applies).
- Headless curses render tests need `agentor.dashboard.render.curses.color_pair` and `curses.napms` patched — use `tests/test_dashboard_render.py::TestPriorityGlyph` as the template.
- Python 3.11 target — no nested f-strings reusing the same quote char (PEP 701 is 3.12+).
- The stats extractor MUST NOT raise on malformed transcripts; a partial stream-json file should return `None` or a best-effort dict, never break the runner's `result_json` write.
