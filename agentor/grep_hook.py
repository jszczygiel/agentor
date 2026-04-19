"""PreToolUse hook for Claude Code that blocks content-mode `Grep` calls
which forgot to cap results. Invoked by the Claude CLI as
`python3 <path>/grep_hook.py`; the Claude runner wires it in via a
generated settings JSON alongside the Read hook.

Protocol mirrors `read_hook.py`: reads a JSON event from stdin, emits a
`hookSpecificOutput.permissionDecision` JSON on stdout, exits 2 with a
human-readable reason on stderr when denying so the Claude CLI surfaces
it verbatim regardless of JSON support.

Decision rules:
- tool_name != "Grep" → allow (defensive; matcher should already scope it).
- tool_input not a dict → allow.
- output_mode != "content" → allow (Grep defaults to files_with_matches,
  and count/files_with_matches are already bounded).
- head_limit absent → deny with the rejection message.
- head_limit present (any value) → allow; trust agent judgment on the cap.
"""

import argparse
import json
import os
import sys

_REJECTION = (
    "Content-mode Grep must pass `head_limit` (default 50) — "
    "omit only with `output_mode: count` or `files_with_matches`."
)


def decide(payload: dict, enabled: bool = True) -> dict:
    """Return a hookSpecificOutput dict: `{permissionDecision, reason}`."""
    if not enabled:
        return {"permissionDecision": "allow"}
    if payload.get("tool_name") != "Grep":
        return {"permissionDecision": "allow"}
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return {"permissionDecision": "allow"}
    if tool_input.get("output_mode") != "content":
        return {"permissionDecision": "allow"}
    if "head_limit" in tool_input:
        return {"permissionDecision": "allow"}
    return {"permissionDecision": "deny", "reason": _REJECTION}


def _resolve_enabled(cli_disable: bool) -> bool:
    if cli_disable:
        return False
    env = os.environ.get("AGENTOR_GREP_HOOK")
    if env is not None and env.strip().lower() in ("0", "false", "off"):
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--disable", action="store_true",
        help="Allow every call; useful for smoke-testing.",
    )
    args = parser.parse_args(argv)
    enabled = _resolve_enabled(args.disable)

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        # Malformed input: fail open, don't wedge the agent on our own bug.
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }))
        return 0

    result = decide(payload, enabled=enabled)
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": result["permissionDecision"],
        }
    }
    if "reason" in result:
        output["hookSpecificOutput"]["permissionDecisionReason"] = result["reason"]
    sys.stdout.write(json.dumps(output))
    if result["permissionDecision"] == "deny":
        sys.stderr.write(result.get("reason", "Grep denied by hook."))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
