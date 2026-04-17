"""Shared stream-json transcript walker.

The claude-code CLI emits one JSON event per line (plus a human-readable
header). Both the dashboard (live activity feed) and the offline
`tools/analyze_transcripts.py` / `tools/analyze_waste.py` scripts walk the
same shape. This module factors out:

- JSONL line filtering (skip blanks, non-`{` header lines, malformed events).
- `tool_use` id → (name, input) pairing so tool_result blocks carry their
  originating call's context forward.
- tool_result `content` → plain text extraction (string or list-of-text-blocks).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Union


@dataclass(frozen=True)
class SessionInit:
    raw: dict


@dataclass(frozen=True)
class AssistantText:
    text: str


@dataclass(frozen=True)
class AssistantUsage:
    usage: dict
    stop_reason: str | None


@dataclass(frozen=True)
class ToolCall:
    id: str | None
    name: str
    input: dict


@dataclass(frozen=True)
class ToolResult:
    tool_use_id: str | None
    tool_name: str | None
    tool_input: dict
    text: str
    is_error: bool


@dataclass(frozen=True)
class RunResult:
    total_cost_usd: float | None
    usage: dict | None
    duration_ms: int | None
    num_turns: int | None
    subtype: str | None
    is_error: bool
    result: str | None
    stop_reason: str | None


TranscriptEvent = Union[
    SessionInit,
    AssistantText,
    AssistantUsage,
    ToolCall,
    ToolResult,
    RunResult,
]


def iter_raw_events(path: Path) -> Iterator[dict]:
    """Yield parsed JSON objects from a stream-json transcript file.

    Skips blank lines, header lines that don't start with `{`, and any line
    that isn't valid JSON or doesn't decode to a dict. A live transcript may
    end mid-write, so robust-by-default tolerance matters."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return
    for line in raw.splitlines():
        s = line.strip()
        if not s or not s.startswith("{"):
            continue
        try:
            ev = json.loads(s)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            yield ev


def tool_result_text(content: object) -> str:
    """Flatten a tool_result `content` payload into plain text.

    Claude emits two shapes: a bare string, or a list of blocks where each
    block may be `{"type": "text", "text": "..."}`. Non-text list entries
    are dropped. Anything else falls back to `str(...)`."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for sub in content:
            if isinstance(sub, dict) and sub.get("type") == "text":
                parts.append(str(sub.get("text") or ""))
            elif isinstance(sub, str):
                parts.append(sub)
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


def iter_events(path: Path) -> Iterator[TranscriptEvent]:
    """Walk a transcript and yield typed activity events in order.

    Each assistant message emits an `AssistantUsage` (when a `usage` dict is
    present) followed by one event per content block (`AssistantText` or
    `ToolCall`). Each user message emits one `ToolResult` per `tool_result`
    block, with the originating `ToolCall`'s name + input carried forward
    when the `tool_use_id` matches one we've already seen. The terminal
    `result` event maps to `RunResult`."""
    tool_use_by_id: dict[str, tuple[str, dict]] = {}
    for ev in iter_raw_events(path):
        etype = ev.get("type")
        if etype == "system" and ev.get("subtype") == "init":
            yield SessionInit(raw=ev)
        elif etype == "assistant":
            msg = ev.get("message") or {}
            usage = msg.get("usage")
            stop_reason = msg.get("stop_reason")
            if isinstance(usage, dict):
                yield AssistantUsage(usage=usage, stop_reason=stop_reason)
            elif stop_reason is not None:
                yield AssistantUsage(usage={}, stop_reason=stop_reason)
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        yield AssistantText(text=text)
                elif btype == "tool_use":
                    name = block.get("name") or "tool"
                    tinput = block.get("input") or {}
                    if not isinstance(tinput, dict):
                        tinput = {}
                    tid = block.get("id")
                    if isinstance(tid, str):
                        tool_use_by_id[tid] = (name, tinput)
                    yield ToolCall(id=tid, name=name, input=tinput)
        elif etype == "user":
            msg = ev.get("message") or {}
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_result":
                    continue
                use_id = block.get("tool_use_id")
                paired = tool_use_by_id.get(use_id) if isinstance(use_id, str) else None
                tname = paired[0] if paired else None
                tinput = paired[1] if paired else {}
                text = tool_result_text(block.get("content"))
                yield ToolResult(
                    tool_use_id=use_id if isinstance(use_id, str) else None,
                    tool_name=tname,
                    tool_input=tinput,
                    text=text,
                    is_error=bool(block.get("is_error")),
                )
        elif etype == "result":
            yield RunResult(
                total_cost_usd=ev.get("total_cost_usd"),
                usage=ev.get("usage") if isinstance(ev.get("usage"), dict) else None,
                duration_ms=ev.get("duration_ms"),
                num_turns=ev.get("num_turns"),
                subtype=ev.get("subtype"),
                is_error=bool(ev.get("is_error")),
                result=ev.get("result"),
                stop_reason=ev.get("stop_reason"),
            )
