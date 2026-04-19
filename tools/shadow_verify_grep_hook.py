#!/usr/bin/env python3
"""Shadow-run the content-mode Grep head_limit hook against existing
transcripts and report how many calls would have been rejected.

Usage:
    python3 tools/shadow_verify_grep_hook.py [TRANSCRIPTS_DIR_OR_FILE]

If no path is given, falls back to $AGENTOR_PROJECT_ROOT/.agentor/transcripts.
Prints a total + per-file breakdown so the operator can confirm the hook
would catch the runs flagged in the backlog evidence."""
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentor.grep_hook import decide  # noqa: E402
from agentor.transcript import ToolCall, iter_events  # noqa: E402


def _resolve_targets(arg: str | None) -> list[Path]:
    if arg:
        p = Path(arg)
        if p.is_dir():
            return sorted(p.glob("*.log"))
        return [p]
    root = os.environ.get("AGENTOR_PROJECT_ROOT")
    if not root:
        print(
            "usage: shadow_verify_grep_hook.py <TRANSCRIPTS_DIR_OR_FILE>\n"
            "or set AGENTOR_PROJECT_ROOT.", file=sys.stderr,
        )
        sys.exit(2)
    return sorted((Path(root) / ".agentor" / "transcripts").glob("*.log"))


def scan(path: Path) -> tuple[int, int]:
    """Return (grep_calls, would_reject) for a single transcript."""
    total = 0
    rejected = 0
    for ev in iter_events(path):
        if not isinstance(ev, ToolCall) or ev.name != "Grep":
            continue
        total += 1
        payload = {"tool_name": "Grep", "tool_input": ev.input}
        if decide(payload)["permissionDecision"] == "deny":
            rejected += 1
    return total, rejected


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    targets = _resolve_targets(arg)
    if not targets:
        print("no transcripts found", file=sys.stderr)
        return 1
    grand_total = 0
    grand_rejected = 0
    per_file: Counter = Counter()
    for t in targets:
        calls, rej = scan(t)
        grand_total += calls
        grand_rejected += rej
        if calls:
            per_file[t.name] = (calls, rej)
    if not grand_total:
        print("no Grep calls found in scanned transcripts")
        return 0
    print(f"scanned files: {len(targets)}")
    print(f"total Grep calls: {grand_total}")
    print(f"would-reject: {grand_rejected} "
          f"({grand_rejected / grand_total:.1%})")
    print(f"would-allow: {grand_total - grand_rejected}")
    print()
    print("per-file breakdown (calls / rejected), top offenders:")
    ranked = sorted(
        per_file.items(), key=lambda kv: kv[1][1], reverse=True,
    )
    for name, (calls, rej) in ranked[:25]:
        if rej:
            print(f"  {name}: {calls} / {rej}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
