import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from agentor.dashboard.formatters import _token_windows_invalidate
from agentor.dashboard.render import (
    ACTIONS,
    _render,
    _render_table,
    _render_token_panel,
)
from agentor.models import Item, ItemStatus
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

    def erase(self):
        self.lines.clear()

    def refresh(self):
        pass


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


class TestRenderStatusLineTokenIndicator(unittest.TestCase):
    """The compact `tok sess=… wk=…` indicator is appended to the main status
    line so cumulative session + weekly spend is readable at a glance without
    scanning the full token panel."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.store = Store(Path(self.td.name) / "state.db")
        item = Item(id="a", title="t", body="", source_file="s.md",
                    source_line=1, tags={})
        self.store.upsert_discovered(item)
        # 1234 + 56 + 78000 + 9 = 79299 → "79.3k" via _fmt_tokens.
        self.store.update_result_json("a", json.dumps({
            "usage": {
                "input_tokens": 1234,
                "output_tokens": 56,
                "cache_read_input_tokens": 78000,
                "cache_creation_input_tokens": 9,
            },
        }))
        # Shared across tests — clear so our known totals aren't masked by
        # a prior run's cached result (see `_TOKEN_CACHE_TTL_S`).
        _token_windows_invalidate()

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _render_once(self, width: int = 200):
        stdscr = _FakeStdscr(width=width)
        cfg = SimpleNamespace(
            project_name="demo",
            agent=SimpleNamespace(runner="stub", pool_size=1,
                                  context_window=200_000),
        )
        daemon = SimpleNamespace(
            stats=SimpleNamespace(completed=0),
            system_alert=None,
            started_at=0.0,
            workers=set(),
        )
        with patch("agentor.dashboard.render.curses.color_pair",
                   return_value=0), \
             patch("agentor.dashboard.render._set_terminal_title"):
            _render(stdscr, cfg, self.store, daemon, log_ring=[],
                    filter_idx=0, selected_id=None)
        return stdscr.lines

    def test_status_line_contains_compact_indicator(self):
        lines = self._render_once()
        joined = "\n".join(s for _, s in lines)
        # Session (daemon not started → mirrors today) and 7d windows
        # both include the only seeded item → totals match.
        self.assertIn("tok sess=79.3k", joined)
        self.assertIn("wk=79.3k", joined)

    def test_indicator_lives_on_status_line_not_panel(self):
        # The panel row for "session" starts with " session" (leading space
        # from _render_token_panel). The compact indicator must be on the
        # *preceding* status line — i.e. the line with `pool=`.
        lines = self._render_once()
        status_lines = [s for _, s in lines if "pool=" in s]
        self.assertEqual(len(status_lines), 1)
        self.assertIn("tok sess=", status_lines[0])
        self.assertIn("wk=", status_lines[0])


class TestPriorityGlyph(unittest.TestCase):
    """Main-table rendering must flag priority>0 rows with a `*` glyph
    adjacent to the title, while priority==0 rows reserve a blank slot so
    title columns stay aligned between pinned and unpinned rows."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.store = Store(Path(self.td.name) / "state.db")
        self.pinned = Item(id="pinned_id", title="PinnedTitle", body="",
                           source_file="s.md", source_line=1, tags={})
        self.plain = Item(id="plain_id", title="PlainTitle", body="",
                          source_file="s.md", source_line=2, tags={})
        self.store.upsert_discovered(self.pinned)
        self.store.upsert_discovered(self.plain)
        self.store.bump_priority("pinned_id", 1)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _render(self, width: int = 120):
        stdscr = _FakeStdscr(width=width)
        # curses.color_pair requires initscr(); stub it for unit-level
        # coverage since we only care about the textual line content.
        with patch("agentor.dashboard.render.curses.color_pair",
                   return_value=0):
            _render_table(stdscr, self.store, top=0, height=10, w=width,
                          statuses=[ItemStatus.QUEUED],
                          context_window=200_000, selected_id=None)
        return stdscr.lines

    def test_priority_glyph_present_for_pinned_row(self):
        lines = self._render()
        pinned_line = next(s for _, s in lines if "PinnedTitle" in s)
        # Glyph + space precedes the title.
        self.assertIn("* PinnedTitle", pinned_line)

    def test_no_glyph_for_unpinned_row(self):
        lines = self._render()
        plain_line = next(s for _, s in lines if "PlainTitle" in s)
        # No `*` anywhere on the unpinned row — would be a false positive.
        self.assertNotIn("*", plain_line)
        # Title is preceded by "  " (blank glyph + separator space).
        self.assertIn("  PlainTitle", plain_line)

    def test_titles_align_across_priorities(self):
        lines = self._render()
        pinned_line = next(s for _, s in lines if "PinnedTitle" in s)
        plain_line = next(s for _, s in lines if "PlainTitle" in s)
        self.assertEqual(
            pinned_line.index("PinnedTitle"),
            plain_line.index("PlainTitle"),
            "pinned and plain rows must align titles at the same column",
        )


if __name__ == "__main__":
    unittest.main()
