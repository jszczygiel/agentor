import json
from pathlib import Path

from ..config import Config
from ..store import StoredItem
from ..transcript import (
    AssistantText,
    RunResult,
    SessionInit,
    ToolCall,
    ToolResult,
    iter_events,
)

from .formatters import _one_line, _phase_for


def _transcript_path_for(cfg: Config, item: StoredItem) -> Path:
    phase = _phase_for(item)
    if not phase:
        phase = "execute" if item.agent_ref else "plan"
    return (
        cfg.project_root / ".agentor" / "transcripts" / f"{item.id}.{phase}.log"
    )


# ~256KB of tail is enough to cover thousands of stream-json events and
# tens of thousands of raw log lines — far past the dashboard's render
# budget on anything but a pathological long line. Stays in RAM comfortably
# while capping the per-tick work at O(tail_bytes), not O(file_size).
_TAIL_BYTES = 256 * 1024


def _tail_lines(path: Path, limit: int = 12) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            if size <= _TAIL_BYTES:
                fh.seek(0)
            else:
                fh.seek(size - _TAIL_BYTES)
            data = fh.read()
    except FileNotFoundError:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    # Drop the first line when we seeked into the middle of the file — it's
    # almost certainly truncated at the seek boundary and would show up as
    # a garbled tail row.
    if size > _TAIL_BYTES and lines:
        lines = lines[1:]
    return lines[-limit:]


def _brief_tool_input(name: str, inp: object) -> str:
    """Pick the most informative field of a tool_use input and render it in one
    line. Keeps `Bash(git status)` and `Read(/path/file.py)` recognisable
    without dumping full JSON."""
    if not isinstance(inp, dict):
        return ""
    priority = {
        "Bash": ("command",),
        "Read": ("file_path",),
        "Write": ("file_path",),
        "Edit": ("file_path",),
        "Glob": ("pattern",),
        "Grep": ("pattern",),
        "WebFetch": ("url",),
        "WebSearch": ("query",),
    }
    for key in priority.get(name, ()):
        val = inp.get(key)
        if val:
            return _one_line(str(val), 80)
    for key in ("command", "file_path", "path", "pattern", "query", "url",
                "description"):
        val = inp.get(key)
        if val:
            return _one_line(str(val), 80)
    try:
        return _one_line(json.dumps(inp, ensure_ascii=False), 80)
    except Exception:
        return ""


def _tool_result_preview(text: str) -> str:
    return _one_line(text, 120) or "(empty)"


def _session_activity(path: Path, limit: int = 25) -> list[str]:
    """Render a compact activity feed from the claude stream-json transcript:
    assistant text, tool_use calls, tool_result summaries.

    Only reads the trailing `_TAIL_BYTES` of the file — a full read on a
    multi-MB transcript was the root cause of the dashboard appearing hung
    while inspect view refreshed once per second."""
    out: list[str] = []
    for ev in iter_events(path, tail_bytes=_TAIL_BYTES):
        if isinstance(ev, SessionInit):
            out.append("·  session init")
        elif isinstance(ev, AssistantText):
            out.append(f"·  {_one_line(ev.text, 160)}")
        elif isinstance(ev, ToolCall):
            brief = _brief_tool_input(ev.name, ev.input)
            out.append(f">  {ev.name}({brief})" if brief else f">  {ev.name}")
        elif isinstance(ev, ToolResult):
            snippet = _tool_result_preview(ev.text)
            tag = "!" if ev.is_error else "<"
            out.append(f"{tag}  {snippet}")
        elif isinstance(ev, RunResult):
            rr = ev.result or ev.stop_reason or "done"
            out.append(f"=  {_one_line(str(rr), 160)}")
    return out[-limit:]
