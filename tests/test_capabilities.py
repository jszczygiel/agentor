"""Tests for `agentor.capabilities` — declarative per-provider flags.

Pins each provider's flag values and the `capabilities_for` dispatch so
a future refactor can't silently flip `supports_mid_run_injection` on
codex (and suddenly the checkpoint emitter starts writing to a closed
stdin) or drop `reports_context_window` from claude (dashboard CTX%
would go silent).
"""
from __future__ import annotations

import unittest

from agentor.capabilities import (
    CLAUDE_CAPS,
    CODEX_CAPS,
    STUB_CAPS,
    ProviderCapabilities,
    capabilities_for,
)


class TestClaudeCaps(unittest.TestCase):
    def test_flags(self):
        self.assertTrue(CLAUDE_CAPS.supports_mid_run_injection)
        self.assertTrue(CLAUDE_CAPS.reports_context_window)
        self.assertTrue(CLAUDE_CAPS.reports_output_tokens_per_turn)
        self.assertEqual(CLAUDE_CAPS.result_source, "stdout_json")
        self.assertTrue(CLAUDE_CAPS.requires_explicit_session_arg)
        self.assertEqual(CLAUDE_CAPS.resume_arg_name, "--resume")


class TestCodexCaps(unittest.TestCase):
    def test_flags(self):
        self.assertFalse(CODEX_CAPS.supports_mid_run_injection)
        self.assertFalse(CODEX_CAPS.reports_context_window)
        self.assertFalse(CODEX_CAPS.reports_output_tokens_per_turn)
        self.assertEqual(CODEX_CAPS.result_source, "output_file")
        self.assertFalse(CODEX_CAPS.requires_explicit_session_arg)
        self.assertIsNone(CODEX_CAPS.resume_arg_name)


class TestStubCaps(unittest.TestCase):
    def test_flags(self):
        # Stub has no real session / streaming / output file; all defaults
        # are False so callers that gate on a capability skip cleanly.
        self.assertFalse(STUB_CAPS.supports_mid_run_injection)
        self.assertFalse(STUB_CAPS.reports_context_window)
        self.assertFalse(STUB_CAPS.reports_output_tokens_per_turn)
        # "stdout_json" per reviewer approval — keeps the Literal union to
        # the two values the ticket spec declared, even though stub
        # doesn't actually resolve a result_text either way.
        self.assertEqual(STUB_CAPS.result_source, "stdout_json")
        self.assertFalse(STUB_CAPS.requires_explicit_session_arg)
        self.assertIsNone(STUB_CAPS.resume_arg_name)


class TestDataclassImmutable(unittest.TestCase):
    def test_frozen(self):
        # Frozen dataclass — class-level constants must not be mutable
        # from test code or runtime code (capability flags are load-
        # bearing for multiple gating decisions).
        with self.assertRaises(Exception):
            CLAUDE_CAPS.supports_mid_run_injection = False  # type: ignore[misc]


class TestCapabilitiesFor(unittest.TestCase):
    def test_dispatch_by_name(self):
        self.assertIs(capabilities_for("stub"), STUB_CAPS)
        self.assertIs(capabilities_for("claude"), CLAUDE_CAPS)
        self.assertIs(capabilities_for("codex"), CODEX_CAPS)

    def test_case_insensitive(self):
        # Mirrors `make_runner`'s `kind.lower()` behaviour so operators
        # writing `runner = "Claude"` in agentor.toml get the same
        # capabilities as `runner = "claude"`.
        self.assertIs(capabilities_for("Claude"), CLAUDE_CAPS)
        self.assertIs(capabilities_for("CODEX"), CODEX_CAPS)

    def test_unknown_raises(self):
        with self.assertRaises(ValueError) as cm:
            capabilities_for("gemini")
        self.assertIn("gemini", str(cm.exception))

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            capabilities_for("")


class TestRunnerBinding(unittest.TestCase):
    """Each runner subclass binds the matching capability constant as a
    class attribute — regression against a future `Runner` subclass
    forgetting the binding and silently inheriting `STUB_CAPS`."""

    def test_claude_runner_bound(self):
        from agentor.runner import ClaudeRunner
        self.assertIs(ClaudeRunner.capabilities, CLAUDE_CAPS)

    def test_codex_runner_bound(self):
        from agentor.runner import CodexRunner
        self.assertIs(CodexRunner.capabilities, CODEX_CAPS)

    def test_stub_runner_bound(self):
        from agentor.runner import StubRunner
        self.assertIs(StubRunner.capabilities, STUB_CAPS)


if __name__ == "__main__":
    unittest.main()
