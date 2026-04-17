#!/usr/bin/env python3
"""Analyze claude stream-json transcripts from agentor runs.

Usage:
    python3 tools/analyze_transcripts.py [TRANSCRIPTS_DIR]

Defaults to $AGENTOR_PROJECT_ROOT/.agentor/transcripts when the env var
is set. Pass a directory explicitly for ad-hoc analysis of another
project's transcripts."""
import os
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentor.transcript import (  # noqa: E402
    AssistantUsage,
    RunResult,
    ToolCall,
    ToolResult,
    iter_events,
)


if len(sys.argv) > 1:
    TRANS_DIR = Path(sys.argv[1])
else:
    root = os.environ.get("AGENTOR_PROJECT_ROOT")
    if not root:
        print(
            "usage: analyze_transcripts.py <TRANSCRIPTS_DIR>\n"
            "or set AGENTOR_PROJECT_ROOT to the project root.",
            file=sys.stderr,
        )
        sys.exit(2)
    TRANS_DIR = Path(root) / ".agentor" / "transcripts"

# Opus 4 pricing (USD per 1M tokens) — fallback when result event lacks cost
OPUS_INPUT = 15.0
OPUS_OUTPUT = 75.0
OPUS_CACHE_WRITE = 18.75
OPUS_CACHE_READ = 1.50


def analyze_run(path: Path):
    stem = path.stem  # e.g. 07340a3f34a9.execute or 07340a3f34a9 (single)
    parts = stem.split(".")
    item_id = parts[0]
    phase = parts[1] if len(parts) > 1 else "single"

    total_input = 0
    total_output = 0
    total_cache_create = 0
    total_cache_read = 0
    tool_uses = Counter()
    reads_by_file = Counter()
    tool_result_sizes = []  # (bytes, tool_name, tool_input)
    result_cost = None
    result_usage = None
    result_duration_ms = None
    result_num_turns = None
    hit_turn_limit = False
    stop_reason = None
    file_size = path.stat().st_size

    for ev in iter_events(path):
        if isinstance(ev, AssistantUsage):
            usage = ev.usage
            total_input += usage.get("input_tokens", 0) or 0
            total_output += usage.get("output_tokens", 0) or 0
            total_cache_create += usage.get("cache_creation_input_tokens", 0) or 0
            total_cache_read += usage.get("cache_read_input_tokens", 0) or 0
            stop_reason = ev.stop_reason or stop_reason
        elif isinstance(ev, ToolCall):
            name = ev.name or "?"
            tool_uses[name] += 1
            if name == "Read":
                reads_by_file[ev.input.get("file_path") or "?"] += 1
        elif isinstance(ev, ToolResult):
            sz = len(ev.text.encode("utf-8", errors="replace"))
            tname = ev.tool_name or "?"
            tool_result_sizes.append((sz, tname, ev.tool_input))
        elif isinstance(ev, RunResult):
            result_cost = ev.total_cost_usd
            result_usage = ev.usage
            result_duration_ms = ev.duration_ms
            result_num_turns = ev.num_turns
            if ev.subtype and "turn" in ev.subtype.lower():
                hit_turn_limit = True
            if ev.is_error:
                err_txt = str(ev.result or "").lower()
                if "turn" in err_txt and "limit" in err_txt:
                    hit_turn_limit = True

    # Prefer result.usage totals when present (they cover the whole session)
    if result_usage:
        total_input = result_usage.get("input_tokens", total_input) or total_input
        total_output = result_usage.get("output_tokens", total_output) or total_output
        total_cache_create = (
            result_usage.get("cache_creation_input_tokens", total_cache_create)
            or total_cache_create
        )
        total_cache_read = (
            result_usage.get("cache_read_input_tokens", total_cache_read)
            or total_cache_read
        )

    denom = total_cache_read + total_input + total_cache_create
    hit_ratio = (total_cache_read / denom) if denom else 0.0

    if result_cost is None:
        est = (
            total_input * OPUS_INPUT
            + total_output * OPUS_OUTPUT
            + total_cache_create * OPUS_CACHE_WRITE
            + total_cache_read * OPUS_CACHE_READ
        ) / 1_000_000
        cost = est
        cost_source = "est"
    else:
        cost = result_cost
        cost_source = "result"

    num_turns = result_num_turns or 0

    top_tool_results = sorted(tool_result_sizes, key=lambda x: -x[0])[:5]
    top_reads = reads_by_file.most_common(10)

    return {
        "path": path,
        "item_id": item_id,
        "phase": phase,
        "file_size": file_size,
        "input": total_input,
        "output": total_output,
        "cache_create": total_cache_create,
        "cache_read": total_cache_read,
        "hit_ratio": hit_ratio,
        "cost": cost,
        "cost_source": cost_source,
        "num_turns": num_turns,
        "duration_ms": result_duration_ms,
        "tool_uses": dict(tool_uses),
        "top_tool_results": top_tool_results,
        "top_reads": top_reads,
        "reads_total": sum(reads_by_file.values()),
        "reads_distinct": len(reads_by_file),
        "hit_turn_limit": hit_turn_limit,
        "stop_reason": stop_reason,
    }


def fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1000:.1f}k"
    return str(n)


def fmt_bytes(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}MB"
    if n >= 1_000:
        return f"{n/1000:.1f}kB"
    return f"{n}B"


def main():
    logs = sorted(TRANS_DIR.glob("*.log"))
    runs = [analyze_run(p) for p in logs]

    print(f"# Transcripts directory: {TRANS_DIR}")
    print(f"# Total files: {len(runs)}")
    print()
    print(
        "id            phase    sizeKB  turns  in_tok   out_tok  cache_w   cache_r  hit%   cost$  source"
    )
    for r in sorted(runs, key=lambda r: -r["cost"]):
        print(
            f"{r['item_id']:<13} {r['phase']:<8} "
            f"{r['file_size']//1024:>6}  "
            f"{r['num_turns']:>4}  "
            f"{fmt_tokens(r['input']):>7} "
            f"{fmt_tokens(r['output']):>8} "
            f"{fmt_tokens(r['cache_create']):>7} "
            f"{fmt_tokens(r['cache_read']):>8} "
            f"{r['hit_ratio']*100:>5.1f}  "
            f"{r['cost']:>6.2f}  "
            f"{r['cost_source']}"
        )

    total_cost = sum(r["cost"] for r in runs)
    total_in = sum(r["input"] for r in runs)
    total_out = sum(r["output"] for r in runs)
    total_cw = sum(r["cache_create"] for r in runs)
    total_cr = sum(r["cache_read"] for r in runs)
    total_turns = sum(r["num_turns"] for r in runs)
    costs = sorted(r["cost"] for r in runs)

    def pct(pcts, data):
        if not data:
            return 0
        k = (len(data) - 1) * pcts
        f = int(k)
        c = min(f + 1, len(data) - 1)
        return data[f] + (data[c] - data[f]) * (k - f)

    print()
    print("--- AGGREGATE ---")
    print(f"Total cost: ${total_cost:.2f}")
    print(f"Total input tok: {fmt_tokens(total_in)}")
    print(f"Total output tok: {fmt_tokens(total_out)}")
    print(f"Total cache_write tok: {fmt_tokens(total_cw)}")
    print(f"Total cache_read tok: {fmt_tokens(total_cr)}")
    overall_hit = total_cr / (total_cr + total_in + total_cw) if (total_cr + total_in + total_cw) else 0
    print(f"Overall cache hit ratio: {overall_hit*100:.1f}%")
    print(f"Total turns: {total_turns}")
    print(f"Median cost/run: ${statistics.median(costs):.2f}")
    print(f"p90 cost/run: ${pct(0.9, costs):.2f}")
    print(f"Max cost/run: ${max(costs):.2f}")

    # Plan vs execute breakdown
    plan_runs = [r for r in runs if r["phase"] == "plan"]
    exec_runs = [r for r in runs if r["phase"] == "execute"]
    single_runs = [r for r in runs if r["phase"] == "single"]
    print()
    print(
        f"Plan: {len(plan_runs)} runs, ${sum(r['cost'] for r in plan_runs):.2f} total, "
        f"avg ${statistics.mean([r['cost'] for r in plan_runs]) if plan_runs else 0:.2f}/run, "
        f"avg turns {statistics.mean([r['num_turns'] for r in plan_runs]) if plan_runs else 0:.1f}"
    )
    print(
        f"Exec: {len(exec_runs)} runs, ${sum(r['cost'] for r in exec_runs):.2f} total, "
        f"avg ${statistics.mean([r['cost'] for r in exec_runs]) if exec_runs else 0:.2f}/run, "
        f"avg turns {statistics.mean([r['num_turns'] for r in exec_runs]) if exec_runs else 0:.1f}"
    )
    print(
        f"Single: {len(single_runs)} runs, ${sum(r['cost'] for r in single_runs):.2f} total"
    )

    # Poor cache hit (<50%)
    poor = [r for r in runs if r["hit_ratio"] < 0.5 and (r["input"] + r["cache_create"] + r["cache_read"]) > 10_000]
    print(f"Runs with cache hit <50%: {len(poor)}")
    for r in poor:
        print(
            f"  {r['item_id']} {r['phase']} hit={r['hit_ratio']*100:.1f}% cost=${r['cost']:.2f} turns={r['num_turns']}"
        )

    # Top 10 costliest
    print()
    print("--- TOP 10 COSTLIEST RUNS ---")
    for r in sorted(runs, key=lambda r: -r["cost"])[:10]:
        tops = ", ".join(
            f"{t[1]}:{fmt_bytes(t[0])}"
            for t in r["top_tool_results"][:3]
        )
        rereads = [(f, c) for f, c in r["top_reads"] if c > 1]
        rereads_str = (
            ", ".join(f"{Path(f).name}x{c}" for f, c in rereads[:3])
            if rereads
            else "-"
        )
        print(
            f"{r['item_id']} {r['phase']} ${r['cost']:.2f} "
            f"turns={r['num_turns']} tools={r['tool_uses']} "
            f"topresults=[{tops}] rereads=[{rereads_str}]"
        )

    # Waste: re-reads within a run
    print()
    print("--- RE-READ WASTE (same file, >=3x in one run) ---")
    for r in runs:
        heavy = [(f, c) for f, c in r["top_reads"] if c >= 3]
        if heavy:
            print(f"  {r['item_id']} {r['phase']}:")
            for f, c in heavy:
                print(f"    {c}x {f}")

    # Huge tool result outputs
    print()
    print("--- HUGE TOOL RESULTS (>50kB) ---")
    for r in runs:
        for sz, tname, tinput in r["top_tool_results"]:
            if sz > 50_000:
                if tname == "Bash":
                    detail = (tinput.get("command") or "")[:100]
                elif tname == "Grep":
                    detail = f"pattern={tinput.get('pattern', '')[:40]} mode={tinput.get('output_mode', 'files')} head={tinput.get('head_limit', 'none')}"
                elif tname == "Read":
                    detail = tinput.get("file_path", "")
                elif tname == "Glob":
                    detail = tinput.get("pattern", "")
                else:
                    detail = str(tinput)[:80]
                print(f"  {r['item_id']} {r['phase']} {tname} {fmt_bytes(sz)}  {detail}")

    # Tool usage totals
    print()
    print("--- TOOL USE TOTALS ---")
    agg_tools = Counter()
    for r in runs:
        for k, v in r["tool_uses"].items():
            agg_tools[k] += v
    for tname, count in agg_tools.most_common():
        print(f"  {tname}: {count}")

    # Turn limits / stop reasons
    print()
    print("--- STOP REASONS / TURN LIMITS ---")
    for r in runs:
        if r["hit_turn_limit"] or r["num_turns"] >= 40 or r["stop_reason"] not in (None, "end_turn", "tool_use"):
            print(f"  {r['item_id']} {r['phase']} turns={r['num_turns']} stop={r['stop_reason']} hit_limit={r['hit_turn_limit']}")


if __name__ == "__main__":
    main()
