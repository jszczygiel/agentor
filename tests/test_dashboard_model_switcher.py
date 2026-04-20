"""Dashboard [M] model switcher — mode wiring + daemon integration.

Focused on the coordinator `_model_switcher_mode`: patches the overlay
primitive and asserts the daemon's in-memory override mutates as
expected. The overlay's own keystroke handling lives in
`test_dashboard_render.TestModelSwitcherOverlay`.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agentor.config import KNOWN_MODELS
from agentor.dashboard.modes import _model_switcher_mode
from agentor.dashboard.render import _MODEL_OVERRIDE_CLEAR


def _cfg(runner: str = "claude") -> SimpleNamespace:
    return SimpleNamespace(agent=SimpleNamespace(runner=runner,
                                                 model="claude-opus-4-7"))


def _daemon(override: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(model_override=override)


class TestModelSwitcherMode(unittest.TestCase):
    def test_pick_sets_daemon_override(self):
        daemon = _daemon(None)
        with patch("agentor.dashboard.modes._prompt_model_switcher",
                   return_value="claude-haiku-4-5") as picker, \
             patch("agentor.dashboard.modes._flash"):
            _model_switcher_mode(stdscr=object(), cfg=_cfg(), daemon=daemon)
        self.assertEqual(daemon.model_override, "claude-haiku-4-5")
        # Picker should have been called with the current runner's rows
        # and the pre-existing override (None here).
        args, kwargs = picker.call_args
        rows_arg = args[1]
        self.assertEqual(rows_arg, KNOWN_MODELS["claude"])
        self.assertIsNone(args[2])
        self.assertEqual(args[3], "claude")

    def test_cancel_leaves_override_unchanged(self):
        daemon = _daemon("claude-sonnet-4-6")
        with patch("agentor.dashboard.modes._prompt_model_switcher",
                   return_value=None), \
             patch("agentor.dashboard.modes._flash"):
            _model_switcher_mode(stdscr=object(), cfg=_cfg(), daemon=daemon)
        self.assertEqual(daemon.model_override, "claude-sonnet-4-6")

    def test_clear_sentinel_resets_override(self):
        daemon = _daemon("claude-haiku-4-5")
        with patch("agentor.dashboard.modes._prompt_model_switcher",
                   return_value=_MODEL_OVERRIDE_CLEAR), \
             patch("agentor.dashboard.modes._flash"):
            _model_switcher_mode(stdscr=object(), cfg=_cfg(), daemon=daemon)
        self.assertIsNone(daemon.model_override)

    def test_current_override_passed_to_picker(self):
        daemon = _daemon("claude-opus-4-7")
        with patch("agentor.dashboard.modes._prompt_model_switcher",
                   return_value=None) as picker, \
             patch("agentor.dashboard.modes._flash"):
            _model_switcher_mode(stdscr=object(), cfg=_cfg(), daemon=daemon)
        args, _ = picker.call_args
        self.assertEqual(args[2], "claude-opus-4-7")

    def test_stub_runner_has_no_rows(self):
        daemon = _daemon(None)
        with patch("agentor.dashboard.modes._prompt_model_switcher",
                   return_value=None) as picker, \
             patch("agentor.dashboard.modes._flash"):
            _model_switcher_mode(stdscr=object(), cfg=_cfg("stub"),
                                 daemon=daemon)
        args, _ = picker.call_args
        self.assertEqual(args[1], [])
        self.assertIsNone(daemon.model_override)


if __name__ == "__main__":
    unittest.main()
