# Enforce head_limit on content-mode Greps — 2026-04-19

## Surprises
- Paired backlog item `enforce-read-offset-limit-on-large-files` landed
  on main *while this branch was being planned*. Main gained
  `agentor/read_hook.py`, `AgentConfig.large_file_line_threshold`, and a
  `write_claude_settings` + `--settings {settings_path}` wiring. The
  original plan's `.claude/settings.local.json` approach was abandoned
  in favour of reusing the already-merged plumbing.
- Grep's default `output_mode` is `files_with_matches`, not `content`.
  Enforcement simplifies to a single explicit-content check; an absent
  `output_mode` key is already safe.

## Gotchas for future runs
- When a plan's "share plumbing with X" caveat fires, check whether X
  has already merged to main before starting. A pre-execute `git merge
  main` (fast-forward) saves a full re-architect mid-execute.
- New PreToolUse rules should be added as additional matchers in
  `write_claude_settings` (runner.py), not as a parallel settings
  mechanism. Each hook toggles via its own `AgentConfig` flag.
- Hook scripts follow the protocol established by `read_hook.py`: a
  pure `decide(payload)` returning `{permissionDecision, [reason]}`
  plus a CLI `main()` that wraps it as `hookSpecificOutput` JSON + exit
  code 2 on deny. Mirror the shape — the test harness in
  `tests/test_read_hook.py` / `tests/test_grep_hook.py` depends on it.

## Follow-ups
- None in scope. `tools/shadow_verify_grep_hook.py` stays for operators
  wanting to replay future transcripts.

## Stop if
- Shadow-verify reports 0% rejection on known-bad transcripts (e.g.
  `cc9b8c6f4f4e.plan.log`) — means `iter_events` tool pairing or the
  Grep input schema drifted.
