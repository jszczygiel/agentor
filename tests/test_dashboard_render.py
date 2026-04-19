import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from agentor.dashboard.render import ACTIONS, _render_token_panel
from agentor.models import Item
from agentor.store import Store


class _FakeStdscr:
    """Captures lines passed to `_safe_addstr` so we can grep the output."""

    def __init__(self, width: int = 120):
        self._width = width
        self.lines: list[tuple[int, str]] = []

    def getmaxyx(self):
        return (40, self._width)

    def addnstr(self, y, x, s, w, attr=0):
        self.lines.append((y, s[:w]))


class TestActionsHint(unittest.TestCase):
    def test_unpause_not_advertised(self):
        self.assertNotIn("[u]npause", ACTIONS)
        self.assertNotIn("unpause", ACTIONS)

    def test_core_actions_present(self):
        for key in ("[r]eview", "[d]eferred", "[i]nspect",
                    "[tab]filter", "[+/-]pool", "[q]uit"):
            self.assertIn(key, ACTIONS)

    def test_removed_pickup_mode_actions_gone(self):
        # Regression guard: pickup walk (`p`) and pickup-mode toggle (`m`)
        # were removed when auto-dispatch became the only mode.
        self.assertNotIn("[p]ickup", ACTIONS)
        self.assertNotIn("[m]ode", ACTIONS)

    def test_double_space_separators(self):
        # single-space separators between words would compress the layout
        # and mislead operators about which tokens are grouped.
        self.assertNotIn("] [", ACTIONS)


class TestRenderTokenPanel(unittest.TestCase):
    """Smoke: the panel writes a header and one line per window into the
    curses surface. Runs against a real Store so the SELECT path executes."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.store = Store(Path(self.td.name) / "state.db")
        item = Item(id="a", title="t", body="", source_file="s.md",
                    source_line=1, tags={})
        self.store.upsert_discovered(item)
        self.store.update_result_json("a", json.dumps({
            "usage": {
                "input_tokens": 1234,
                "output_tokens": 56,
                "cache_read_input_tokens": 78000,
                "cache_creation_input_tokens": 9,
            },
        }))

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_panel_draws_header_and_three_windows(self):
        stdscr = _FakeStdscr()
        daemon = SimpleNamespace(started_at=0.0)
        next_row = _render_token_panel(stdscr, 0, 120, self.store, daemon)
        # 1 header + 3 window rows = row pointer advances by 4.
        self.assertEqual(next_row, 4)
        joined = "\n".join(s for _, s in stdscr.lines)
        self.assertIn("tokens", joined)
        self.assertIn("session", joined)
        self.assertIn("today", joined)
        self.assertIn("7d", joined)
        # Values are formatted via _fmt_tokens — 78000 → "78.0k".
        self.assertIn("78.0k", joined)


if __name__ == "__main__":
    unittest.main()
