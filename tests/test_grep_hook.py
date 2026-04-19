import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from agentor.grep_hook import _REJECTION, decide


class TestDecide(unittest.TestCase):
    def test_content_without_head_limit_denies(self):
        result = decide(
            {"tool_name": "Grep",
             "tool_input": {"output_mode": "content", "pattern": "foo"}},
        )
        self.assertEqual(result["permissionDecision"], "deny")
        self.assertIn("head_limit", result["reason"])
        self.assertEqual(result["reason"], _REJECTION)

    def test_content_with_head_limit_allows(self):
        for limit in (1, 20, 50, 500):
            with self.subTest(head_limit=limit):
                r = decide({
                    "tool_name": "Grep",
                    "tool_input": {"output_mode": "content",
                                   "head_limit": limit},
                })
                self.assertEqual(r["permissionDecision"], "allow")

    def test_content_with_head_limit_zero_allows(self):
        """`head_limit: 0` is the agent's explicit "unlimited" choice —
        trust it; don't second-guess."""
        r = decide({
            "tool_name": "Grep",
            "tool_input": {"output_mode": "content", "head_limit": 0},
        })
        self.assertEqual(r["permissionDecision"], "allow")

    def test_output_mode_count_allows(self):
        r = decide({"tool_name": "Grep",
                    "tool_input": {"output_mode": "count"}})
        self.assertEqual(r["permissionDecision"], "allow")

    def test_output_mode_files_with_matches_allows(self):
        r = decide({"tool_name": "Grep",
                    "tool_input": {"output_mode": "files_with_matches"}})
        self.assertEqual(r["permissionDecision"], "allow")

    def test_output_mode_missing_allows(self):
        # Grep defaults to files_with_matches, already bounded.
        r = decide({"tool_name": "Grep",
                    "tool_input": {"pattern": "foo"}})
        self.assertEqual(r["permissionDecision"], "allow")

    def test_non_grep_tool_allows(self):
        r = decide({"tool_name": "Read",
                    "tool_input": {"output_mode": "content"}})
        self.assertEqual(r["permissionDecision"], "allow")

    def test_malformed_tool_input_allows(self):
        r = decide({"tool_name": "Grep", "tool_input": "not-a-dict"})
        self.assertEqual(r["permissionDecision"], "allow")

    def test_disabled_allows_everything(self):
        r = decide(
            {"tool_name": "Grep",
             "tool_input": {"output_mode": "content"}},
            enabled=False,
        )
        self.assertEqual(r["permissionDecision"], "allow")


class TestHookScriptStdin(unittest.TestCase):
    """Run the hook as a subprocess the way Claude invokes it — JSON on
    stdin, JSON on stdout, exit 2 on deny."""

    def setUp(self):
        self.script = (
            Path(__file__).resolve().parent.parent / "agentor" / "grep_hook.py"
        )

    def _run(self, payload: dict,
             extra_args: list[str] | None = None,
             env_extra: dict[str, str] | None = None,
             ) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(self.script), *(extra_args or [])],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
        )

    def test_deny_exits_with_code_2_and_stderr_reason(self):
        cp = self._run({
            "tool_name": "Grep",
            "tool_input": {"output_mode": "content"},
        })
        self.assertEqual(cp.returncode, 2)
        self.assertIn("head_limit", cp.stderr)
        out = json.loads(cp.stdout)
        self.assertEqual(
            out["hookSpecificOutput"]["permissionDecision"], "deny",
        )

    def test_allow_exits_zero_when_head_limit_present(self):
        cp = self._run({
            "tool_name": "Grep",
            "tool_input": {"output_mode": "content", "head_limit": 50},
        })
        self.assertEqual(cp.returncode, 0)
        out = json.loads(cp.stdout)
        self.assertEqual(
            out["hookSpecificOutput"]["permissionDecision"], "allow",
        )

    def test_allow_files_with_matches(self):
        cp = self._run({
            "tool_name": "Grep",
            "tool_input": {"output_mode": "files_with_matches"},
        })
        self.assertEqual(cp.returncode, 0)

    def test_non_grep_tool_passthrough(self):
        cp = self._run({
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/foo"},
        })
        self.assertEqual(cp.returncode, 0)

    def test_malformed_stdin_fails_open(self):
        cp = subprocess.run(
            [sys.executable, str(self.script)],
            input="not json",
            capture_output=True,
            text=True,
        )
        self.assertEqual(cp.returncode, 0)
        out = json.loads(cp.stdout)
        self.assertEqual(
            out["hookSpecificOutput"]["permissionDecision"], "allow",
        )

    def test_disable_flag_allows_block_payload(self):
        cp = self._run(
            {"tool_name": "Grep",
             "tool_input": {"output_mode": "content"}},
            extra_args=["--disable"],
        )
        self.assertEqual(cp.returncode, 0)

    def test_env_disable_allows_block_payload(self):
        cp = self._run(
            {"tool_name": "Grep",
             "tool_input": {"output_mode": "content"}},
            env_extra={"AGENTOR_GREP_HOOK": "0"},
        )
        self.assertEqual(cp.returncode, 0)


if __name__ == "__main__":
    unittest.main()
