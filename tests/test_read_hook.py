import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentor.read_hook import decide


def _make_file(root: Path, name: str, lines: int) -> Path:
    path = root / name
    path.write_text("\n".join(f"line{i}" for i in range(lines)))
    return path


class TestDecide(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_large_file_no_offset_denies(self):
        big = _make_file(self.root, "big.py", 500)
        result = decide(
            {"tool_name": "Read", "tool_input": {"file_path": str(big)}},
            threshold=400,
        )
        self.assertEqual(result["permissionDecision"], "deny")
        self.assertIn("500", result["reason"])
        self.assertIn("offset", result["reason"])

    def test_large_file_with_offset_allows(self):
        big = _make_file(self.root, "big.py", 500)
        result = decide(
            {"tool_name": "Read", "tool_input": {
                "file_path": str(big), "offset": 1, "limit": 100,
            }},
            threshold=400,
        )
        self.assertEqual(result["permissionDecision"], "allow")

    def test_large_file_with_limit_only_allows(self):
        big = _make_file(self.root, "big.py", 500)
        result = decide(
            {"tool_name": "Read", "tool_input": {
                "file_path": str(big), "limit": 100,
            }},
            threshold=400,
        )
        self.assertEqual(result["permissionDecision"], "allow")

    def test_small_file_allows(self):
        small = _make_file(self.root, "small.py", 200)
        result = decide(
            {"tool_name": "Read", "tool_input": {"file_path": str(small)}},
            threshold=400,
        )
        self.assertEqual(result["permissionDecision"], "allow")

    def test_missing_file_allows(self):
        result = decide(
            {"tool_name": "Read", "tool_input": {
                "file_path": str(self.root / "does_not_exist.py"),
            }},
            threshold=400,
        )
        self.assertEqual(result["permissionDecision"], "allow")

    def test_non_read_tool_allows(self):
        big = _make_file(self.root, "big.py", 500)
        result = decide(
            {"tool_name": "Edit", "tool_input": {"file_path": str(big)}},
            threshold=400,
        )
        self.assertEqual(result["permissionDecision"], "allow")

    def test_threshold_zero_disables(self):
        big = _make_file(self.root, "big.py", 500)
        result = decide(
            {"tool_name": "Read", "tool_input": {"file_path": str(big)}},
            threshold=0,
        )
        self.assertEqual(result["permissionDecision"], "allow")

    def test_threshold_boundary_equal_allows(self):
        """Files exactly at the threshold are allowed; strictly greater denies."""
        eq = _make_file(self.root, "eq.py", 400)
        result = decide(
            {"tool_name": "Read", "tool_input": {"file_path": str(eq)}},
            threshold=400,
        )
        self.assertEqual(result["permissionDecision"], "allow")

    def test_malformed_tool_input_allows(self):
        result = decide(
            {"tool_name": "Read", "tool_input": "not-a-dict"},
            threshold=400,
        )
        self.assertEqual(result["permissionDecision"], "allow")


class TestHookScriptStdin(unittest.TestCase):
    """Run the hook as a subprocess the way Claude invokes it — JSON on
    stdin, JSON on stdout, exit 2 on deny."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        self.script = (
            Path(__file__).resolve().parent.parent / "agentor" / "read_hook.py"
        )

    def tearDown(self):
        self.td.cleanup()

    def _run(self, payload: dict, threshold: int = 400) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.script), "--threshold", str(threshold)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
        )

    def test_deny_exits_with_code_2_and_stderr_reason(self):
        big = _make_file(self.root, "big.py", 500)
        cp = self._run({
            "tool_name": "Read",
            "tool_input": {"file_path": str(big)},
        })
        self.assertEqual(cp.returncode, 2)
        self.assertIn("500", cp.stderr)
        out = json.loads(cp.stdout)
        self.assertEqual(
            out["hookSpecificOutput"]["permissionDecision"], "deny",
        )

    def test_allow_exits_zero(self):
        small = _make_file(self.root, "small.py", 200)
        cp = self._run({
            "tool_name": "Read",
            "tool_input": {"file_path": str(small)},
        })
        self.assertEqual(cp.returncode, 0)
        out = json.loads(cp.stdout)
        self.assertEqual(
            out["hookSpecificOutput"]["permissionDecision"], "allow",
        )

    def test_malformed_stdin_fails_open(self):
        cp = subprocess.run(
            [sys.executable, str(self.script), "--threshold", "400"],
            input="not json",
            capture_output=True,
            text=True,
        )
        self.assertEqual(cp.returncode, 0)
        out = json.loads(cp.stdout)
        self.assertEqual(
            out["hookSpecificOutput"]["permissionDecision"], "allow",
        )


if __name__ == "__main__":
    unittest.main()
