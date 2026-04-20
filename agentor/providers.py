"""Per-CLI behaviour that recovery + runner code must consult without
hardcoding a Claude substring.

A `Provider` encapsulates four concerns that differ between the Claude
CLI and the Codex CLI:

1. Dead-session detection — the set of error substrings that mean
   "resuming this persisted session id / thread id will never succeed,
   start fresh instead". Claude says `No conversation found with session
   ID ...`; Codex says `thread not found` / `thread/start failed` /
   `session not found`. Routing through the active provider keeps
   recovery from matching a Claude-only string against a Codex failure
   row (or vice-versa).

2. Wall-clock session expiry — Claude CLI sessions age out in ~5h, so
   the recovery sweep pre-emptively demotes WORKING items whose session
   is older than `agent.session_max_age_hours` rather than pay for a
   doomed `--resume`. Stub has no real session. A provider returns
   `None` here to opt out of the age gate entirely.

3. Resume primer — the kill-resume path needs a "don't re-Read these
   files" block pulled from the interrupted transcript. Claude's
   stream-json vocabulary carries Read/Grep/Edit tool calls; Codex emits
   a different envelope (`thread.started` / `turn.started` / message
   events) with no tool-call granularity yet, so its implementation
   returns `None` until the transcript format is settled.

4. Activity feed — the dashboard inspect view renders a compact "what
   did the agent just do" list. Each provider parses its own transcript
   vocabulary and emits rendered strings; the dashboard stays vendor-
   agnostic and just concatenates whatever the active provider produced.

The module is intentionally dependency-light (imports only `Config` for a
forward ref via string annotation) so `runner` can import it at module
top without a cycle.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .transcript import (
    AssistantText,
    AssistantUsage,
    RunResult,
    SessionInit,
    ToolCall,
    ToolResult,
    iter_events,
    iter_raw_events,
)

if TYPE_CHECKING:
    from .config import Config


class Provider:
    """Base class. Subclasses override per-CLI methods; defaults cover
    providers that don't implement every hook (Codex has no primer, Stub
    has no sessions, etc.)."""

    # Short alias → current-best model id for this CLI. Rotated in lockstep
    # with the vendor's releases. `execute_model_whitelist` in AgentConfig
    # defaults to `[]` meaning "this map's keys" — keep the default path
    # honest by populating the map on every concrete subclass. Empty maps
    # disable the `@model:` tag / plan-nomination channel for that provider.
    model_aliases: ClassVar[dict[str, str]] = {}

    def is_dead_session_error(self, msg: str) -> bool:
        """True when the error message means the persisted session id /
        thread id is gone and the next `--resume` will always fail.

        Matches are lowercased-substring; callers may pass either the raw
        error string or the whitespace-stripped `error_sig` form (both
        `_error_signature` outputs and raw text flow through the same
        callsite in recovery)."""
        raise NotImplementedError

    def session_max_age_hours(self) -> float | None:
        """Configured max age (in hours) beyond which a persisted session
        id is assumed dead. Returning `None` disables the age gate
        entirely — recovery still honours the per-failure-row predicate
        but stops demoting purely on wall-clock age."""
        raise NotImplementedError

    def model_to_alias(self, model_id: str) -> str | None:
        """Reverse lookup: map a full model id back to its short alias.
        Default is exact-match against `model_aliases`; subclasses that
        want a prefix fallback (e.g. `claude-opus-4-6` → `opus` even when
        the map has rotated to `claude-opus-4-7`) override."""
        if not model_id:
            return None
        for alias, mid in self.model_aliases.items():
            if mid == model_id:
                return alias
        return None

    def build_primer(self, transcript_path: Path) -> str | None:
        """Return a markdown "don't re-fetch these files" primer for a
        kill-resumed run, or None when the prior transcript carries no
        useful signal. Default is a no-op so providers without a primer
        implementation (Codex, Stub) don't need to override."""
        return None

    def activity_feed(
        self, transcript_path: Path, limit: int = 25,
    ) -> list[str]:
        """Render a compact activity feed from a transcript. Default is
        an empty list — providers override to parse their transcript
        vocabulary into feed lines the dashboard concatenates verbatim."""
        return []


# ---------------------------------------------------------------------------
# Shared formatting helpers
# ---------------------------------------------------------------------------


_FEED_TAIL_BYTES = 256 * 1024
# ~256KB of tail is enough to cover thousands of stream-json events and
# tens of thousands of raw log lines — far past the dashboard's render
# budget on anything but a pathological long line.


def _one_line(text: str, width: int) -> str:
    s = " ".join((text or "").split())
    return s[: width - 1] + "…" if len(s) > width else s


# ---------------------------------------------------------------------------
# Claude: resume primer helpers (moved from resume_primer.py)
# ---------------------------------------------------------------------------


_PRIMER_TAIL_BYTES = 512 * 1024
_MAX_FILES_PER_SECTION = 20
_MAX_GREP_HITS_PER_PATTERN = 10
_MAX_LINE_LEN = 200

# Path-ish line in a Grep tool_result: no embedded spaces/colons, contains a
# slash or a dot (filename). Conservative — anything ambiguous is dropped.
_PATHISH = re.compile(r"^[\w./\-]+$")


def _primer_str_field(inp: dict, key: str) -> str:
    val = inp.get(key)
    if isinstance(val, str):
        return val.strip()
    return ""


def _primer_ingest_tool_call(
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
        path = _primer_str_field(inp, "file_path")
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
        path = _primer_str_field(inp, "file_path")
        if not path or path in edits_seen:
            return
        edits_seen.add(path)
        if len(edits) < _MAX_FILES_PER_SECTION:
            edits.append(path)
        return
    if name == "Grep":
        pattern = _primer_str_field(inp, "pattern")
        if not pattern:
            return
        if pattern not in grep_hits:
            if len(grep_order) >= _MAX_FILES_PER_SECTION:
                return
            grep_hits[pattern] = []
            grep_order.append(pattern)
        return
    # Bash and everything else intentionally ignored.


def _primer_ingest_grep_result(
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
        if "/" not in s and "." not in s:
            continue
        if s in bucket:
            continue
        bucket.append(s)
        if len(bucket) >= _MAX_GREP_HITS_PER_PATTERN:
            return


def _primer_cap(s: str) -> str:
    return s if len(s) <= _MAX_LINE_LEN else s[: _MAX_LINE_LEN - 1] + "…"


def _primer_render(
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
            lines.append(_primer_cap(f"- {p}"))
        lines.append("")
    if reads_partial:
        lines.append("Files read partially:")
        for p in reads_partial:
            lines.append(_primer_cap(f"- {p}"))
        lines.append("")
    if grep_order:
        lines.append("Greps that matched:")
        for pattern in grep_order:
            hits = grep_hits.get(pattern) or []
            if hits:
                joined = ", ".join(hits)
                lines.append(_primer_cap(f'- "{pattern}" -> {joined}'))
            else:
                lines.append(_primer_cap(f'- "{pattern}" -> (no parsed hits)'))
        lines.append("")
    if edits:
        lines.append("Files edited:")
        for p in edits:
            lines.append(_primer_cap(f"- {p}"))
        lines.append("")
    lines.append(
        "The approved plan still applies. Skip re-reading any of the above "
        "unless you have a specific reason."
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude: activity-feed helpers (moved from dashboard/transcript.py)
# ---------------------------------------------------------------------------


def _brief_tool_input(name: str, inp: object) -> str:
    """Pick the most informative field of a tool_use input and render it in
    one line. Keeps `Bash(git status)` and `Read(/path/file.py)` recognisable
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


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class ClaudeProvider(Provider):
    """Claude CLI. Sessions live ~5h and produce `No conversation found
    with session ID <uuid>` when a stale id is resumed."""

    _NEEDLES = (
        "no conversation found with session id",
    )
    _SIG_NEEDLES = tuple(n.replace(" ", "") for n in _NEEDLES)

    # Rotated in lockstep with Anthropic releases.
    model_aliases: ClassVar[dict[str, str]] = {
        "haiku": "claude-haiku-4-5",
        "sonnet": "claude-sonnet-4-6",
        "opus": "claude-opus-4-7",
    }

    _ALIAS_PREFIX_RE = re.compile(r"^claude-(haiku|sonnet|opus)\b")

    def __init__(self, config: "Config") -> None:
        self._config = config

    def is_dead_session_error(self, msg: str) -> bool:
        low = (msg or "").lower()
        if not low:
            return False
        return any(n in low for n in self._NEEDLES) or any(
            n in low for n in self._SIG_NEEDLES
        )

    def session_max_age_hours(self) -> float | None:
        hours = float(self._config.agent.session_max_age_hours)
        return hours if hours > 0 else None

    def model_to_alias(self, model_id: str) -> str | None:
        # Prefix fallback so e.g. `claude-opus-4-6` still resolves to
        # `opus` when `model_aliases["opus"]` has rotated to a newer id.
        exact = super().model_to_alias(model_id)
        if exact is not None:
            return exact
        m = self._ALIAS_PREFIX_RE.match(model_id or "")
        return m.group(1) if m else None

    def build_primer(
        self, transcript_path: Path, *, min_turns: int = 3,
    ) -> str | None:
        """Return a markdown primer block for a prior killed run, or None.

        Fires only when the transcript has at least `min_turns` assistant
        turns and at least one of (reads, greps, edits) with useful
        content. Returning None means the caller should proceed without
        a primer."""
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
                _primer_ingest_tool_call(
                    ev,
                    reads_full=reads_full, reads_full_seen=reads_full_seen,
                    reads_partial=reads_partial,
                    reads_partial_seen=reads_partial_seen,
                    grep_hits=grep_hits, grep_order=grep_order,
                    edits=edits, edits_seen=edits_seen,
                )
                if ev.name == "Grep":
                    pattern = _primer_str_field(ev.input, "pattern")
                    last_grep_pattern = pattern or None
                else:
                    last_grep_pattern = None
                continue
            if isinstance(ev, ToolResult):
                if (ev.tool_name == "Grep" and last_grep_pattern
                        and not ev.is_error):
                    _primer_ingest_grep_result(
                        ev.text, last_grep_pattern, grep_hits, grep_order,
                    )
                last_grep_pattern = None
                continue

        if assistant_turns < min_turns:
            return None
        if not (reads_full or reads_partial or grep_hits or edits):
            return None

        return _primer_render(
            reads_full=reads_full,
            reads_partial=reads_partial,
            grep_hits=grep_hits,
            grep_order=grep_order,
            edits=edits,
        )

    def activity_feed(
        self, transcript_path: Path, limit: int = 25,
    ) -> list[str]:
        """Render a compact activity feed from the claude stream-json
        transcript: assistant text, tool_use calls, tool_result summaries.

        Only reads the trailing `_FEED_TAIL_BYTES` of the file — a full
        read on a multi-MB transcript was the root cause of the dashboard
        appearing hung while inspect view refreshed once per second."""
        out: list[str] = []
        for ev in iter_events(transcript_path, tail_bytes=_FEED_TAIL_BYTES):
            if isinstance(ev, SessionInit):
                out.append("·  session init")
            elif isinstance(ev, AssistantText):
                out.append(f"·  {_one_line(ev.text, 160)}")
            elif isinstance(ev, ToolCall):
                brief = _brief_tool_input(ev.name, ev.input)
                out.append(
                    f">  {ev.name}({brief})" if brief else f">  {ev.name}"
                )
            elif isinstance(ev, ToolResult):
                snippet = _tool_result_preview(ev.text)
                tag = "!" if ev.is_error else "<"
                out.append(f"{tag}  {snippet}")
            elif isinstance(ev, RunResult):
                rr = ev.result or ev.stop_reason or "done"
                out.append(f"=  {_one_line(str(rr), 160)}")
        return out[-limit:]


class CodexProvider(Provider):
    """Codex CLI. Threads aren't immortal either — the CLI returns
    `thread not found` / `thread/start failed` / `session not found`
    once the backend drops a stale thread id. Max age uses the same
    generic knob as Claude: if the operator tuned it, honour it."""

    _NEEDLES = (
        "thread not found",
        "thread/start failed",
        "session not found",
    )
    _SIG_NEEDLES = tuple(n.replace(" ", "") for n in _NEEDLES)

    # Size-tier aliases over OpenAI's current flagships. Distinct from
    # Claude's `haiku/sonnet/opus` vocabulary — `@model:haiku` on a
    # Codex-routed item correctly falls through to the default with a
    # soft warning instead of silently pinning a Claude id.
    model_aliases: ClassVar[dict[str, str]] = {
        "mini": "gpt-5-mini",
        "full": "gpt-5",
    }

    def __init__(self, config: "Config") -> None:
        self._config = config

    def is_dead_session_error(self, msg: str) -> bool:
        low = (msg or "").lower()
        if not low:
            return False
        return any(n in low for n in self._NEEDLES) or any(
            n in low for n in self._SIG_NEEDLES
        )

    def session_max_age_hours(self) -> float | None:
        hours = float(self._config.agent.session_max_age_hours)
        return hours if hours > 0 else None

    # Codex has no Read/Grep granularity in its transcript yet — the
    # primer would have no meaningful content to emit, and fabricating
    # one would mislead the resumed agent. Inherit the no-op default.

    def activity_feed(
        self, transcript_path: Path, limit: int = 25,
    ) -> list[str]:
        """Render a compact activity feed from the codex JSONL transcript.

        Codex emits `thread.started`, `turn.started`, plain message /
        result events, and `error` rows. Mapping:
        - `·  thread started`     (thread.started)
        - `·  turn N started`     (turn.started, N tracked locally)
        - `!  {message}`          (error)
        - `<  {one_line(msg)}`    (first non-empty string from
                                   message/last_message/result)
        """
        out: list[str] = []
        turns = 0
        for ev in iter_raw_events(
            transcript_path, tail_bytes=_FEED_TAIL_BYTES,
        ):
            etype = ev.get("type")
            if etype == "thread.started":
                out.append("·  thread started")
                continue
            if etype == "turn.started":
                turns += 1
                out.append(f"·  turn {turns} started")
                continue
            if etype == "error":
                msg = ev.get("message")
                if isinstance(msg, str) and msg.strip():
                    out.append(f"!  {_one_line(msg, 160)}")
                else:
                    out.append("!  (error)")
                continue
            for key in ("message", "last_message", "result"):
                val = ev.get(key)
                if isinstance(val, str) and val.strip():
                    out.append(f"<  {_one_line(val, 160)}")
                    break
        return out[-limit:]


class StubProvider(Provider):
    """Test runner — no real sessions, no wall-clock expiry, no dead-
    session signature."""

    # Mirror Claude's aliases so `runner="stub"` tests that expected the
    # old global `_ALIAS_TO_MODEL` continue to resolve `haiku/sonnet/opus`
    # without needing to pin `runner="claude"`.
    model_aliases: ClassVar[dict[str, str]] = dict(ClaudeProvider.model_aliases)

    def __init__(self, config: "Config") -> None:
        self._config = config

    def is_dead_session_error(self, msg: str) -> bool:
        return False

    def session_max_age_hours(self) -> float | None:
        return None


def make_provider(config: "Config") -> Provider:
    kind = config.agent.runner.lower()
    if kind == "stub":
        return StubProvider(config)
    if kind == "claude":
        return ClaudeProvider(config)
    if kind == "codex":
        return CodexProvider(config)
    raise ValueError(f"unknown agent.runner: {kind!r}")


def detect_provider(config: "Config", transcript_path: Path) -> Provider:
    """Return the provider whose vocabulary matches the given transcript.

    Sniffs the first non-header JSON line: `{"type": "thread.started"}`
    or `{"type": "turn.started"}` is Codex; Claude's `system`/`assistant`/
    `user`/`result` is Claude. Missing / unreadable / empty transcripts
    fall through to the configured default from `make_provider` so the
    dashboard keeps working pre-dispatch and a daemon `[M]` provider flip
    doesn't misparse an in-flight transcript produced under a prior
    runner setting."""
    try:
        with transcript_path.open("rb") as fh:
            # Read at most 8KB — first parseable event always lands here.
            raw = fh.read(8192)
    except (FileNotFoundError, OSError):
        return make_provider(config)
    for line in raw.decode("utf-8", errors="replace").splitlines():
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
        if etype in ("thread.started", "turn.started"):
            return CodexProvider(config)
        if etype in ("system", "assistant", "user", "result"):
            return ClaudeProvider(config)
        # First parseable event matched neither vocabulary (e.g. a codex
        # `error` row emitted before any turn). Keep scanning the rest of
        # the header slice rather than eagerly defaulting.
    return make_provider(config)
