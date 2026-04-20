import json
import tempfile
import unittest
import uuid
from pathlib import Path

from agentor.config import (
    AgentConfig, Config, GitConfig, ParsingConfig, ReviewConfig, SourcesConfig,
)
from agentor.providers import ClaudeProvider


def _cfg() -> Config:
    return Config(
        project_name="p", project_root=Path("/tmp/never"),
        sources=SourcesConfig(watch=[], exclude=[]),
        parsing=ParsingConfig(mode="checkbox"),
        agent=AgentConfig(runner="claude"),
        git=GitConfig(base_branch="main", branch_prefix="agent/"),
        review=ReviewConfig(),
    )


def _build_primer(path: Path) -> str | None:
    return ClaudeProvider(_cfg()).build_primer(path)


def _assistant_text(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "model": "claude-opus-4-7",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "content": [{"type": "text", "text": text}],
        },
    }


def _tool_call(name: str, inp: dict, tool_id: str | None = None) -> dict:
    tid = tool_id or f"tool-{uuid.uuid4().hex[:8]}"
    return {
        "type": "assistant",
        "message": {
            "model": "claude-opus-4-7",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "content": [{
                "type": "tool_use", "id": tid, "name": name, "input": inp,
            }],
        },
    }


def _tool_result(tool_id: str, text: str, is_error: bool = False) -> dict:
    return {
        "type": "user",
        "message": {
            "content": [{
                "type": "tool_result", "tool_use_id": tool_id,
                "content": text, "is_error": is_error,
            }],
        },
    }


def _write_transcript(events: list[dict]) -> Path:
    f = tempfile.NamedTemporaryFile(
        "w", suffix=".log", delete=False, encoding="utf-8",
    )
    f.write("stdout:\n")
    for ev in events:
        f.write(json.dumps(ev))
        f.write("\n")
    f.close()
    return Path(f.name)


class TestBuildPrimer(unittest.TestCase):
    def test_missing_file_returns_none(self):
        missing = Path(tempfile.gettempdir()) / f"nope-{uuid.uuid4()}.log"
        self.assertIsNone(_build_primer(missing))

    def test_below_turn_threshold_returns_none(self):
        path = _write_transcript([
            _tool_call("Read", {"file_path": "a.py"}),
            _tool_call("Read", {"file_path": "b.py"}),
        ])
        # 2 assistant turns < min_turns=3.
        self.assertIsNone(_build_primer(path))

    def test_ten_reads_and_five_greps_listed(self):
        events: list[dict] = []
        for i in range(10):
            events.append(_tool_call(
                "Read", {"file_path": f"scripts/file_{i}.gd"},
                tool_id=f"r{i}",
            ))
            events.append(_tool_result(f"r{i}", f"lines of file_{i}"))
        patterns = ["zoom_level", "WIND_VECTOR", "main_loop", "tile_id", "hud"]
        for i, pat in enumerate(patterns):
            events.append(_tool_call(
                "Grep", {"pattern": pat}, tool_id=f"g{i}",
            ))
            events.append(_tool_result(
                f"g{i}", f"scripts/hit_{pat}_a.gd\nscripts/hit_{pat}_b.gd\n",
            ))
        path = _write_transcript(events)
        primer = _build_primer(path)
        self.assertIsNotNone(primer)
        for i in range(10):
            self.assertIn(f"scripts/file_{i}.gd", primer)
        for pat in patterns:
            self.assertIn(f'"{pat}"', primer)
            self.assertIn(f"scripts/hit_{pat}_a.gd", primer)
            self.assertIn(f"scripts/hit_{pat}_b.gd", primer)
        self.assertIn("## Prior run", primer)

    def test_partial_read_renders_range(self):
        events = [
            _tool_call("Read", {
                "file_path": "scripts/ui/hud.gd",
                "offset": 120, "limit": 60,
            }, tool_id="r1"),
            _tool_result("r1", "..."),
            _tool_call("Read", {"file_path": "scripts/main/game.gd"},
                       tool_id="r2"),
            _tool_result("r2", "..."),
            _tool_call("Read", {"file_path": "scripts/util.gd"},
                       tool_id="r3"),
            _tool_result("r3", "..."),
        ]
        path = _write_transcript(events)
        primer = _build_primer(path)
        self.assertIsNotNone(primer)
        self.assertIn("scripts/ui/hud.gd:120-180", primer)
        self.assertIn("Files read end-to-end:", primer)
        self.assertIn("Files read partially:", primer)

    def test_bash_events_excluded(self):
        events = [
            _tool_call("Bash", {"command": "ls -la"}, tool_id="b1"),
            _tool_result("b1", "total 42\nsecret.key\n"),
            _tool_call("Bash", {"command": "cat /etc/passwd"}, tool_id="b2"),
            _tool_result("b2", "root:x:0:0:ephemeral"),
            _tool_call("Read", {"file_path": "agentor/runner.py"},
                       tool_id="r1"),
            _tool_result("r1", "import os"),
        ]
        path = _write_transcript(events)
        primer = _build_primer(path)
        self.assertIsNotNone(primer)
        self.assertNotIn("ls -la", primer)
        self.assertNotIn("secret.key", primer)
        self.assertNotIn("ephemeral", primer)
        self.assertNotIn("Bash", primer)
        self.assertIn("agentor/runner.py", primer)

    def test_edit_and_write_listed_as_edited(self):
        events = [
            _tool_call("Read", {"file_path": "a.py"}, tool_id="r1"),
            _tool_result("r1", "x"),
            _tool_call("Edit", {
                "file_path": "a.py", "old_string": "x", "new_string": "y",
            }, tool_id="e1"),
            _tool_result("e1", "ok"),
            _tool_call("Write", {
                "file_path": "b.py", "content": "print(1)",
            }, tool_id="w1"),
            _tool_result("w1", "ok"),
        ]
        path = _write_transcript(events)
        primer = _build_primer(path)
        self.assertIsNotNone(primer)
        self.assertIn("Files edited:", primer)
        self.assertIn("- a.py", primer)
        self.assertIn("- b.py", primer)

    def test_empty_useful_content_returns_none(self):
        # Three assistant turns but no tool calls of interest.
        events = [
            _assistant_text("hello"),
            _assistant_text("thinking"),
            _assistant_text("done"),
        ]
        path = _write_transcript(events)
        self.assertIsNone(_build_primer(path))

    def test_grep_without_path_lines_falls_back_to_no_hits(self):
        events = [
            _tool_call("Grep", {"pattern": "foo"}, tool_id="g1"),
            _tool_result("g1", "error: regex parse failed\ntry again"),
            _tool_call("Grep", {"pattern": "bar"}, tool_id="g2"),
            _tool_result("g2", "lib/x.py\nlib/y.py"),
            _tool_call("Read", {"file_path": "z.py"}, tool_id="r1"),
            _tool_result("r1", "ok"),
        ]
        path = _write_transcript(events)
        primer = _build_primer(path)
        self.assertIsNotNone(primer)
        self.assertIn('"foo" -> (no parsed hits)', primer)
        self.assertIn('"bar" -> lib/x.py, lib/y.py', primer)


if __name__ == "__main__":
    unittest.main()
