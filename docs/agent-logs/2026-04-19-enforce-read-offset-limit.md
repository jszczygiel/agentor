# Enforce Read offset/limit via PreToolUse hook — 2026-04-19

## Gotchas for future runs
- Claude CLI hook commands are executed via `sh -c`, so inline env assignment (`AGENTOR_READ_THRESHOLD=400 python3 …`) works — no need for a wrapper script.
- Hook payload matcher scoping isn't bullet-proof; the hook still defensive-checks `tool_name == "Read"` so the script is safe to reuse behind broader matchers later.
- Settings JSON must be valid even when hooks are disabled (`threshold=0`) — Claude errors if `--settings` points at a missing file. Write an empty `{"hooks": {}}` instead of skipping.

## Follow-ups
- Codex runner does not receive the hook (its hook model differs). If the prompt-level advisory proves insufficient for codex too, wire an equivalent once codex exposes PreToolUse.
- Consider also gating `Grep output_mode=content` without `head_limit` via the same mechanism — same token-economy class of problem.

## Stop if
- Claude CLI version in use rejects `--settings <path>` or the `PreToolUse` matcher schema — fall back to denying via exit code only and surface a config error; don't silently continue without enforcement.
