import unittest
from pathlib import Path

from agentor.auto_accept import should_auto_accept
from agentor.config import (AgentConfig, Config, GitConfig, ParsingConfig,
                            ReviewConfig, SourcesConfig)
from agentor.models import Item


def _mk_config(mode: str) -> Config:
    return Config(
        project_name="t",
        project_root=Path("/tmp/t"),
        sources=SourcesConfig(),
        parsing=ParsingConfig(),
        agent=AgentConfig(auto_accept_plan=mode),
        git=GitConfig(),
        review=ReviewConfig(),
    )


def _mk_item() -> Item:
    return Item(
        id="abc", title="T", body="B",
        source_file="backlog.md", source_line=1, tags={},
    )


class TestShouldAutoAccept(unittest.TestCase):
    def test_off_rejects(self):
        decision, reason = should_auto_accept(_mk_config("off"), _mk_item())
        self.assertFalse(decision)
        self.assertEqual(reason, "off")

    def test_always_accepts(self):
        decision, reason = should_auto_accept(_mk_config("always"), _mk_item())
        self.assertTrue(decision)
        self.assertEqual(reason, "always")

    def test_unknown_falls_back_to_off(self):
        decision, reason = should_auto_accept(
            _mk_config("bogus-value-xyz"), _mk_item(),
        )
        self.assertFalse(decision)
        self.assertEqual(reason, "off")

    def test_default_config_is_off(self):
        cfg = Config(
            project_name="t",
            project_root=Path("/tmp/t"),
            sources=SourcesConfig(),
            parsing=ParsingConfig(),
            agent=AgentConfig(),  # defaults
            git=GitConfig(),
            review=ReviewConfig(),
        )
        decision, _ = should_auto_accept(cfg, _mk_item())
        self.assertFalse(decision)


if __name__ == "__main__":
    unittest.main()
