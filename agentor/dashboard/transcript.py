import json
from pathlib import Path

from ..config import Config
from ..store import StoredItem

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


def _tool_result_preview(body: object) -> str:
    if isinstance(body, list):
        parts = []
        for b in body:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text") or ""))
            elif isinstance(b, str):
                parts.append(b)
        body = "\n".join(parts)
    if not isinstance(body, str):
        body = str(body)
    return _one_line(body, 120) or "(empty)"


def _session_activity(path: Path, limit: int = 25) -> list[str]:
    """Parse the claude stream-json transcript into a compact activity feed:
    assistant text, tool_use calls, tool_result summaries. Skips non-JSON
    header lines and malformed events — a live transcript always ends mid-
    write so robust-by-default matters."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return []
    out: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or not s.startswith("{"):
            continue
        try:
            ev = json.loads(s)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype == "system" and ev.get("subtype") == "init":
            out.append("·  session init")
        elif etype == "assistant":
            msg = ev.get("message") or {}
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        out.append(f"·  {_one_line(text, 160)}")
                elif btype == "tool_use":
                    name = block.get("name") or "tool"
                    brief = _brief_tool_input(name, block.get("input"))
                    out.append(f">  {name}({brief})" if brief else f">  {name}")
        elif etype == "user":
            msg = ev.get("message") or {}
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_result":
                    continue
                snippet = _tool_result_preview(block.get("content"))
                tag = "!" if block.get("is_error") else "<"
                out.append(f"{tag}  {snippet}")
        elif etype == "result":
            rr = ev.get("result") or ev.get("stop_reason") or "done"
            out.append(f"=  {_one_line(str(rr), 160)}")
    return out[-limit:]
