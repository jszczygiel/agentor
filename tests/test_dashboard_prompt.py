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
        self.addnstr_calls: list[tuple] = []
        self.move_calls: list[tuple[int, int]] = []

    def bkgd(self, *_a, **_kw): pass
    def box(self): pass
    def addnstr(self, *a, **_kw): self.addnstr_calls.append(a)
    def refresh(self): pass
    def keypad(self, flag): self.keypad_calls.append(flag)
    def move(self, y, x): self.move_calls.append((y, x))


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

    def test_initial_seed_paints_and_positions_cursor(self):
        """When `initial=` is provided, each seeded line is painted into the
        edit window via addnstr and the cursor is moved to the end of the
        last line. Omitting the kwarg leaves the window untouched — the
        backwards-compatibility guarantee existing call sites rely on."""
        painted_wins: list[_FakeWin] = []

        class _RecordingWin(_FakeWin):
            def __init__(self):
                super().__init__()
                painted_wins.append(self)

        def _newwin(*_a, **_kw):
            return _RecordingWin()

        class FakeTextbox:
            def __init__(self, win):
                self.win = win
                self.stripspaces = True

            def edit(self, _validator):
                pass

            def gather(self):
                return "Q1: Keep flag?\nA1: yes\n"

        self.ctx.enter_context(patch.object(curses, "newwin", _newwin))
        self.ctx.enter_context(patch.object(curses, "curs_set", lambda *_a: 0))
        self.ctx.enter_context(patch("curses.textpad.Textbox", FakeTextbox))
        stdscr = _FakeStdscr()
        seed = "Q1: Keep the legacy flag?\nA1: "
        out = render._prompt_multiline(stdscr, "label", initial=seed)
        # Widget creates frame then edit. Edit is the only window move()
        # is ever called on (cursor positioning); frame only gets addnstr.
        edit_candidates = [w for w in painted_wins if w.move_calls]
        self.assertTrue(edit_candidates, "expected cursor move on edit window")
        edit_win = edit_candidates[-1]
        # Each seeded line shows up as addnstr(y, x, line, maxlen).
        line_strings = [call[2] for call in edit_win.addnstr_calls]
        self.assertIn("Q1: Keep the legacy flag?", line_strings)
        self.assertIn("A1: ", line_strings)
        # Cursor lands at end of last seeded line (row 1, col = len("A1: ")).
        self.assertEqual(edit_win.move_calls[-1], (1, len("A1: ")))
        # Sanity: gather() drives the return path unchanged.
        self.assertEqual(out, "Q1: Keep flag?\nA1: yes")

    def test_no_initial_paints_nothing(self):
        """Default `initial=''` must not call addnstr on the edit window —
        existing callers depend on the blank-buffer contract."""
        painted_wins: list[_FakeWin] = []

        class _RecordingWin(_FakeWin):
            def __init__(self):
                super().__init__()
                painted_wins.append(self)

        def _newwin(*_a, **_kw):
            return _RecordingWin()

        class FakeTextbox:
            def __init__(self, win):
                self.stripspaces = True

            def edit(self, _validator):
                pass

            def gather(self):
                return ""

        self.ctx.enter_context(patch.object(curses, "newwin", _newwin))
        self.ctx.enter_context(patch.object(curses, "curs_set", lambda *_a: 0))
        self.ctx.enter_context(patch("curses.textpad.Textbox", FakeTextbox))
        render._prompt_multiline(_FakeStdscr(), "label")
        # Frame window paints via addnstr (header/footer); edit window
        # should not be painted at all.
        edit_wins = [w for w in painted_wins if not w.addnstr_calls]
        self.assertTrue(edit_wins, "expected an unpainted edit window")

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


class TestNewIssueNoteIsMultiline(unittest.TestCase):
    """Regression guard: the bug/idea note capture path must accept multi-line
    input, because operators use it for real expansion prompts that benefit
    from paragraph breaks. Earlier `_new_issue_mode` used the single-row
    `_prompt_text` prompt and was called out in IMPROVEMENTS.md."""

    def test_note_prompt_uses_multiline_and_preserves_newlines(self):
        """Whatever `_prompt_multiline` returns (including embedded newlines)
        must be forwarded verbatim to `_expand_note`."""
        typed = "first line\n\nsecond paragraph\nthird line"
        seen_note: dict[str, object] = {}

        cfg = SimpleNamespace(
            sources=SimpleNamespace(watch=["docs/backlog/foo.md"]),
            parsing=SimpleNamespace(mode="checkbox"),
            project_root=Path("/tmp/_agentor_test_root"),
            agent=SimpleNamespace(runner="claude"),
        )
        daemon = SimpleNamespace(provider_override=None)

        def fake_target(_cfg):
            return Path("/tmp/_agentor_test_root/docs/backlog/foo.md"), "file"

        def fake_multiline(_stdscr, _label, **_kw):
            return typed

        def fake_run_with_progress(_stdscr, _title, work, **_kw):
            return work(lambda _msg: None)

        def fake_expand(note, provider, _kind, **_kw):
            seen_note["note"] = note
            seen_note["provider"] = provider
            return "- [ ] expanded\n  body"

        def fake_append(_path, _block):
            return None

        def fake_scan_once(_cfg, _store):
            return SimpleNamespace(new_items=0)

        fake_provider = object()
        with patch.object(modes, "_new_issue_target", fake_target), \
             patch.object(modes, "_prompt_multiline", fake_multiline), \
             patch.object(modes, "_run_with_progress", fake_run_with_progress), \
             patch.object(modes, "_expand_note", fake_expand), \
             patch.object(modes, "make_provider",
                          lambda _cfg: fake_provider), \
             patch.object(modes, "_append_checkbox_block", fake_append), \
             patch.object(modes, "scan_once", fake_scan_once), \
             patch.object(modes, "_flash", lambda *_a, **_kw: None):
            modes._new_issue_mode(_FakeStdscr(), cfg, store=None, daemon=daemon)

        self.assertEqual(seen_note.get("note"), typed)
        self.assertIs(seen_note.get("provider"), fake_provider)

    def test_note_prompt_empty_cancel_skips_expand(self):
        """Empty return from the overlay must short-circuit before any
        provider call — same contract as the prior `_prompt_text` path."""
        called = {"expand": False}

        cfg = SimpleNamespace(
            sources=SimpleNamespace(watch=["docs/backlog/foo.md"]),
            parsing=SimpleNamespace(mode="checkbox"),
            project_root=Path("/tmp/_agentor_test_root"),
            agent=SimpleNamespace(runner="claude"),
        )
        daemon = SimpleNamespace(provider_override=None)

        def fake_target(_cfg):
            return Path("/tmp/_agentor_test_root/docs/backlog/foo.md"), "file"

        def fake_expand(*_a, **_kw):
            called["expand"] = True
            return ""

        with patch.object(modes, "_new_issue_target", fake_target), \
             patch.object(modes, "_prompt_multiline", lambda *_a, **_kw: ""), \
             patch.object(modes, "_expand_note", fake_expand), \
             patch.object(modes, "_flash", lambda *_a, **_kw: None):
            modes._new_issue_mode(_FakeStdscr(), cfg, store=None, daemon=daemon)

        self.assertFalse(called["expand"])

    def test_new_issue_mode_routes_through_configured_provider(self):
        """`agent.runner = "codex"` → `_new_issue_mode` must build a
        Codex provider, not a Claude one. Prior behaviour shelled to
        `claude` verbatim regardless of the configured runner."""
        from agentor.providers import CodexProvider

        cfg = SimpleNamespace(
            sources=SimpleNamespace(watch=["docs/backlog/foo.md"]),
            parsing=SimpleNamespace(mode="checkbox"),
            project_root=Path("/tmp/_agentor_test_root"),
            agent=SimpleNamespace(runner="codex"),
        )
        daemon = SimpleNamespace(provider_override=None)
        got_providers: list[object] = []

        def fake_target(_cfg):
            return Path("/tmp/_agentor_test_root/docs/backlog/foo.md"), "file"

        def fake_expand(_note, provider, _kind, **_kw):
            got_providers.append(provider)
            return "- [ ] ok\n  body"

        def fake_run_with_progress(_stdscr, _title, work, **_kw):
            return work(lambda _msg: None)

        with patch.object(modes, "_new_issue_target", fake_target), \
             patch.object(modes, "_prompt_multiline",
                          lambda *_a, **_kw: "note"), \
             patch.object(modes, "_run_with_progress", fake_run_with_progress), \
             patch.object(modes, "_expand_note", fake_expand), \
             patch.object(modes, "_append_checkbox_block",
                          lambda *_a, **_kw: None), \
             patch.object(modes, "scan_once",
                          lambda *_a, **_kw: SimpleNamespace(new_items=0)), \
             patch.object(modes, "_flash", lambda *_a, **_kw: None):
            modes._new_issue_mode(_FakeStdscr(), cfg, store=None, daemon=daemon)

        self.assertEqual(len(got_providers), 1)
        self.assertIsInstance(got_providers[0], CodexProvider)

    def test_new_issue_mode_honours_provider_override(self):
        """`daemon.provider_override` must shadow `agent.runner` when
        picking the one-shot provider — mirrors `daemon._make_runner`."""
        from agentor.config import (AgentConfig, Config, GitConfig,
                                    ParsingConfig, ReviewConfig,
                                    SourcesConfig)
        from agentor.providers import CodexProvider

        cfg = Config(
            project_name="p",
            project_root=Path("/tmp/_agentor_test_root"),
            sources=SourcesConfig(watch=["docs/backlog/foo.md"], exclude=[]),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(runner="claude"),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )
        daemon = SimpleNamespace(provider_override="codex")
        got_providers: list[object] = []

        def fake_expand(_note, provider, _kind, **_kw):
            got_providers.append(provider)
            return "- [ ] ok\n  body"

        def fake_run_with_progress(_stdscr, _title, work, **_kw):
            return work(lambda _msg: None)

        with patch.object(
                modes, "_new_issue_target",
                lambda _cfg: (
                    Path("/tmp/_agentor_test_root/docs/backlog/foo.md"),
                    "file",
                ),
             ), \
             patch.object(modes, "_prompt_multiline",
                          lambda *_a, **_kw: "note"), \
             patch.object(modes, "_run_with_progress", fake_run_with_progress), \
             patch.object(modes, "_expand_note", fake_expand), \
             patch.object(modes, "_append_checkbox_block",
                          lambda *_a, **_kw: None), \
             patch.object(modes, "scan_once",
                          lambda *_a, **_kw: SimpleNamespace(new_items=0)), \
             patch.object(modes, "_flash", lambda *_a, **_kw: None):
            modes._new_issue_mode(_FakeStdscr(), cfg, store=None, daemon=daemon)

        self.assertIsInstance(got_providers[0], CodexProvider)


class _ResizingStdscr(_FakeStdscr):
    """Stdscr stub whose `getmaxyx()` shrinks once `_handle_resize` has fired,
    so `_prompt_multiline._rebuild` re-computes geometry against new dims.
    `clear`/`refresh` are no-ops the validator may call mid-edit."""

    def __init__(self, h: int = 30, w: int = 100,
                 resized: tuple[int, int] | None = None):
        super().__init__(h=h, w=w)
        self._resized = resized
        self.clear_calls = 0

    def getmaxyx(self):
        if self._post_resize:
            return self._resized or (self._h, self._w)
        return (self._h, self._w)

    def clear(self):
        self.clear_calls += 1
        if self._resized is not None:
            self._post_resize = True

    @property
    def _post_resize(self) -> bool:
        return getattr(self, "_flag", False)

    @_post_resize.setter
    def _post_resize(self, v: bool) -> None:
        self._flag = v


class _RecordingWin(_FakeWin):
    """FakeWin that records `addnstr` paints so resize tests can assert the
    saved text was restored into the rebuilt edit window."""

    def __init__(self):
        super().__init__()
        self.addnstr_calls: list[tuple[int, int, str, int]] = []

    def addnstr(self, y, x, s, n, attr=0):
        self.addnstr_calls.append((y, x, s, n))


class TestPromptMultilineResize(unittest.TestCase):
    """KEY_RESIZE inside the Textbox validator must rebuild the overlay at
    the new dims rather than leaking the keycode into the gathered buffer."""

    def setUp(self):
        self.ctx = _Ctx()

    def tearDown(self):
        self.ctx.close()

    def test_key_resize_swallowed_by_validator(self):
        """Validator returns 0 for KEY_RESIZE so `Textbox.edit` keeps looping;
        the literal `chr(410)` must never reach `gather()`."""
        returns: list[int] = []

        class FakeTextbox:
            def __init__(self, win):
                self.win = win
                self.stripspaces = True

            def edit(self, validator):
                returns.append(validator(curses.KEY_RESIZE))
                returns.append(validator(ord("a")))

            def gather(self):
                # Real Textbox.gather reads cells via inch — we mimic the
                # contract that only on-screen text is returned, with the
                # KEY_RESIZE never landing in the buffer.
                return "a"

        _install_fakes(self.ctx, FakeTextbox)
        with patch.object(curses, "update_lines_cols"):
            out = render._prompt_multiline(_ResizingStdscr(), "label")
        self.assertEqual(returns[0], 0)
        self.assertEqual(returns[1], ord("a"))
        self.assertNotIn(chr(curses.KEY_RESIZE), out)
        self.assertEqual(out, "a")

    def test_key_resize_rebuilds_overlay(self):
        """One synthetic KEY_RESIZE must cause two extra `curses.newwin`
        calls (frame + edit) and swap `box.win` to the new edit window."""
        newwin_calls: list[tuple] = []
        seen: dict[str, object] = {}

        class FakeTextbox:
            def __init__(self, win):
                self.win = win
                self.stripspaces = True
                seen["initial_win"] = win

            def edit(self, validator):
                validator(curses.KEY_RESIZE)
                seen["post_resize_win"] = self.win

            def gather(self):
                return ""

        _install_fakes(self.ctx, FakeTextbox, newwin_calls=newwin_calls)
        with patch.object(curses, "update_lines_cols"):
            render._prompt_multiline(
                _ResizingStdscr(h=30, w=100, resized=(28, 90)), "label")
        # 2 initial (frame, edit) + 2 rebuild (frame, edit) = 4 newwin calls.
        self.assertEqual(len(newwin_calls), 4)
        # Edit window swapped — pre-resize identity must differ from post.
        self.assertIsNot(seen["initial_win"], seen["post_resize_win"])

    def test_key_resize_restores_text(self):
        """Pre-existing text in the editor must be re-painted into the
        rebuilt edit window so the operator doesn't lose what they typed."""
        newwin_calls: list[tuple] = []
        edit_wins: list[_RecordingWin] = []

        def _newwin(*a, **_kw):
            newwin_calls.append(a)
            win = _RecordingWin()
            edit_wins.append(win)
            return win

        class FakeTextbox:
            def __init__(self, win):
                self.win = win
                self.stripspaces = True

            def edit(self, validator):
                validator(curses.KEY_RESIZE)

            def gather(self):
                # Pre-seed: simulates the operator having typed two lines
                # before the resize fires.
                return "draft line one\nsecond line\n"

        with patch.object(curses, "newwin", _newwin), \
             patch.object(curses, "curs_set", lambda *_a: 0), \
             patch("curses.textpad.Textbox", FakeTextbox), \
             patch.object(curses, "update_lines_cols"):
            render._prompt_multiline(
                _ResizingStdscr(h=30, w=100, resized=(28, 90)), "label")
        # newwin order: frame, edit, new_frame, new_edit. The rebuilt edit
        # window is the 4th; assert it received both lines via addnstr.
        self.assertEqual(len(edit_wins), 4)
        rebuilt_edit = edit_wins[3]
        painted = [s for (_y, _x, s, _n) in rebuilt_edit.addnstr_calls]
        self.assertIn("draft line one", painted)
        self.assertIn("second line", painted)

    def test_key_resize_to_tiny_terminal_cancels(self):
        """If the resized terminal drops below the 10×40 floor the rebuild
        bails out and the edit cancels rather than crashing."""
        class FakeTextbox:
            def __init__(self, win):
                self.win = win
                self.stripspaces = True

            def edit(self, validator):
                # Cancel signal returned (7 == Ctrl-G); real Textbox would
                # then exit edit().
                self._last = validator(curses.KEY_RESIZE)

            def gather(self):
                return "should be discarded"

        _install_fakes(self.ctx, FakeTextbox)
        with patch.object(curses, "update_lines_cols"):
            out = render._prompt_multiline(
                _ResizingStdscr(h=30, w=100, resized=(5, 20)), "label")
        self.assertEqual(out, "")  # cancelled


if __name__ == "__main__":
    unittest.main()
