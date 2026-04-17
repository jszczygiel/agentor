#!/usr/bin/env python3
"""Drill into specific waste patterns across stream-json transcripts.

Usage:
    python3 tools/analyze_waste.py [TRANSCRIPTS_DIR]

Defaults to $AGENTOR_PROJECT_ROOT/.agentor/transcripts when set."""
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentor.transcript import ToolResult, iter_events  # noqa: E402


if len(sys.argv) > 1:
    TRANS_DIR = Path(sys.argv[1])
else:
    root = os.environ.get("AGENTOR_PROJECT_ROOT")
    if not root:
        print(
            "usage: analyze_waste.py <TRANSCRIPTS_DIR>\n"
            "or set AGENTOR_PROJECT_ROOT to the project root.",
            file=sys.stderr,
        )
        sys.exit(2)
    TRANS_DIR = Path(root) / ".agentor" / "transcripts"


def main():
    bash_cmd_sizes = []  # (bytes, cmd, item_id)
    grep_sizes = []  # (bytes, pattern, head_limit, output_mode, item_id)
    read_sizes = []  # (bytes, fp, item_id)
    glob_sizes = []
    bash_cmd_counts = Counter()

    for logpath in sorted(TRANS_DIR.glob("*.log")):
        item_id = logpath.stem
        for ev in iter_events(logpath):
            if not isinstance(ev, ToolResult):
                continue
            sz = len(ev.text.encode("utf-8", errors="replace"))
            tname = ev.tool_name
            tinput = ev.tool_input
            if tname == "Bash":
                cmd = tinput.get("command", "")
                bash_cmd_sizes.append((sz, cmd, item_id))
                first = cmd.strip().split()[0] if cmd.strip() else "?"
                bash_cmd_counts[first] += 1
            elif tname == "Grep":
                grep_sizes.append(
                    (
                        sz,
                        tinput.get("pattern", ""),
                        tinput.get("head_limit"),
                        tinput.get("output_mode", "files_with_matches"),
                        item_id,
                    )
                )
            elif tname == "Read":
                read_sizes.append((sz, tinput.get("file_path", ""), item_id))
            elif tname == "Glob":
                glob_sizes.append((sz, tinput.get("pattern", ""), item_id))

    print("--- TOP 20 BASH OUTPUTS ---")
    for sz, cmd, item in sorted(bash_cmd_sizes, key=lambda x: -x[0])[:20]:
        print(f"  {sz:>7} B  {item:<30} {cmd[:100]}")

    print()
    print("--- TOP 20 GREP OUTPUTS ---")
    for sz, pat, head, mode, item in sorted(grep_sizes, key=lambda x: -x[0])[:20]:
        print(
            f"  {sz:>7} B  head={head!s:<5} mode={mode:<18} {item}  pattern={pat[:60]}"
        )

    # Grep head_limit usage
    nohead = [g for g in grep_sizes if g[2] is None]
    with_head = [g for g in grep_sizes if g[2] is not None]
    print()
    print(f"Grep calls: total={len(grep_sizes)}, no head_limit={len(nohead)}, with head_limit={len(with_head)}")
    print(
        f"Avg grep output: no-head={sum(g[0] for g in nohead)/max(len(nohead),1):.0f}B, with-head={sum(g[0] for g in with_head)/max(len(with_head),1):.0f}B"
    )

    print()
    print("--- TOP 20 READ OUTPUTS ---")
    for sz, fp, item in sorted(read_sizes, key=lambda x: -x[0])[:20]:
        print(f"  {sz:>7} B  {item:<30} {fp}")

    # Re-read waste: same file in same run
    print()
    print("--- TOTAL BYTES WASTED ON RE-READS (stream-json runs only) ---")
    wasted = defaultdict(int)
    reads_by_run = defaultdict(list)  # (item_id, fp) -> [sizes]
    for sz, fp, item in read_sizes:
        reads_by_run[(item, fp)].append(sz)
    for (item, fp), sizes in reads_by_run.items():
        if len(sizes) > 1:
            # All re-reads after first are potential waste (file unchanged across the re-read burst)
            wasted[item] += sum(sizes[1:])
    total_waste = sum(wasted.values())
    print(f"  total re-read bytes: {total_waste:,}")
    for item, b in sorted(wasted.items(), key=lambda x: -x[1])[:10]:
        print(f"  {item:<30} {b:,} B")

    print()
    print("--- BASH COMMAND FREQUENCY (first token) ---")
    for cmd, n in bash_cmd_counts.most_common(20):
        print(f"  {n:>4}  {cmd}")


if __name__ == "__main__":
    main()
