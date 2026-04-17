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
        phase = "execute" if item.session_id else "plan"
    return (
        cfg.project_root / ".agentor" / "transcripts" / f"{item.id}.{phase}.log"
    )


def _tail_lines(path: Path, limit: int = 12) -> list[str]:
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return []
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
    assistant text, tool_use calls, tool_result summaries."""
    out: list[str] = []
    for ev in iter_events(path):
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
