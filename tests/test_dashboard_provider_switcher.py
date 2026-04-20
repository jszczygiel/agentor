"""Dashboard [M] provider switcher — mode wiring + daemon integration.

Focused on the coordinator `_provider_switcher_mode`: patches the overlay
primitive and asserts the daemon's in-memory provider override mutates
as expected. The overlay's own keystroke handling lives in
`test_dashboard_render.TestProviderSwitcherOverlay`.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agentor.config import PROVIDERS
from agentor.dashboard.modes import _provider_switcher_mode
from agentor.dashboard.render import _PROVIDER_OVERRIDE_CLEAR


def _cfg(runner: str = "claude") -> SimpleNamespace:
    return SimpleNamespace(agent=SimpleNamespace(runner=runner,
                                                 model="claude-opus-4-7"))


def _daemon(override: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(provider_override=override)


class TestProviderSwitcherMode(unittest.TestCase):
    def test_pick_sets_daemon_override(self):
        daemon = _daemon(None)
        with patch("agentor.dashboard.modes._prompt_provider_switcher",
                   return_value="codex") as picker, \
             patch("agentor.dashboard.modes._flash"):
            _provider_switcher_mode(stdscr=object(), cfg=_cfg(),
                                    daemon=daemon)
        self.assertEqual(daemon.provider_override, "codex")
        args, _ = picker.call_args
        # Picker is called with the shared PROVIDERS list, the current
        # override (None here), and the configured runner kind.
        self.assertEqual(args[1], list(PROVIDERS))
        self.assertIsNone(args[2])
        self.assertEqual(args[3], "claude")

    def test_cancel_leaves_override_unchanged(self):
        daemon = _daemon("codex")
        with patch("agentor.dashboard.modes._prompt_provider_switcher",
                   return_value=None), \
             patch("agentor.dashboard.modes._flash"):
            _provider_switcher_mode(stdscr=object(), cfg=_cfg(),
                                    daemon=daemon)
        self.assertEqual(daemon.provider_override, "codex")

    def test_clear_sentinel_resets_override(self):
        daemon = _daemon("codex")
        with patch("agentor.dashboard.modes._prompt_provider_switcher",
                   return_value=_PROVIDER_OVERRIDE_CLEAR), \
             patch("agentor.dashboard.modes._flash"):
            _provider_switcher_mode(stdscr=object(), cfg=_cfg(),
                                    daemon=daemon)
        self.assertIsNone(daemon.provider_override)

    def test_current_override_passed_to_picker(self):
        daemon = _daemon("codex")
        with patch("agentor.dashboard.modes._prompt_provider_switcher",
                   return_value=None) as picker, \
             patch("agentor.dashboard.modes._flash"):
            _provider_switcher_mode(stdscr=object(), cfg=_cfg(),
                                    daemon=daemon)
        args, _ = picker.call_args
        self.assertEqual(args[2], "codex")

    def test_providers_list_excludes_stub(self):
        # `stub` is a test fixture, never a user-toggled provider.
        kinds = [k for k, _ in PROVIDERS]
        self.assertNotIn("stub", kinds)
        self.assertIn("claude", kinds)
        self.assertIn("codex", kinds)


if __name__ == "__main__":
    unittest.main()
