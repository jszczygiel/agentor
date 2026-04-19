"""Build a 'prior run already investigated' primer for kill-resumed sessions.

When the agentor harness is killed mid-execute (laptop sleep, OOM, ^C), the
resumed claude session keeps its prompt cache but loses any structured memory
of which files were already read or grepped. Without a primer the resumed
agent cold-starts discovery and re-Reads files worth tens of thousands of
tokens (observed: 14× Reads of a 1,291-line file in one session).

This module walks the killed run's stream-json transcript via the shared
`agentor.transcript` parser and emits a compact markdown block listing files
Read end-to-end / partially, Grep patterns and the files they matched, and
files the agent edited. The primer is prepended to the next execute prompt.

Deliberately excluded: Bash tool results — those were ephemeral observations,
and their outputs may now be stale."""
from __future__ import annotations

import re
from pathlib import Path

from .transcript import AssistantUsage, ToolCall, ToolResult, iter_events

# Cap how much of the prior transcript we inspect. A very long killed run
# could otherwise balloon the follow-up prompt. 512KB tail is enough to cover
# the tool calls of any realistic dev session.
_PRIMER_TAIL_BYTES = 512 * 1024

# Bound the primer so a pathological prior run can't bloat the resumed prompt.
_MAX_FILES_PER_SECTION = 20
_MAX_GREP_HITS_PER_PATTERN = 10
_MAX_LINE_LEN = 200

# Path-ish line in a Grep tool_result: no embedded spaces/colons, contains a
# slash or a dot (filename). Conservative — anything ambiguous is dropped.
_PATHISH = re.compile(r"^[\w./\-]+$")


def build_primer(
    transcript_path: Path, *, min_turns: int = 3,
) -> str | None:
    """Return a markdown primer block for a prior killed run, or None.

    Fires only when the transcript has at least `min_turns` assistant turns
    and at least one of (reads, greps, edits) with useful content. Returning
    None means the caller should proceed without a primer."""
    if not transcript_path.exists():
        return None

    reads_full: list[str] = []
    reads_full_seen: set[str] = set()
    reads_partial: list[str] = []
    reads_partial_seen: set[str] = set()
    grep_hits: dict[str, list[str]] = {}
    grep_order: list[str] = []
    edits: list[str] = []
    edits_seen: set[str] = set()
    assistant_turns = 0

    last_grep_pattern: str | None = None

    for ev in iter_events(transcript_path, tail_bytes=_PRIMER_TAIL_BYTES):
        if isinstance(ev, AssistantUsage):
            assistant_turns += 1
            continue
        if isinstance(ev, ToolCall):
            _ingest_tool_call(
                ev,
                reads_full=reads_full, reads_full_seen=reads_full_seen,
                reads_partial=reads_partial, reads_partial_seen=reads_partial_seen,
                grep_hits=grep_hits, grep_order=grep_order,
                edits=edits, edits_seen=edits_seen,
            )
            if ev.name == "Grep":
                pattern = _str_field(ev.input, "pattern")
                last_grep_pattern = pattern or None
            else:
                last_grep_pattern = None
            continue
        if isinstance(ev, ToolResult):
            if ev.tool_name == "Grep" and last_grep_pattern and not ev.is_error:
                _ingest_grep_result(
                    ev.text, last_grep_pattern, grep_hits, grep_order,
                )
            last_grep_pattern = None
            continue

    if assistant_turns < min_turns:
        return None
    if not (reads_full or reads_partial or grep_hits or edits):
        return None

    return _render(
        reads_full=reads_full,
        reads_partial=reads_partial,
        grep_hits=grep_hits,
        grep_order=grep_order,
        edits=edits,
    )


def _ingest_tool_call(
    ev: ToolCall,
    *,
    reads_full: list[str],
    reads_full_seen: set[str],
    reads_partial: list[str],
    reads_partial_seen: set[str],
    grep_hits: dict[str, list[str]],
    grep_order: list[str],
    edits: list[str],
    edits_seen: set[str],
) -> None:
    name = ev.name
    inp = ev.input if isinstance(ev.input, dict) else {}
    if name == "Read":
        path = _str_field(inp, "file_path")
        if not path:
            return
        offset = inp.get("offset")
        limit = inp.get("limit")
        if offset or limit:
            start = int(offset) if isinstance(offset, int) else 1
            end = start + int(limit) if isinstance(limit, int) else None
            label = f"{path}:{start}-{end}" if end else f"{path}:{start}+"
            if label not in reads_partial_seen:
                reads_partial_seen.add(label)
                if len(reads_partial) < _MAX_FILES_PER_SECTION:
                    reads_partial.append(label)
        else:
            if path not in reads_full_seen:
                reads_full_seen.add(path)
                if len(reads_full) < _MAX_FILES_PER_SECTION:
                    reads_full.append(path)
        return
    if name in ("Edit", "Write"):
        path = _str_field(inp, "file_path")
        if not path or path in edits_seen:
            return
        edits_seen.add(path)
        if len(edits) < _MAX_FILES_PER_SECTION:
            edits.append(path)
        return
    if name == "Grep":
        pattern = _str_field(inp, "pattern")
        if not pattern:
            return
        if pattern not in grep_hits:
            if len(grep_order) >= _MAX_FILES_PER_SECTION:
                return
            grep_hits[pattern] = []
            grep_order.append(pattern)
        return
    # Bash and everything else intentionally ignored.


def _ingest_grep_result(
    text: str, pattern: str,
    grep_hits: dict[str, list[str]], grep_order: list[str],
) -> None:
    if pattern not in grep_hits:
        if len(grep_order) >= _MAX_FILES_PER_SECTION:
            return
        grep_hits[pattern] = []
        grep_order.append(pattern)
    bucket = grep_hits[pattern]
    for line in text.splitlines():
        s = line.strip()
        if not s or not _PATHISH.match(s):
            continue
        # Need a slash or a dot so we're not picking up bare words.
        if "/" not in s and "." not in s:
            continue
        if s in bucket:
            continue
        bucket.append(s)
        if len(bucket) >= _MAX_GREP_HITS_PER_PATTERN:
            return


def _str_field(inp: dict, key: str) -> str:
    val = inp.get(key)
    if isinstance(val, str):
        return val.strip()
    return ""


def _render(
    *,
    reads_full: list[str],
    reads_partial: list[str],
    grep_hits: dict[str, list[str]],
    grep_order: list[str],
    edits: list[str],
) -> str:
    lines = [
        "## Prior run (killed) already investigated — do NOT re-Read unless needed:",
        "",
    ]
    if reads_full:
        lines.append("Files read end-to-end:")
        for p in reads_full:
            lines.append(_cap(f"- {p}"))
        lines.append("")
    if reads_partial:
        lines.append("Files read partially:")
        for p in reads_partial:
            lines.append(_cap(f"- {p}"))
        lines.append("")
    if grep_order:
        lines.append("Greps that matched:")
        for pattern in grep_order:
            hits = grep_hits.get(pattern) or []
            if hits:
                joined = ", ".join(hits)
                lines.append(_cap(f'- "{pattern}" -> {joined}'))
            else:
                lines.append(_cap(f'- "{pattern}" -> (no parsed hits)'))
        lines.append("")
    if edits:
        lines.append("Files edited:")
        for p in edits:
            lines.append(_cap(f"- {p}"))
        lines.append("")
    lines.append(
        "The approved plan still applies. Skip re-reading any of the above "
        "unless you have a specific reason."
    )
    lines.append("")
    return "\n".join(lines)


def _cap(s: str) -> str:
    return s if len(s) <= _MAX_LINE_LEN else s[: _MAX_LINE_LEN - 1] + "…"
