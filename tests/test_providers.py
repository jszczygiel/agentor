"""Per-provider invariants for `Provider.model_aliases` and
`Provider.model_to_alias`. Mirrors the runner-level tests in
`test_runner.py::TestAliasMapShape` / `TestResolveExecuteTier` but
narrows to the provider surface itself — no Config or StoredItem."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock

from agentor.config import (AgentConfig, Config, GitConfig, ParsingConfig,
                            ReviewConfig, SourcesConfig)
from agentor.providers import (ClaudeProvider, CodexProvider, Provider,
                               StubProvider, make_provider)


def _cfg() -> Config:
    return Config(
        project_name="p", project_root=Path("/tmp/never"),
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
        self.assertIsNone(p.model_to_alias("gpt-5"))

    def test_claude_exact_match_wins_over_prefix(self):
        p = ClaudeProvider(_cfg())
        # The currently-bound id must round-trip exactly.
        self.assertEqual(
            p.model_to_alias(ClaudeProvider.model_aliases["sonnet"]),
            "sonnet",
        )

    def test_codex_exact_match_only(self):
        # No prefix fallback — Codex has no well-known family-name
        # shape in its model ids (gpt-5, gpt-5-mini, o4-mini, …).
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


if __name__ == "__main__":
    unittest.main()
