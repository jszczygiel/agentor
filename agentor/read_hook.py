"""PreToolUse hook for Claude Code that blocks whole-file `Read` calls on
large files. Invoked by the Claude CLI as `python3 -m agentor.read_hook`
(or direct path); the Claude runner wires it in via a generated settings
JSON.

Protocol: reads a JSON event from stdin, decides allow vs deny, emits a
`hookSpecificOutput.permissionDecision` JSON on stdout. On deny also exits
with code 2 and writes a human-readable reason to stderr, which the
Claude CLI surfaces verbatim to the agent regardless of JSON support.

Decision rules:
- tool_name != "Read" → allow (defensive; matcher should already scope it).
- file_path missing or unreadable → allow (let Read surface the error).
- offset or limit already passed → allow.
- line count <= threshold → allow.
- line count > threshold → deny with a message naming the line count.

Threshold comes from --threshold CLI arg, else AGENTOR_READ_THRESHOLD env
var, else a built-in default of 400. A threshold <= 0 disables the hook.
"""

import argparse
import json
import os
import sys
from pathlib import Path

_DEFAULT_THRESHOLD = 400
_MAX_SCAN_BYTES = 5 * 1024 * 1024  # 5 MB; bigger files deny outright.


def _count_lines(path: Path) -> int | None:
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > _MAX_SCAN_BYTES:
        # Don't read the whole file just to confirm it's huge; return a
        # value that certainly exceeds any reasonable threshold so the
        # caller denies.
        return size // 40  # ~avg line length fallback
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return None


def decide(payload: dict, threshold: int) -> dict:
    """Return a hookSpecificOutput dict: `{permissionDecision, reason}`."""
    if threshold <= 0:
        return {"permissionDecision": "allow"}
    if payload.get("tool_name") != "Read":
        return {"permissionDecision": "allow"}
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return {"permissionDecision": "allow"}
    if tool_input.get("offset") is not None or tool_input.get("limit") is not None:
        return {"permissionDecision": "allow"}
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return {"permissionDecision": "allow"}
    p = Path(file_path)
    if not p.exists() or not p.is_file():
        return {"permissionDecision": "allow"}
    lines = _count_lines(p)
    if lines is None or lines <= threshold:
        return {"permissionDecision": "allow"}
    reason = (
        f"File is {lines} lines (threshold {threshold}). "
        "Read a narrow range (offset/limit) or Grep for the relevant "
        "symbol first."
    )
    return {"permissionDecision": "deny", "reason": reason}


def _resolve_threshold(cli_value: int | None) -> int:
    if cli_value is not None:
        return cli_value
    env = os.environ.get("AGENTOR_READ_THRESHOLD")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return _DEFAULT_THRESHOLD


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=int, default=None)
    args = parser.parse_args(argv)
    threshold = _resolve_threshold(args.threshold)

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        # Malformed input: fail open, don't block the agent.
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }))
        return 0

    result = decide(payload, threshold)
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
        sys.stderr.write(result.get("reason", "Read denied by hook."))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
