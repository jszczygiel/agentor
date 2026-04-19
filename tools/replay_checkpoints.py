#!/usr/bin/env python3
"""Replay a finished stream-json transcript through CheckpointEmitter in
dry-run mode and print where nudges would have landed.

Usage:
    python3 tools/replay_checkpoints.py <transcript.log> [MORE.log ...]
    python3 tools/replay_checkpoints.py --soft 60 --hard 100 \
        --output-tokens 50000 <transcript.log>

Prints one line per crossed threshold with the current turn index and
cumulative output token count, followed by the nudge body that would have
been injected."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentor.checkpoint import (  # noqa: E402
    CheckpointConfig,
    CheckpointEmitter,
)
from agentor.transcript import AssistantUsage, iter_events  # noqa: E402


def replay(path: Path, cfg: CheckpointConfig) -> list[dict]:
    emitter = CheckpointEmitter(cfg)
    turn = 0
    output_tokens = 0
    fires: list[dict] = []
    for ev in iter_events(path):
        if not isinstance(ev, AssistantUsage):
            continue
        usage = ev.usage or {}
        if not usage:
            continue
        turn += 1
        output_tokens += int(usage.get("output_tokens", 0) or 0)
        for nudge in emitter.observe(turn, output_tokens):
            fires.append({
                "turn": turn, "output_tokens": output_tokens, "nudge": nudge,
            })
    return fires


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--soft", type=int, default=60)
    ap.add_argument("--hard", type=int, default=100)
    ap.add_argument("--output-tokens", type=int, default=50_000)
    args = ap.parse_args()

    cfg = CheckpointConfig(
        soft_turns=args.soft,
        hard_turns=args.hard,
        output_tokens=args.output_tokens,
    )

    total = 0
    for path in args.paths:
        fires = replay(path, cfg)
        total += len(fires)
        if not fires:
            print(f"{path}: no thresholds crossed")
            continue
        print(f"{path}: {len(fires)} injection(s) would land:")
        for f in fires:
            print(f"  turn {f['turn']:>3}  output_tokens={f['output_tokens']}")
            # Truncate nudge to one line for readability.
            body = " ".join(f["nudge"].split())
            print(f"    {body[:180]}")
    print(f"\ntotal: {total} injections across {len(args.paths)} transcript(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
