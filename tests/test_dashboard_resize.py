"""Terminal-resize handling. Without an explicit KEY_RESIZE branch the
stale wide-tier content from before the shrink stayed in the curses
buffer, wrapped in the narrower terminal, and pushed row 0 (title/
header bar) off-screen. The fix forces a full `clear()` +
`update_lines_cols()` so the next tick repaints at the new size."""

import curses
import unittest
from unittest.mock import patch

from agentor.dashboard.render import _handle_resize


class _StubScreen:
    """Minimal stdscr exposing the hooks `_handle_resize` touches."""

    def __init__(self) -> None:
        self.clear_calls = 0

    def clear(self) -> None:
        self.clear_calls += 1


class TestHandleResize(unittest.TestCase):
    def test_resize_clears_and_updates_lines_cols(self):
        scr = _StubScreen()
        with patch.object(curses, "update_lines_cols") as mock_ulc:
            result = _handle_resize(scr, curses.KEY_RESIZE)
        self.assertTrue(result)
        self.assertEqual(scr.clear_calls, 1)
        mock_ulc.assert_called_once_with()

    def test_non_resize_is_no_op(self):
        scr = _StubScreen()
        with patch.object(curses, "update_lines_cols") as mock_ulc:
            result = _handle_resize(scr, ord("q"))
        self.assertFalse(result)
        self.assertEqual(scr.clear_calls, 0)
        mock_ulc.assert_not_called()

    def test_update_lines_cols_optional(self):
        # Windows `windows-curses` historically lacked `update_lines_cols`.
        # `_handle_resize` must still clear — the forced repaint is the
        # load-bearing half of the fix.
        scr = _StubScreen()
        original = getattr(curses, "update_lines_cols", None)
        if original is not None:
            delattr(curses, "update_lines_cols")
        try:
            result = _handle_resize(scr, curses.KEY_RESIZE)
        finally:
            if original is not None:
                curses.update_lines_cols = original  # type: ignore[attr-defined]
        self.assertTrue(result)
        self.assertEqual(scr.clear_calls, 1)


class _LoopStdscr:
    """Scripted stdscr for the main `_loop`. `getch_queue` drives each
    tick; when exhausted the loop sees `q` and exits. Captures every
    `clear()` call so the test can assert the resize path fired."""

    def __init__(self, getch_queue: list[int]) -> None:
        self.getch_queue = list(getch_queue)
        self.clear_calls = 0
        self.erase_calls = 0

    def clear(self) -> None:
        self.clear_calls += 1

    def erase(self) -> None:
        self.erase_calls += 1

    def getmaxyx(self):
        return (30, 40)

    def refresh(self) -> None:
        pass

    def addnstr(self, y, x, s, n, attr=0):
        pass

    def nodelay(self, *a, **k): pass
    def timeout(self, *a, **k): pass

    def getch(self):
        if not self.getch_queue:
            return ord("q")
        return self.getch_queue.pop(0)


class _FakeStats:
    completed = 0


class _FakeAgent:
    runner = "claude"
    pool_size = 0
    context_window = 200_000


class _FakeCfg:
    agent = _FakeAgent()
    project_name = "t"


class _FakeDaemon:
    system_alert = None
    workers: list = []
    stats = _FakeStats()
    started_at = 0.0

    def try_fill_pool(self) -> None:
        pass

    def clear_alert(self) -> None:
        pass


class _FakeStore:
    def count_by_status(self, st):
        return 0

    def list_by_status(self, st):
        return []

    def latest_transition_at(self, *a, **k):
        return None

    def aggregate_token_usage(self, *, since=None):
        return {"input": 0, "output": 0, "cache_read": 0,
                "cache_create": 0, "total": 0}


class TestLoopResize(unittest.TestCase):
    """End-to-end: feed KEY_RESIZE into the main loop and assert it took
    the clear-and-continue path rather than exiting or dispatching a
    random action."""

    def test_key_resize_clears_and_continues(self):
        from collections import deque

        # Queue: first tick returns KEY_RESIZE, second returns 'q' to quit.
        scr = _LoopStdscr([curses.KEY_RESIZE, ord("q")])
        from agentor.dashboard import _loop
        with patch.object(curses, "color_pair", return_value=0), \
             patch.object(curses, "curs_set"), \
             patch.object(curses, "update_lines_cols") as mock_ulc, \
             patch("agentor.dashboard.render._set_terminal_title"):
            _loop(scr, _FakeCfg(), _FakeStore(), _FakeDaemon(), deque())
        # Exactly one resize handled.
        self.assertEqual(scr.clear_calls, 1)
        mock_ulc.assert_called_once_with()


class _InspectStdscr(_LoopStdscr):
    def move(self, *a, **k): pass
    def getstr(self, *a, **k): return b""


class TestInspectResize(unittest.TestCase):
    """Inside the inspect view the same shrink would leave the pre-resize
    detail screen stale. Verify `_inspect_render` routes KEY_RESIZE to a
    clear() rather than interpreting it as a keystroke (which would
    otherwise fall through to `_inspect_dispatch` and, worst case, do
    nothing — but we still want the clear to fire so the next tick
    repaints the content block at the new width)."""

    def test_inspect_key_resize_clears_and_continues(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from agentor.dashboard.modes import _inspect_render
        from agentor.models import Item
        from agentor.store import Store

        td = TemporaryDirectory()
        try:
            store = Store(Path(td.name) / "state.db")
            item = Item(id="a", title="t", body="", source_file="s.md",
                        source_line=1, tags={})
            store.upsert_discovered(item)
            stored = store.get("a")
            assert stored is not None

            class _Cfg:
                class agent:
                    max_attempts = 3
                    runner = "claude"
                    context_window = 200_000

                class git:
                    base_branch = "main"

                project_name = "p"
                project_root = Path(td.name)

            # Queue: KEY_RESIZE first, then 'q' to close the inspect view.
            scr = _InspectStdscr([curses.KEY_RESIZE, ord("q")])
            with patch.object(curses, "update_lines_cols") as mock_ulc:
                result = _inspect_render(scr, _Cfg(), store, stored, None)
            self.assertEqual(result, "quit")
            self.assertEqual(scr.clear_calls, 1)
            mock_ulc.assert_called_once_with()
        finally:
            store.close()
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
