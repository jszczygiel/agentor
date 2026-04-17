import json
import tempfile
import unittest
from pathlib import Path

from agentor.transcript import (
    AssistantText,
    AssistantUsage,
    RunResult,
    SessionInit,
    ToolCall,
    ToolResult,
    iter_events,
    iter_raw_events,
    tool_result_text,
)


def _write(lines: list[str]) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8")
    f.write("\n".join(lines))
    f.close()
    return Path(f.name)


class TestIterRawEvents(unittest.TestCase):
    def test_skips_header_blank_and_malformed(self):
        path = _write([
            "cmd: claude -p --input-format stream-json",
            "stdout:",
            "",
            "not-json",
            '{"type": "system", "subtype": "init"}',
            '{bad json',
            '{"type": "result", "num_turns": 3}',
            "stderr:",
        ])
        events = list(iter_raw_events(path))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["type"], "system")
        self.assertEqual(events[1]["num_turns"], 3)

    def test_missing_file_is_empty(self):
        self.assertEqual(list(iter_raw_events(Path("/nonexistent/xyz.log"))), [])

    def test_array_line_skipped(self):
        # Top-level arrays don't decode to dict — skip silently.
        path = _write(['[1, 2, 3]', '{"type": "result"}'])
        events = list(iter_raw_events(path))
        self.assertEqual(len(events), 1)


class TestToolResultText(unittest.TestCase):
    def test_string_passthrough(self):
        self.assertEqual(tool_result_text("hello"), "hello")

    def test_list_of_text_blocks(self):
        content = [
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
        ]
        self.assertEqual(tool_result_text(content), "hello world")

    def test_mixed_list_drops_non_text(self):
        content = [
            {"type": "text", "text": "a"},
            {"type": "image", "source": "..."},
            "bare-string",
            {"type": "text", "text": "b"},
        ]
        self.assertEqual(tool_result_text(content), "abare-stringb")

    def test_none_and_other(self):
        self.assertEqual(tool_result_text(None), "")
        self.assertEqual(tool_result_text(42), "42")


class TestIterEvents(unittest.TestCase):
    def test_session_init_emitted(self):
        path = _write(['{"type": "system", "subtype": "init", "session_id": "s1"}'])
        events = list(iter_events(path))
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], SessionInit)
        self.assertEqual(events[0].raw["session_id"], "s1")

    def test_assistant_mixed_blocks_preserve_order(self):
        msg = {
            "type": "assistant",
            "message": {
                "usage": {"input_tokens": 100},
                "stop_reason": "tool_use",
                "content": [
                    {"type": "text", "text": "Thinking..."},
                    {"type": "tool_use", "id": "t1", "name": "Bash",
                     "input": {"command": "ls"}},
                    {"type": "text", "text": "Now reading"},
                    {"type": "tool_use", "id": "t2", "name": "Read",
                     "input": {"file_path": "/a/b.py"}},
                ],
            },
        }
        path = _write([json.dumps(msg)])
        events = list(iter_events(path))
        self.assertEqual(len(events), 5)
        self.assertIsInstance(events[0], AssistantUsage)
        self.assertEqual(events[0].usage["input_tokens"], 100)
        self.assertEqual(events[0].stop_reason, "tool_use")
        self.assertIsInstance(events[1], AssistantText)
        self.assertEqual(events[1].text, "Thinking...")
        self.assertIsInstance(events[2], ToolCall)
        self.assertEqual(events[2].name, "Bash")
        self.assertEqual(events[2].input["command"], "ls")
        self.assertIsInstance(events[3], AssistantText)
        self.assertEqual(events[3].text, "Now reading")
        self.assertIsInstance(events[4], ToolCall)
        self.assertEqual(events[4].id, "t2")

    def test_empty_text_block_skipped(self):
        msg = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "   "},
                    {"type": "text", "text": "real"},
                ],
            },
        }
        path = _write([json.dumps(msg)])
        events = [e for e in iter_events(path) if isinstance(e, AssistantText)]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].text, "real")

    def test_tool_result_paired_with_prior_tool_use(self):
        assistant = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "tid-42", "name": "Grep",
                     "input": {"pattern": "foo"}},
                ],
            },
        }
        user = {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "tid-42",
                     "content": "match line"},
                ],
            },
        }
        path = _write([json.dumps(assistant), json.dumps(user)])
        results = [e for e in iter_events(path) if isinstance(e, ToolResult)]
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r.tool_use_id, "tid-42")
        self.assertEqual(r.tool_name, "Grep")
        self.assertEqual(r.tool_input["pattern"], "foo")
        self.assertEqual(r.text, "match line")
        self.assertFalse(r.is_error)

    def test_unpaired_tool_result_has_none_name(self):
        user = {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "orphan",
                     "content": "stuff"},
                ],
            },
        }
        path = _write([json.dumps(user)])
        results = [e for e in iter_events(path) if isinstance(e, ToolResult)]
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0].tool_name)
        self.assertEqual(results[0].tool_input, {})
        self.assertEqual(results[0].text, "stuff")

    def test_tool_result_is_error_propagates(self):
        user = {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "x",
                     "content": "boom", "is_error": True},
                ],
            },
        }
        path = _write([json.dumps(user)])
        results = [e for e in iter_events(path) if isinstance(e, ToolResult)]
        self.assertTrue(results[0].is_error)

    def test_tool_result_list_content_flattened(self):
        user = {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "x",
                     "content": [
                         {"type": "text", "text": "line1\n"},
                         {"type": "text", "text": "line2"},
                     ]},
                ],
            },
        }
        path = _write([json.dumps(user)])
        results = [e for e in iter_events(path) if isinstance(e, ToolResult)]
        self.assertEqual(results[0].text, "line1\nline2")

    def test_run_result_populated(self):
        ev = {
            "type": "result",
            "total_cost_usd": 0.42,
            "usage": {"input_tokens": 1, "output_tokens": 2},
            "duration_ms": 1234,
            "num_turns": 7,
            "subtype": "success",
            "is_error": False,
            "result": "done",
        }
        path = _write([json.dumps(ev)])
        events = list(iter_events(path))
        self.assertEqual(len(events), 1)
        r = events[0]
        self.assertIsInstance(r, RunResult)
        self.assertEqual(r.total_cost_usd, 0.42)
        self.assertEqual(r.usage["input_tokens"], 1)
        self.assertEqual(r.duration_ms, 1234)
        self.assertEqual(r.num_turns, 7)
        self.assertEqual(r.subtype, "success")
        self.assertFalse(r.is_error)
        self.assertEqual(r.result, "done")

    def test_run_result_tolerates_missing_fields(self):
        path = _write(['{"type": "result"}'])
        events = list(iter_events(path))
        self.assertEqual(len(events), 1)
        r = events[0]
        self.assertIsNone(r.total_cost_usd)
        self.assertIsNone(r.usage)
        self.assertFalse(r.is_error)

    def test_non_dict_content_blocks_skipped(self):
        msg = {
            "type": "assistant",
            "message": {
                "content": ["stray-string", None, {"type": "text", "text": "kept"}],
            },
        }
        path = _write([json.dumps(msg)])
        texts = [e.text for e in iter_events(path) if isinstance(e, AssistantText)]
        self.assertEqual(texts, ["kept"])

    def test_tool_use_input_non_dict_coerced_empty(self):
        msg = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "t", "name": "X", "input": "oops"},
                ],
            },
        }
        path = _write([json.dumps(msg)])
        calls = [e for e in iter_events(path) if isinstance(e, ToolCall)]
        self.assertEqual(calls[0].input, {})


if __name__ == "__main__":
    unittest.main()
