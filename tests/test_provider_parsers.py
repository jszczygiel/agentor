"""Provider-aware transcript parsers: activity feed + primer + sniff.

Exercises the relocated primer + feed parsing on `Provider` subclasses
and the `detect_provider` sniffer that the dashboard uses to pick the
right walker regardless of `cfg.agent.runner`."""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from agentor.config import (
    AgentConfig, Config, GitConfig, ParsingConfig, ReviewConfig, SourcesConfig,
)
from agentor.providers import (
    ClaudeProvider, CodexProvider, StubProvider, detect_provider,
)


def _cfg(runner: str = "claude") -> Config:
    return Config(
        project_name="p", project_root=Path("/tmp/never"),
        sources=SourcesConfig(watch=[], exclude=[]),
        parsing=ParsingConfig(mode="checkbox"),
        agent=AgentConfig(runner=runner),
        git=GitConfig(base_branch="main", branch_prefix="agent/"),
        review=ReviewConfig(),
    )


def _write_jsonl(events: list[dict], header: str = "") -> Path:
    f = tempfile.NamedTemporaryFile(
        "w", suffix=".log", delete=False, encoding="utf-8",
    )
    if header:
        f.write(header)
        if not header.endswith("\n"):
            f.write("\n")
    for ev in events:
        f.write(json.dumps(ev))
        f.write("\n")
    f.close()
    return Path(f.name)


class TestCodexActivityFeed(unittest.TestCase):
    """Codex transcripts use a different vocabulary (thread.started /
    turn.started / message / error) — the codex provider renders them
    into the same `glyph  text` shape the dashboard expects."""

    def test_full_mix_renders_ordered(self):
        path = _write_jsonl([
            {"type": "thread.started", "thread_id": "t-1"},
            {"type": "turn.started"},
            {"type": "turn.started"},
            {"type": "message", "message": "here is my plan"},
            {"type": "error", "message": "connection reset"},
        ])
        try:
            out = CodexProvider(_cfg("codex")).activity_feed(path, limit=25)
        finally:
            path.unlink()
        self.assertEqual(out, [
            "·  thread started",
            "·  turn 1 started",
            "·  turn 2 started",
            "<  here is my plan",
            "!  connection reset",
        ])

    def test_last_message_and_result_also_rendered(self):
        # Codex emits the final agent output under different keys
        # depending on event type — feed must pick whichever non-empty
        # string lands first.
        path = _write_jsonl([
            {"type": "thread.started"},
            {"type": "turn.started"},
            {"type": "turn.completed", "last_message": "final answer"},
            {"type": "task_complete", "result": "shipped"},
        ])
        try:
            out = CodexProvider(_cfg("codex")).activity_feed(path, limit=25)
        finally:
            path.unlink()
        self.assertIn("·  thread started", out)
        self.assertIn("<  final answer", out)
        self.assertIn("<  shipped", out)

    def test_error_without_message_falls_back(self):
        path = _write_jsonl([
            {"type": "thread.started"},
            {"type": "error"},
        ])
        try:
            out = CodexProvider(_cfg("codex")).activity_feed(path, limit=25)
        finally:
            path.unlink()
        self.assertIn("!  (error)", out)

    def test_missing_file_returns_empty(self):
        missing = Path(tempfile.gettempdir()) / f"nope-{uuid.uuid4()}.log"
        self.assertEqual(
            CodexProvider(_cfg("codex")).activity_feed(missing), [],
        )

    def test_limit_keeps_tail(self):
        events = [{"type": "thread.started"}]
        for _ in range(30):
            events.append({"type": "turn.started"})
        path = _write_jsonl(events)
        try:
            out = CodexProvider(_cfg("codex")).activity_feed(path, limit=5)
        finally:
            path.unlink()
        self.assertEqual(len(out), 5)
        # Last line is the final `turn.started`.
        self.assertTrue(out[-1].startswith("·  turn "))


class TestCodexBuildPrimer(unittest.TestCase):
    """Codex has no Read/Grep granularity yet — primer always returns
    None so the resume path runs with an unchanged prompt rather than
    an empty or fabricated "already investigated" block."""

    def test_returns_none_for_codex_transcript(self):
        path = _write_jsonl([
            {"type": "thread.started"},
            {"type": "turn.started"},
            {"type": "turn.started"},
            {"type": "turn.started"},
            {"type": "message", "message": "did stuff"},
        ])
        try:
            self.assertIsNone(
                CodexProvider(_cfg("codex")).build_primer(path),
            )
        finally:
            path.unlink()

    def test_returns_none_for_missing_file(self):
        missing = Path(tempfile.gettempdir()) / f"nope-{uuid.uuid4()}.log"
        self.assertIsNone(CodexProvider(_cfg("codex")).build_primer(missing))


class TestStubProviderDefaults(unittest.TestCase):
    """StubProvider inherits the base no-op hooks so tests that drive
    the stub runner don't trip on a primer/feed expectation."""

    def test_primer_default_is_none(self):
        path = _write_jsonl([{"type": "thread.started"}])
        try:
            self.assertIsNone(StubProvider(_cfg("stub")).build_primer(path))
        finally:
            path.unlink()

    def test_activity_feed_default_is_empty(self):
        path = _write_jsonl([{"type": "thread.started"}])
        try:
            self.assertEqual(
                StubProvider(_cfg("stub")).activity_feed(path), [],
            )
        finally:
            path.unlink()


class TestDetectProvider(unittest.TestCase):
    """Sniff picks the right walker from the transcript header, not
    `cfg.agent.runner`. Mid-daemon `[M]` provider flips leave in-flight
    transcripts in their original vocabulary — sniffing keeps the
    dashboard honest while the config says something else."""

    def test_codex_transcript_detects_codex(self):
        path = _write_jsonl([
            {"type": "thread.started", "thread_id": "t-1"},
            {"type": "turn.started"},
        ])
        try:
            # cfg lies about the runner; sniff must override.
            p = detect_provider(_cfg("claude"), path)
        finally:
            path.unlink()
        self.assertIsInstance(p, CodexProvider)

    def test_claude_transcript_detects_claude(self):
        # Claude transcripts start with a human-readable header line
        # (non-JSON) before any JSONL event; the sniffer must skip it.
        path = _write_jsonl(
            [
                {"type": "system", "subtype": "init", "session_id": "s-1"},
                {"type": "assistant",
                 "message": {"content": [{"type": "text", "text": "hi"}]}},
            ],
            header="args: ['claude']\n\nstdout:",
        )
        try:
            p = detect_provider(_cfg("codex"), path)
        finally:
            path.unlink()
        self.assertIsInstance(p, ClaudeProvider)

    def test_missing_transcript_falls_back_to_cfg(self):
        missing = Path(tempfile.gettempdir()) / f"nope-{uuid.uuid4()}.log"
        self.assertIsInstance(
            detect_provider(_cfg("claude"), missing), ClaudeProvider,
        )
        self.assertIsInstance(
            detect_provider(_cfg("codex"), missing), CodexProvider,
        )

    def test_empty_transcript_falls_back_to_cfg(self):
        path = Path(tempfile.mkstemp(suffix=".log")[1])
        try:
            self.assertIsInstance(
                detect_provider(_cfg("codex"), path), CodexProvider,
            )
        finally:
            path.unlink()

    def test_pre_session_error_row_still_resolves(self):
        # Codex can emit an `error` row before any thread/turn event.
        # Sniff should keep scanning until it finds a vocabulary match
        # (here: a later `turn.started`) rather than defaulting on the
        # first unrecognised row.
        path = _write_jsonl([
            {"type": "error", "message": "flaky auth"},
            {"type": "turn.started"},
        ])
        try:
            self.assertIsInstance(
                detect_provider(_cfg("claude"), path), CodexProvider,
            )
        finally:
            path.unlink()


class TestDashboardSessionActivityProviderAware(unittest.TestCase):
    """End-to-end: the dashboard's `_session_activity` wrapper must
    route through `detect_provider` so a Codex transcript is no longer
    silently rendered empty just because someone imports the helper
    with a default (Claude) cfg."""

    def test_codex_transcript_yields_feed_via_dashboard_wrapper(self):
        from agentor.dashboard.transcript import _session_activity
        path = _write_jsonl([
            {"type": "thread.started"},
            {"type": "turn.started"},
            {"type": "message", "message": "hello from codex"},
        ])
        try:
            out = _session_activity(_cfg("claude"), path)
        finally:
            path.unlink()
        self.assertIn("·  thread started", out)
        self.assertIn("·  turn 1 started", out)
        self.assertIn("<  hello from codex", out)


if __name__ == "__main__":
    unittest.main()
