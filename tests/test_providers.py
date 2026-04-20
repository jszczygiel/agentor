"""Per-provider invariants for `Provider.model_aliases` and
`Provider.model_to_alias`. Mirrors the runner-level tests in
`test_runner.py::TestAliasMapShape` / `TestResolveExecuteTier` but
narrows to the provider surface itself — no Config or StoredItem."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agentor.config import (AgentConfig, Config, GitConfig, ParsingConfig,
                            ReviewConfig, SourcesConfig)
from agentor.providers import (ClaudeProvider, CodexProvider, Provider,
                               StubProvider, make_provider)


def _cfg(root: Path | None = None) -> Config:
    return Config(
        project_name="p", project_root=root or Path("/tmp/never"),
        sources=SourcesConfig(watch=[], exclude=[]),
        parsing=ParsingConfig(mode="checkbox"),
        agent=AgentConfig(),
        git=GitConfig(base_branch="main", branch_prefix="agent/"),
        review=ReviewConfig(),
    )


class TestProviderModelAliases(unittest.TestCase):
    def test_base_provider_has_empty_default(self):
        # Subclasses must declare their own; the base is intentionally
        # empty so a new provider that forgets to populate gets caught
        # by the runner's `next(iter(...))` fallback.
        self.assertEqual(Provider.model_aliases, {})

    def test_claude_aliases_populated(self):
        self.assertIn("haiku", ClaudeProvider.model_aliases)
        self.assertIn("sonnet", ClaudeProvider.model_aliases)
        self.assertIn("opus", ClaudeProvider.model_aliases)
        self.assertTrue(
            ClaudeProvider.model_aliases["haiku"].startswith("claude-haiku-"),
        )

    def test_codex_aliases_are_not_claude_shaped(self):
        for alias, mid in CodexProvider.model_aliases.items():
            self.assertFalse(
                mid.startswith("claude-"),
                msg=f"Codex alias {alias!r} → {mid!r} looks Claude-shaped",
            )

    def test_stub_mirrors_claude_for_test_ergonomics(self):
        # Tests routinely default `AgentConfig.runner` to "stub";
        # mirroring Claude's aliases keeps `@model:haiku` resolvable
        # without forcing every test cfg to pin `runner="claude"`.
        self.assertEqual(
            StubProvider.model_aliases, ClaudeProvider.model_aliases,
        )


class TestModelToAlias(unittest.TestCase):
    """`model_to_alias` is the reverse lookup `_resolve_execute_tier`
    uses to derive a default alias from `agent.model`. The base class
    does exact-match only; `ClaudeProvider` adds a prefix fallback for
    rotation lag (`claude-opus-4-6` → `opus` even after the alias map
    rotates to `claude-opus-4-7`)."""

    def test_base_exact_match_only(self):
        p = MagicMock(spec=Provider)
        p.model_aliases = {"a": "model-a", "b": "model-b"}
        self.assertEqual(
            Provider.model_to_alias(p, "model-a"), "a",
        )
        self.assertIsNone(Provider.model_to_alias(p, "model-unknown"))
        self.assertIsNone(Provider.model_to_alias(p, ""))

    def test_claude_prefix_fallback_for_rotation_lag(self):
        # Operator's `agent.model = "claude-opus-4-6"` (pre-rotation id)
        # must still resolve to `opus` after the alias map has moved on.
        p = ClaudeProvider(_cfg())
        self.assertEqual(p.model_to_alias("claude-opus-4-6"), "opus")
        self.assertEqual(p.model_to_alias("claude-haiku-9-9"), "haiku")
        self.assertIsNone(p.model_to_alias("gpt-5.4"))

    def test_claude_exact_match_wins_over_prefix(self):
        p = ClaudeProvider(_cfg())
        # The currently-bound id must round-trip exactly.
        self.assertEqual(
            p.model_to_alias(ClaudeProvider.model_aliases["sonnet"]),
            "sonnet",
        )

    def test_codex_exact_match_only(self):
        # No prefix fallback — Codex has no well-known family-name
        # shape in its model ids (`gpt-5.4`, `gpt-5.4-mini`, `o4-mini`, …).
        p = CodexProvider(_cfg())
        for alias, mid in CodexProvider.model_aliases.items():
            self.assertEqual(p.model_to_alias(mid), alias)
        self.assertIsNone(p.model_to_alias("claude-opus-4-7"))


class TestMakeProvider(unittest.TestCase):
    """`make_provider` dispatch mirrors `make_runner` — refactor didn't
    touch the shape here, but the alias-map refactor now means each
    returned provider carries a distinct map, worth one smoke test."""

    def test_make_provider_returns_right_aliases(self):
        cfg = _cfg()
        cfg.agent.runner = "claude"
        self.assertEqual(
            make_provider(cfg).model_aliases,
            ClaudeProvider.model_aliases,
        )
        cfg.agent.runner = "codex"
        self.assertEqual(
            make_provider(cfg).model_aliases,
            CodexProvider.model_aliases,
        )
        cfg.agent.runner = "stub"
        self.assertEqual(
            make_provider(cfg).model_aliases,
            StubProvider.model_aliases,
        )


class TestInvokeOneShot(unittest.TestCase):
    """`Provider.invoke_one_shot` is the dashboard bug-bash expander's
    entry point — each concrete provider renders the call its own way.
    Base class raises NotImplementedError so new providers can't silently
    skip the impl."""

    def test_base_raises_not_implemented(self):
        p = MagicMock(spec=Provider)
        with self.assertRaises(NotImplementedError):
            Provider.invoke_one_shot(p, "hi", timeout=1.0)

    def test_stub_raises_not_implemented(self):
        # Dashboard never runs under `runner="stub"` in production;
        # the stub opts out explicitly so a misrouted call surfaces.
        p = StubProvider(_cfg())
        with self.assertRaises(NotImplementedError):
            p.invoke_one_shot("hi", timeout=1.0)

    def test_claude_builds_expected_argv_and_returns_stdout(self):
        p = ClaudeProvider(_cfg(root=Path("/tmp/_agentor_oneshot")))
        cp = MagicMock(returncode=0, stdout=" expanded\n", stderr="")
        with patch(
            "agentor.providers.subprocess.run", return_value=cp,
        ) as run:
            out = p.invoke_one_shot("the prompt", timeout=30.0)
        self.assertEqual(out, "expanded")
        args, kwargs = run.call_args
        self.assertEqual(
            args[0],
            ["claude", "-p", "the prompt", "--dangerously-skip-permissions"],
        )
        self.assertEqual(kwargs["cwd"], "/tmp/_agentor_oneshot")
        self.assertEqual(kwargs["timeout"], 30.0)
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])

    def test_claude_filenotfound_maps_to_runtime_error(self):
        p = ClaudeProvider(_cfg())
        with patch(
            "agentor.providers.subprocess.run",
            side_effect=FileNotFoundError(),
        ):
            with self.assertRaisesRegex(RuntimeError, "claude CLI not found"):
                p.invoke_one_shot("hi", timeout=1.0)

    def test_claude_timeout_maps_to_runtime_error(self):
        p = ClaudeProvider(_cfg())
        with patch(
            "agentor.providers.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=5),
        ):
            with self.assertRaisesRegex(RuntimeError, "claude timed out"):
                p.invoke_one_shot("hi", timeout=5.0)

    def test_claude_nonzero_exit_maps_to_runtime_error(self):
        p = ClaudeProvider(_cfg())
        cp = MagicMock(
            returncode=1, stdout="", stderr="boom\nbad thing happened",
        )
        with patch("agentor.providers.subprocess.run", return_value=cp):
            with self.assertRaisesRegex(RuntimeError, "bad thing"):
                p.invoke_one_shot("hi", timeout=1.0)

    def test_claude_empty_stdout_maps_to_runtime_error(self):
        p = ClaudeProvider(_cfg())
        cp = MagicMock(returncode=0, stdout="   \n", stderr="")
        with patch("agentor.providers.subprocess.run", return_value=cp):
            with self.assertRaisesRegex(RuntimeError, "empty output"):
                p.invoke_one_shot("hi", timeout=1.0)

    def test_codex_writes_prompt_argv_and_reads_output_file(self):
        # Codex one-shot: verify the CLI argv uses `-o <tmp>` + bare
        # prompt (no --json, no -m), and the contents of the written
        # output file flow back as the return value. Uses a real tmp
        # dir so the file-read path is genuinely exercised.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = CodexProvider(_cfg(root=root))
            observed: dict = {}

            def fake_run(cmd, **kwargs):
                observed["cmd"] = cmd
                observed["cwd"] = kwargs.get("cwd")
                # Find the path argv[-o] and write the "final message"
                # there, simulating codex behaviour.
                i = cmd.index("-o")
                Path(cmd[i + 1]).write_text("final message body\n")
                return MagicMock(returncode=0, stdout="", stderr="")

            with patch("agentor.providers.subprocess.run",
                       side_effect=fake_run):
                out = p.invoke_one_shot("prompt text", timeout=60.0)

            self.assertEqual(out, "final message body")
            cmd = observed["cmd"]
            self.assertEqual(cmd[0], "codex")
            self.assertEqual(cmd[1], "exec")
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)
            self.assertIn("-o", cmd)
            # Final argv slot is the bare prompt string.
            self.assertEqual(cmd[-1], "prompt text")
            # No `--json`, no `-m` / model flag for one-shot.
            self.assertNotIn("--json", cmd)
            self.assertNotIn("-m", cmd)
            self.assertEqual(observed["cwd"], str(root))

    def test_codex_filenotfound_maps_to_runtime_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = CodexProvider(_cfg(root=Path(td)))
            with patch(
                "agentor.providers.subprocess.run",
                side_effect=FileNotFoundError(),
            ):
                with self.assertRaisesRegex(RuntimeError,
                                            "codex CLI not found"):
                    p.invoke_one_shot("hi", timeout=1.0)

    def test_codex_cleans_up_tmp_on_success(self):
        # The tmp file must be unlinked even on the happy path so the
        # `.agentor/tmp/` dir doesn't fill with per-capture leftovers.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = CodexProvider(_cfg(root=root))
            written: list[Path] = []

            def fake_run(cmd, **kwargs):
                i = cmd.index("-o")
                out_path = Path(cmd[i + 1])
                out_path.write_text("done\n")
                written.append(out_path)
                return MagicMock(returncode=0, stdout="", stderr="")

            with patch("agentor.providers.subprocess.run",
                       side_effect=fake_run):
                p.invoke_one_shot("hi", timeout=1.0)

            self.assertEqual(len(written), 1)
            self.assertFalse(
                written[0].exists(),
                msg="codex one-shot left tmp output file behind",
            )

    def test_codex_cleans_up_tmp_on_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = CodexProvider(_cfg(root=root))
            written: list[Path] = []

            def fake_run(cmd, **kwargs):
                i = cmd.index("-o")
                out_path = Path(cmd[i + 1])
                out_path.write_text("")
                written.append(out_path)
                return MagicMock(returncode=2, stdout="", stderr="kaboom")

            with patch("agentor.providers.subprocess.run",
                       side_effect=fake_run):
                with self.assertRaises(RuntimeError):
                    p.invoke_one_shot("hi", timeout=1.0)

            self.assertFalse(
                written[0].exists(),
                msg="codex one-shot left tmp file after failure",
            )

    def test_codex_empty_output_file_maps_to_runtime_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = CodexProvider(_cfg(root=Path(td)))

            def fake_run(cmd, **kwargs):
                i = cmd.index("-o")
                Path(cmd[i + 1]).write_text("")
                return MagicMock(returncode=0, stdout="", stderr="")

            with patch("agentor.providers.subprocess.run",
                       side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, "empty output"):
                    p.invoke_one_shot("hi", timeout=1.0)


if __name__ == "__main__":
    unittest.main()
