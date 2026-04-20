"""Per-provider placeholder validation for `agent.command` /
`agent.resume_command`. Hard errors on foreign or missing-required
tokens; soft-warn (collected on `Config._command_warnings`) for missing
optional tokens. Mirrors the TOML-loader warning path without touching
stdout/stderr at test time.
"""
from __future__ import annotations

import io
import textwrap
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory

from agentor.config import (AgentConfig, Config, GitConfig, ParsingConfig,
                            ReviewConfig, SourcesConfig, load)
from agentor.providers import (ClaudeProvider, CodexProvider, PlaceholderSchema,
                               StubProvider, validate_agent_command)


def _mk_config(
    root: Path,
    *,
    runner: str,
    command: list[str] | None = None,
    resume_command: list[str] | None = None,
) -> Config:
    return Config(
        project_name="p", project_root=root,
        sources=SourcesConfig(watch=[], exclude=[]),
        parsing=ParsingConfig(mode="checkbox"),
        agent=AgentConfig(
            runner=runner,
            command=list(command or []),
            resume_command=list(resume_command or []),
        ),
        git=GitConfig(), review=ReviewConfig(),
    )


class TestPerProviderSchemas(unittest.TestCase):
    """Each provider must own its own placeholder schema — mixing them up
    is the whole bug this validator prevents."""

    def test_claude_command_schema(self):
        s = ClaudeProvider.command_placeholders
        self.assertEqual(s.required, frozenset())
        self.assertEqual(s.optional,
                         frozenset({"prompt", "model", "settings_path"}))
        self.assertEqual(s.allowed, s.optional)

    def test_claude_refuses_resume_command(self):
        # Claude appends `--resume <id>` at runtime rather than templating
        # it in, so `resume_command_placeholders` is None.
        self.assertIsNone(ClaudeProvider.resume_command_placeholders)

    def test_codex_command_requires_prompt(self):
        s = CodexProvider.command_placeholders
        self.assertIn("prompt", s.required)
        self.assertIn("output_path", s.optional)
        self.assertNotIn("settings_path", s.allowed)
        self.assertNotIn("session_id", s.allowed)

    def test_codex_resume_requires_session_and_prompt(self):
        s = CodexProvider.resume_command_placeholders
        assert s is not None
        self.assertEqual(s.required, frozenset({"session_id", "prompt"}))
        self.assertIn("output_path", s.optional)

    def test_stub_permissive(self):
        # Stub must accept Claude-shaped templates (lots of existing tests
        # use them) and whatever fake-codex fixtures future tests bring.
        s = StubProvider.command_placeholders
        self.assertEqual(s.required, frozenset())
        self.assertTrue({"prompt", "model", "settings_path",
                         "output_path", "session_id"}.issubset(s.allowed))


class TestValidatorHardErrors(unittest.TestCase):
    """ValueError on foreign placeholders, unknown placeholders, missing
    required placeholders, and a resume_command on a claude runner."""

    def test_claude_rejects_output_path(self):
        with self.assertRaisesRegex(ValueError, "output_path"):
            validate_agent_command(
                "claude", ["claude", "{prompt}", "{output_path}"], [],
            )

    def test_claude_rejects_session_id(self):
        with self.assertRaisesRegex(ValueError, "session_id"):
            validate_agent_command(
                "claude", ["claude", "{session_id}", "{prompt}"], [],
            )

    def test_codex_rejects_settings_path(self):
        # Copying a claude command into a codex config and forgetting to
        # strip `--settings {settings_path}` is the canonical footgun.
        with self.assertRaisesRegex(
            ValueError, r"settings_path.*claude-only",
        ):
            validate_agent_command(
                "codex",
                ["codex", "{prompt}", "{settings_path}", "{output_path}"],
                [],
            )

    def test_codex_missing_required_prompt(self):
        with self.assertRaisesRegex(
            ValueError, r"missing required placeholder \{prompt\}",
        ):
            validate_agent_command(
                "codex", ["codex", "{model}", "-o", "{output_path}"], [],
            )

    def test_codex_resume_missing_session_id(self):
        with self.assertRaisesRegex(
            ValueError, r"missing required placeholder \{session_id\}",
        ):
            validate_agent_command(
                "codex",
                ["codex", "{prompt}"],  # valid command
                ["codex", "resume", "{prompt}"],  # missing {session_id}
            )

    def test_claude_resume_command_rejected(self):
        with self.assertRaisesRegex(
            ValueError, "resume_command is set but runner='claude'",
        ):
            validate_agent_command(
                "claude", [], ["anything"],
            )

    def test_unknown_placeholder(self):
        with self.assertRaisesRegex(ValueError, "unknown placeholder"):
            validate_agent_command(
                "codex",
                ["codex", "{prompt}", "{not_a_real_token}"],
                [],
            )


class TestValidatorSoftWarnings(unittest.TestCase):
    def test_missing_optional_settings_path_warns(self):
        warns = validate_agent_command(
            "claude", ["claude", "-p", "{prompt}", "--model", "{model}"], [],
        )
        self.assertEqual(len(warns), 1)
        self.assertIn("settings_path", warns[0])

    def test_missing_optional_model_warns(self):
        warns = validate_agent_command(
            "codex", ["codex", "-o", "{output_path}", "{prompt}"], [],
        )
        self.assertEqual(len(warns), 1)
        self.assertIn("{model}", warns[0])

    def test_empty_template_no_warnings(self):
        # Empty (= use default) is always fine; validator short-circuits.
        self.assertEqual(validate_agent_command("claude", [], []), [])
        self.assertEqual(validate_agent_command("codex", [], []), [])

    def test_unknown_runner_silent(self):
        # A bad runner string is surfaced by make_provider; the placeholder
        # validator is not the right place to duplicate that error.
        self.assertEqual(
            validate_agent_command("not-a-runner", ["weird"], []), [],
        )


class TestConfigPostInit(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_valid_config_constructs_silently(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            cfg = _mk_config(self.root, runner="codex",
                             command=["codex", "-m", "{model}",
                                      "-o", "{output_path}", "{prompt}"])
        self.assertEqual(buf.getvalue(), "")
        self.assertEqual(cfg._command_warnings, [])

    def test_hard_error_raises_at_construction(self):
        with self.assertRaisesRegex(ValueError, "claude-only"):
            _mk_config(
                self.root, runner="codex",
                command=["codex", "{settings_path}", "{prompt}"],
            )

    def test_warnings_buffered_silently(self):
        # Direct Config(...) construction (what tests do) must not print
        # soft-warnings to stderr — they're buffered on the instance so
        # `load()` can opt into emitting them.
        buf = io.StringIO()
        with redirect_stderr(buf):
            cfg = _mk_config(self.root, runner="claude",
                             command=["claude", "-p", "{prompt}"])
        self.assertEqual(buf.getvalue(), "")
        # Missing `{model}` and `{settings_path}` → two warnings.
        self.assertEqual(len(cfg._command_warnings), 2)

    def test_defaults_construct_cleanly(self):
        # `AgentConfig()` with defaults (runner=stub, empty command) is
        # the shape every other test uses — must not regress.
        buf = io.StringIO()
        with redirect_stderr(buf):
            cfg = _mk_config(self.root, runner="stub")
        self.assertEqual(cfg._command_warnings, [])
        self.assertEqual(buf.getvalue(), "")


class TestLoaderEmitsWarnings(unittest.TestCase):
    """TOML `load()` is the one construction path that DOES print the
    soft-warnings to stderr — operators loading a custom `agentor.toml`
    should see what per-invocation overrides they've given up."""

    def test_load_prints_soft_warnings(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            toml = root / "agentor.toml"
            toml.write_text(textwrap.dedent("""
                [project]
                name = "p"
                root = "."

                [agent]
                runner = "claude"
                command = ["claude", "-p", "{prompt}"]
            """).strip() + "\n")
            buf = io.StringIO()
            with redirect_stderr(buf):
                cfg = load(toml)
            err = buf.getvalue()
            self.assertIn("agent.command", err)
            self.assertIn("{model}", err)
            self.assertIn("{settings_path}", err)
            self.assertEqual(cfg.agent.runner, "claude")

    def test_load_hard_errors_on_foreign_placeholder(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            toml = root / "agentor.toml"
            toml.write_text(textwrap.dedent("""
                [project]
                name = "p"
                root = "."

                [agent]
                runner = "codex"
                command = ["codex", "{settings_path}", "{prompt}"]
            """).strip() + "\n")
            with self.assertRaisesRegex(ValueError, "claude-only"):
                load(toml)


class TestDefaultCommandsOnProviders(unittest.TestCase):
    """Default command templates moved from runner.py module-level
    functions to provider static methods. Each default must satisfy its
    own schema — an own-dogfood consistency check."""

    def test_claude_default_passes_claude_schema(self):
        # Claude's default uses stream-json stdin, not `-p {prompt}` argv,
        # so `{prompt}` is absent by design. Validator emits one soft warn
        # for that (harmless — the runner pipes the prompt over stdin).
        warns = validate_agent_command(
            "claude", ClaudeProvider.default_command(), [],
        )
        self.assertEqual(len(warns), 1)
        self.assertIn("{prompt}", warns[0])

    def test_codex_default_passes_codex_schema(self):
        warns = validate_agent_command(
            "codex", CodexProvider.default_command(), [],
        )
        self.assertEqual(warns, [])

    def test_codex_resume_default_passes_codex_schema(self):
        warns = validate_agent_command(
            "codex",
            CodexProvider.default_command(),
            CodexProvider.default_resume_command(),
        )
        self.assertEqual(warns, [])


class TestPlaceholderSchemaDataclass(unittest.TestCase):
    def test_allowed_is_union(self):
        s = PlaceholderSchema(
            required=frozenset({"a"}),
            optional=frozenset({"b", "c"}),
        )
        self.assertEqual(s.allowed, frozenset({"a", "b", "c"}))

    def test_frozen(self):
        s = PlaceholderSchema()
        with self.assertRaises(Exception):
            s.required = frozenset({"x"})  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
