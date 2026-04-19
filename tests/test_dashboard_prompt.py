"""Tests for `_prompt_multiline` in agentor.dashboard.render.

The widget is built on curses primitives, so the tests mock `curses.newwin`
and `curses.textpad.Textbox` to drive the editor without a real tty. The
goal is to verify the contract the callers depend on: Ctrl-G submit returns
typed text, Ctrl-C/Esc and empty submit both return the empty string, and
the widget downgrades to `_prompt_text` on a very small terminal."""

import curses
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentor.dashboard import modes, render


class _FakeStdscr:
    """Minimal stdscr stub — only what `_prompt_multiline` touches."""

    def __init__(self, h: int = 30, w: int = 100):
        self._h = h
        self._w = w
        self.nodelay_calls: list[bool] = []
        self.refresh_calls = 0
        self.touchwin_calls = 0
        # Paint log for the backdrop-ordering test. Each entry is the
        # positional args tuple passed to addnstr().
        self.addnstr_calls: list[tuple] = []
        # Ordered event log so tests can assert backdrop paints happen
        # before popup windows are created. Shared with the newwin patch
        # via _install_fakes.
        self.event_log: list[str] = []

    def getmaxyx(self):
        return (self._h, self._w)

    def nodelay(self, flag):
        self.nodelay_calls.append(flag)

    def touchwin(self):
        self.touchwin_calls += 1

    def refresh(self):
        self.refresh_calls += 1

    def addnstr(self, *args, **_kw):
        self.addnstr_calls.append(args)
        self.event_log.append("addnstr")


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


def _install_fakes(monkey, textbox_factory, newwin_calls=None,
                   event_log=None):
    """Patch the curses entry points `_prompt_multiline` calls. Returns the
    unittest.mock.patch context managers started for cleanup by the caller.

    When `newwin_calls` is a list, each `curses.newwin(...)` call is appended
    so tests can inspect the sizes the overlay requested. When `event_log`
    is a list, each newwin call appends `"newwin"` so tests can assert
    relative ordering of stdscr paints vs. popup window creation."""
    def _newwin(*a, **_kw):
        if newwin_calls is not None:
            newwin_calls.append(a)
        if event_log is not None:
            event_log.append("newwin")
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

    def test_backdrop_painted_before_popup_on_wide_terminal(self):
        # On wide terminals (~160+ cols) the 80-col popup doesn't cover the
        # screen; the overlay must blank stdscr before drawing its frame so
        # pre-existing table/panel cells don't bleed through the margins.
        class FakeTextbox:
            def __init__(self, win):
                self.stripspaces = True
            def edit(self, validator): pass
            def gather(self): return ""

        stdscr = _FakeStdscr(h=40, w=200)
        _install_fakes(self.ctx, FakeTextbox,
                       newwin_calls=None, event_log=stdscr.event_log)
        render._prompt_multiline(stdscr, "label")

        # Backdrop covers every row of stdscr.
        self.assertEqual(len(stdscr.addnstr_calls), 40)
        # Every paint targets column 0 with width w-1 and carries A_DIM so
        # the modal signals the dashboard is inert.
        for y, (ay, ax, text, n, attr) in enumerate(stdscr.addnstr_calls):
            self.assertEqual(ay, y)
            self.assertEqual(ax, 0)
            self.assertEqual(n, 199)
            self.assertEqual(text, " " * 199)
            self.assertTrue(attr & curses.A_DIM)
        # All backdrop paints precede the first popup window creation.
        first_newwin = stdscr.event_log.index("newwin")
        self.assertTrue(all(e == "addnstr"
                            for e in stdscr.event_log[:first_newwin]))
        self.assertEqual(stdscr.event_log.count("addnstr"), 40)

    def test_backdrop_swallows_curses_error(self):
        # If a row-paint raises curses.error (bottom-right cell quirks on
        # some curses builds) the widget must keep going and still open the
        # popup — the leak fix is best-effort, not a new failure mode.
        class FakeTextbox:
            def __init__(self, win):
                self.stripspaces = True
            def edit(self, validator): pass
            def gather(self): return ""

        class _RaisingStdscr(_FakeStdscr):
            def addnstr(self, *args, **kw):
                super().addnstr(*args, **kw)
                raise curses.error("boom")

        stdscr = _RaisingStdscr(h=20, w=120)
        newwin_calls: list[tuple] = []
        _install_fakes(self.ctx, FakeTextbox, newwin_calls=newwin_calls)
        # Must not raise despite every addnstr failing.
        render._prompt_multiline(stdscr, "label")
        # Frame + edit_win still created.
        self.assertEqual(len(newwin_calls), 2)


class TestNewIssueNoteIsMultiline(unittest.TestCase):
    """Regression guard: the bug/idea note capture path must accept multi-line
    input, because operators use it for real expansion prompts that benefit
    from paragraph breaks. Earlier `_new_issue_mode` used the single-row
    `_prompt_text` prompt and was called out in IMPROVEMENTS.md."""

    def test_note_prompt_uses_multiline_and_preserves_newlines(self):
        """Whatever `_prompt_multiline` returns (including embedded newlines)
        must be forwarded verbatim to `_expand_note_via_claude`."""
        typed = "first line\n\nsecond paragraph\nthird line"
        seen_note: dict[str, str] = {}

        cfg = SimpleNamespace(
            sources=SimpleNamespace(watch=["docs/backlog/foo.md"]),
            parsing=SimpleNamespace(mode="checkbox"),
            project_root=Path("/tmp/_agentor_test_root"),
        )

        def fake_target(_cfg):
            return Path("/tmp/_agentor_test_root/docs/backlog/foo.md"), "file"

        def fake_multiline(_stdscr, _label, **_kw):
            return typed

        def fake_run_with_progress(_stdscr, _title, work, **_kw):
            return work(lambda _msg: None)

        def fake_expand(note, _cfg, _kind, timeout):  # noqa: ARG001
            seen_note["note"] = note
            return "- [ ] expanded\n  body"

        def fake_append(_path, _block):
            return None

        def fake_scan_once(_cfg, _store):
            return SimpleNamespace(new_items=0)

        with patch.object(modes, "_new_issue_target", fake_target), \
             patch.object(modes, "_prompt_multiline", fake_multiline), \
             patch.object(modes, "_run_with_progress", fake_run_with_progress), \
             patch.object(modes, "_expand_note_via_claude", fake_expand), \
             patch.object(modes, "_append_checkbox_block", fake_append), \
             patch.object(modes, "scan_once", fake_scan_once), \
             patch.object(modes, "_flash", lambda *_a, **_kw: None):
            modes._new_issue_mode(_FakeStdscr(), cfg, store=None, daemon=None)

        self.assertEqual(seen_note.get("note"), typed)

    def test_note_prompt_empty_cancel_skips_claude(self):
        """Empty return from the overlay must short-circuit before any
        Claude call — same contract as the prior `_prompt_text` path."""
        called = {"expand": False}

        cfg = SimpleNamespace(
            sources=SimpleNamespace(watch=["docs/backlog/foo.md"]),
            parsing=SimpleNamespace(mode="checkbox"),
            project_root=Path("/tmp/_agentor_test_root"),
        )

        def fake_target(_cfg):
            return Path("/tmp/_agentor_test_root/docs/backlog/foo.md"), "file"

        def fake_expand(*_a, **_kw):
            called["expand"] = True
            return ""

        with patch.object(modes, "_new_issue_target", fake_target), \
             patch.object(modes, "_prompt_multiline", lambda *_a, **_kw: ""), \
             patch.object(modes, "_expand_note_via_claude", fake_expand), \
             patch.object(modes, "_flash", lambda *_a, **_kw: None):
            modes._new_issue_mode(_FakeStdscr(), cfg, store=None, daemon=None)

        self.assertFalse(called["expand"])


if __name__ == "__main__":
    unittest.main()
