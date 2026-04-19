---
title: Enforce Read offset/limit on large files via pre-tool hook
category: feature
state: available
---

The system prompt already says *"For files >10k tokens use `Read` with `offset`/`limit`"* (`agentor/config.py:65-67`), but the advisory doesn't stick. A 2026-04-19 review of `/Users/szczygiel/StudioProjects/lancelot/.agentor/transcripts/` (93 logs, 48 sessions, $187.21 total spend) found 118 distinct files re-read within a single session, with the worst offender Reading `game_world.gd` (1,291 lines) **14 times in one run** and dumping it whole twice. Top chronic offenders are all large entity/autoload scripts: `economy_manager.gd` (1,485 lines), `city.gd` (1,382), `game_world.gd` (1,291), `shipyard_system.gd` — every whole-file Read burns ~15k tokens even when cached, and the creation side of the cache isn't free.

Replace the advisory text with an **enforced PreToolUse hook** written to `.agentor/system-prompt.txt` companion hook config (or injected via `--settings`). Hook rejects a `Read` tool call when:

1. The target path's line count exceeds a threshold (start with 400 lines; configurable via `agent.large_file_line_threshold`), AND
2. The call has no `offset` *and* no `limit`.

Rejection message should return the file's line count plus a suggestion: *"File is N lines. Read a narrow range (`offset`/`limit`) or Grep for the relevant symbol first."* The agent then re-issues a scoped Read, which is what the prompt already asks for.

Scope:

- Threshold applies to any path, not a hardcoded allowlist — the god-scripts change over time (the lancelot monolith-split just landed for three of them, so hardcoded paths would be immediately stale).
- Do NOT block Reads that already pass `offset` or `limit`, even on huge files — those are the intended path.
- Do NOT block Reads of small files regardless of flags (no friction where it isn't needed).

Verification:

- Unit test: synthesize a hook-style tool-input payload for a 500-line file with no offset/limit; assert reject. Same payload with `offset=1, limit=100`; assert allow. 200-line file; assert allow regardless.
- Re-run the 2026-04-19 worst session (`d0966d8e680f.execute`) with the hook enabled in a shadow mode and confirm the 14 excess reads drop to ≤1.

Evidence file: `/Users/szczygiel/StudioProjects/lancelot/tmp/agentor_analysis_2026-04-19.md`.
