"""Tests for `_prompt_multiline` in agentor.dashboard.render.

The widget is built on curses primitives, so the tests mock `curses.newwin`
and `curses.textpad.Textbox` to drive the editor without a real tty. The
goal is to verify the contract the callers depend on: Ctrl-G submit returns
typed text, Ctrl-C/Esc and empty submit both return the empty string, and
the widget downgrades to `_prompt_text` on a very small terminal."""

import curses
import unittest
from unittest.mock import patch

from agentor.dashboard import render


class _FakeStdscr:
    """Minimal stdscr stub — only what `_prompt_multiline` touches."""

    def __init__(self, h: int = 30, w: int = 100):
        self._h = h
        self._w = w
        self.nodelay_calls: list[bool] = []
        self.refresh_calls = 0
        self.touchwin_calls = 0

    def getmaxyx(self):
        return (self._h, self._w)

    def nodelay(self, flag):
        self.nodelay_calls.append(flag)

    def touchwin(self):
        self.touchwin_calls += 1

    def refresh(self):
        self.refresh_calls += 1


class _FakeWin:
    """Fake curses window — silently accepts the paint calls the overlay
    makes and records nothing more than the tests need."""

    def __init__(self):
        self.keypad_calls: list[bool] = []

    def bkgd(self, *_a, **_kw): pass
    def box(self): pass
    def addnstr(self, *_a, **_kw): pass
    def refresh(self): pass
    def keypad(self, flag): self.keypad_calls.append(flag)


def _install_fakes(monkey, textbox_factory, newwin_calls=None):
    """Patch the curses entry points `_prompt_multiline` calls. Returns the
    unittest.mock.patch context managers started for cleanup by the caller.

    When `newwin_calls` is a list, each `curses.newwin(...)` call is appended
    so tests can inspect the sizes the overlay requested."""
    def _newwin(*a, **_kw):
        if newwin_calls is not None:
            newwin_calls.append(a)
        return _FakeWin()

    patches = [
        patch.object(curses, "newwin", _newwin),
        patch.object(curses, "curs_set", lambda *_a: 0),
        patch("curses.textpad.Textbox", textbox_factory),
    ]
    for p in patches:
        monkey.enter_context(p)


class _Ctx:
    """Tiny ExitStack-alike so tests can stash several patches."""

    def __init__(self):
        self._exits = []

    def enter_context(self, cm):
        val = cm.__enter__()
        self._exits.append(cm)
        return val

    def close(self):
        while self._exits:
            self._exits.pop().__exit__(None, None, None)


class TestPromptMultiline(unittest.TestCase):
    def setUp(self):
        self.ctx = _Ctx()

    def tearDown(self):
        self.ctx.close()

    def _run(self, *, validator_feed, gathered_text):
        """Drive `_prompt_multiline` end-to-end with a scripted Textbox.
        `validator_feed` is the list of keycodes the fake editor pipes into
        the validator before submitting; `gathered_text` is what
        Textbox.gather() returns."""
        seen = {}

        class FakeTextbox:
            def __init__(self, win):
                self.win = win
                self.stripspaces = True

            def edit(self, validator):
                seen["stripspaces_at_edit"] = self.stripspaces
                for ch in validator_feed:
                    validator(ch)

            def gather(self):
                return gathered_text

        _install_fakes(self.ctx, FakeTextbox)
        stdscr = _FakeStdscr()
        out = render._prompt_multiline(stdscr, "label")
        return out, seen, stdscr

    def test_ctrl_g_submit_returns_typed_text(self):
        # Validator sees no cancel key — Textbox returns the gathered text.
        out, seen, _ = self._run(validator_feed=[], gathered_text="line1\nline2\n")
        self.assertEqual(out, "line1\nline2")
        # stripspaces must be False *before* edit() runs so blank separator
        # lines aren't eaten.
        self.assertIs(seen["stripspaces_at_edit"], False)

    def test_empty_submit_returns_empty(self):
        out, _, _ = self._run(validator_feed=[], gathered_text="   \n  \n")
        self.assertEqual(out, "")

    def test_ctrl_c_cancels(self):
        # Ctrl-C (3) routed through the validator should flag cancel and
        # discard whatever Textbox gathered.
        out, _, _ = self._run(validator_feed=[3], gathered_text="half typed")
        self.assertEqual(out, "")

    def test_esc_cancels(self):
        out, _, _ = self._run(validator_feed=[27], gathered_text="half typed")
        self.assertEqual(out, "")

    def test_backspace_variants_normalized(self):
        # The validator normalizes DEL (127) and Ctrl-H (8) to KEY_BACKSPACE
        # so terminals that send either get the expected edit behavior.
        returned: list[int] = []

        class FakeTextbox:
            def __init__(self, win):
                self.stripspaces = True

            def edit(self, validator):
                for ch in (127, 8, curses.KEY_BACKSPACE):
                    returned.append(validator(ch))

            def gather(self):
                return ""

        _install_fakes(self.ctx, FakeTextbox)
        stdscr = _FakeStdscr()
        render._prompt_multiline(stdscr, "label")
        self.assertEqual(returned,
                         [curses.KEY_BACKSPACE] * 3)

    def _run_capture_newwin(self, *, stdscr, rows=None):
        """Run the widget and return the edit-window size `curses.newwin`
        was called with. The overlay calls newwin twice (frame, then edit
        window) — the edit window is the second call and its first arg is
        the row count."""
        newwin_calls: list[tuple] = []

        class FakeTextbox:
            def __init__(self, win):
                self.stripspaces = True

            def edit(self, validator): pass
            def gather(self): return ""

        _install_fakes(self.ctx, FakeTextbox, newwin_calls=newwin_calls)
        if rows is None:
            render._prompt_multiline(stdscr, "label")
        else:
            render._prompt_multiline(stdscr, "label", rows=rows)
        # frame is first newwin; edit window is second.
        self.assertGreaterEqual(len(newwin_calls), 2)
        edit_rows = newwin_calls[1][0]
        return edit_rows

    def test_default_rows_grow_with_terminal(self):
        # On a 40-row terminal the adaptive default should give well over
        # the old hard-coded 8 rows of edit area.
        edit_rows = self._run_capture_newwin(stdscr=_FakeStdscr(h=40, w=100))
        self.assertGreaterEqual(edit_rows, 20)

    def test_default_rows_capped_on_huge_terminal(self):
        # Very tall terminal must not produce a gigantic overlay — the cap
        # keeps the inner edit area ≤ 30 rows regardless of screen height.
        edit_rows = self._run_capture_newwin(stdscr=_FakeStdscr(h=200, w=100))
        self.assertLessEqual(edit_rows, 30)

    def test_explicit_rows_override_respected(self):
        # Callers passing a concrete `rows=` still get that value (bounded
        # by terminal) — lets future callsites opt out of the adaptive
        # default if they need a specific shape.
        edit_rows = self._run_capture_newwin(
            stdscr=_FakeStdscr(h=40, w=100), rows=12)
        self.assertEqual(edit_rows, 12)

    def test_tiny_terminal_falls_back_to_prompt_text(self):
        # 9 rows is under the 10-row floor; widget must defer to single-line
        # input so operators on cramped terminals still get a prompt.
        called = {}

        def fake_prompt_text(stdscr, message):
            called["message"] = message
            return "hi"

        with patch.object(render, "_prompt_text", fake_prompt_text):
            out = render._prompt_multiline(_FakeStdscr(h=9, w=100), "label")
        self.assertEqual(out, "hi")
        self.assertIn("label", called["message"])
        self.assertIn("empty=cancel", called["message"])


if __name__ == "__main__":
    unittest.main()
