import curses
import json
import time
import unittest
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from agentor.dashboard.formatters import _token_windows_invalidate
from agentor.dashboard.render import (
    ACTIONS,
    ACTIONS_MID,
    ACTIONS_NARROW,
    ACTIONS_WIDE,
    FILTERS,
    _build_alert_banner,
    _build_status_line,
    _layout_tier,
    _render,
    _render_table,
    _state_glyph,
    _table_header,
    _table_row,
)
from agentor.models import Item, ItemStatus
from agentor.store import Store, StoredItem


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
        self.assertNotIn("[p]ickup", ACTIONS)
        self.assertNotIn("[m]ode", ACTIONS)

    def test_double_space_separators(self):
        self.assertNotIn("] [", ACTIONS)


class TestLayoutTier(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(_layout_tier(40), "narrow")
        self.assertEqual(_layout_tier(59), "narrow")
        self.assertEqual(_layout_tier(60), "mid")
        self.assertEqual(_layout_tier(79), "mid")
        self.assertEqual(_layout_tier(80), "wide")
        self.assertEqual(_layout_tier(120), "wide")

    def test_actions_mid_fits_60(self):
        # Mid-tier hint must fit the bottom of the mid range (60 cols).
        # The render prepends one leading space before centering.
        self.assertLessEqual(len(ACTIONS_MID) + 1, 60)

    def test_actions_narrow_fits_40(self):
        self.assertLessEqual(len(ACTIONS_NARROW) + 1, 40)

    def test_actions_alias(self):
        self.assertIs(ACTIONS, ACTIONS_WIDE)

    def test_actions_narrow_points_to_help(self):
        self.assertIn("?", ACTIONS_NARROW)


class TestStateGlyph(unittest.TestCase):
    def test_all_statuses_mapped(self):
        for st in ItemStatus:
            g = _state_glyph(st)
            self.assertEqual(len(g), 1, f"{st} glyph must be 1 char, got {g!r}")


class TestAlertBanner(unittest.TestCase):
    def test_wide_embeds_message(self):
        b = _build_alert_banner("git push failed", 80)
        self.assertIn("git push failed", b)
        self.assertIn("[u]", b)

    def test_long_message_truncated(self):
        b = _build_alert_banner("x" * 200, 40)
        self.assertLessEqual(len(b), 40)

    def test_very_narrow_drops_message(self):
        # At w<33 the wrapper text alone eats the budget; the action
        # prompt must still survive without raising.
        b = _build_alert_banner("some-long-error-message-text", 20)
        self.assertIn("[u]", b)
        self.assertLessEqual(len(b), 20)

    def test_empty_alert_safe(self):
        b = _build_alert_banner("", 60)
        self.assertIn("[u]", b)


class TestStatusLineTier(unittest.TestCase):
    def _stats(self):
        class S:
            completed = 12
        return S()

    def _cfg(self):
        class A:
            runner = "claude"
            pool_size = 3
        class C:
            agent = A()
        return C()

    def _counts(self):
        return {st: 0 for st in ItemStatus} | {
            ItemStatus.QUEUED: 2,
            ItemStatus.WORKING: 1,
            ItemStatus.AWAITING_REVIEW: 4,
            ItemStatus.ERRORED: 1,
        }

    def test_wide_has_full_fields(self):
        line = _build_status_line("wide", self._cfg(), self._stats(),
                                  self._counts(), 1)
        self.assertIn("pool=3", line)
        self.assertIn("queued=2", line)
        self.assertIn("rejected=0", line)

    def test_mid_fits_60(self):
        line = _build_status_line("mid", self._cfg(), self._stats(),
                                  self._counts(), 1)
        self.assertLessEqual(len(line), 60)
        self.assertIn("p=3", line)
        self.assertIn("R=4", line)

    def test_narrow_fits_40(self):
        line = _build_status_line("narrow", self._cfg(), self._stats(),
                                  self._counts(), 1)
        self.assertLessEqual(len(line), 40)
        self.assertIn("p=3", line)
        self.assertIn("R=4", line)

    def test_wide_appends_compact_indicator_when_provided(self):
        line = _build_status_line(
            "wide", self._cfg(), self._stats(), self._counts(), 1,
            token_compact="tok sess=1.2k  wk=3.4M",
        )
        self.assertIn("tok sess=1.2k  wk=3.4M", line)

    def test_wide_without_compact_unchanged(self):
        # Default empty token_compact → no trailing indicator substring.
        line = _build_status_line("wide", self._cfg(), self._stats(),
                                  self._counts(), 1)
        self.assertNotIn("tok sess=", line)
        self.assertNotIn("wk=", line)

    def test_mid_ignores_compact_indicator(self):
        # Passing token_compact at mid must not push line past the 60-col
        # budget — mid drops the indicator entirely.
        line = _build_status_line(
            "mid", self._cfg(), self._stats(), self._counts(), 1,
            token_compact="tok sess=1.2k  wk=3.4M",
        )
        self.assertLessEqual(len(line), 60)
        self.assertNotIn("tok sess=", line)

    def test_narrow_ignores_compact_indicator(self):
        line = _build_status_line(
            "narrow", self._cfg(), self._stats(), self._counts(), 1,
            token_compact="tok sess=1.2k  wk=3.4M",
        )
        self.assertLessEqual(len(line), 40)
        self.assertNotIn("tok sess=", line)


def _make_item(title="hello", src="docs/backlog/foo.md") -> StoredItem:
    return StoredItem(
        id="abc12345", title=title, body="", source_file=src,
        source_line=1, tags={}, status=ItemStatus.WORKING,
        worktree_path=None, branch=None, attempts=0, last_error=None,
        feedback=None, result_json=None, session_id=None,
        agentor_version=None, priority=0, created_at=0.0, updated_at=0.0,
    )


class TestTableRowFits(unittest.TestCase):
    def test_row_fits_at_each_tier(self):
        item = _make_item(title="a fairly long backlog item title that should trim")
        for w in (40, 60, 80, 120):
            tier = _layout_tier(w)
            row = _table_row(tier, item, ItemStatus.WORKING,
                             "01:23", "45%", False, w)
            self.assertLessEqual(
                len(row), w,
                f"tier={tier} w={w} row={row!r} len={len(row)}"
            )

    def test_header_fits_at_each_tier(self):
        for w in (40, 60, 80, 120):
            tier = _layout_tier(w)
            header = _table_header(tier)
            self.assertLessEqual(
                len(header), w,
                f"tier={tier} w={w} header={header!r} len={len(header)}"
            )

    def test_narrow_drops_source(self):
        header = _table_header("narrow")
        self.assertNotIn("SOURCE", header)
        self.assertNotIn("STATE", header)  # narrow uses a 1-char glyph

    def test_mid_drops_source_keeps_state(self):
        header = _table_header("mid")
        self.assertNotIn("SOURCE", header)
        self.assertIn("STATE", header)

    def test_wide_drops_source_keeps_state(self):
        header = _table_header("wide")
        self.assertNotIn("SOURCE", header)
        self.assertIn("STATE", header)

    def test_wide_row_omits_source_basename(self):
        item = _make_item(title="hello", src="docs/backlog/unique-source.md")
        row = _table_row("wide", item, ItemStatus.WORKING,
                         "01:23", "45%", False, 120)
        self.assertNotIn("unique-source", row)
        self.assertNotIn("docs/backlog", row)
        self.assertIn("hello", row)

    def test_error_marker_in_narrow(self):
        item = _make_item()
        row = _table_row("narrow", item, ItemStatus.ERRORED,
                         "—", "—", True, 40)
        # Marker `!` should survive the narrow layout.
        self.assertTrue(row.strip().split()[1].startswith("!"),
                        f"marker missing in {row!r}")


class _StubScreen:
    """Minimal curses-like stdscr for render tests. Captures every line
    written via addnstr/addstr along with the width clip, so the test can
    assert nothing exceeds the reported terminal width."""

    def __init__(self, h: int, w: int) -> None:
        self.h = h
        self.w = w
        self.lines: list[str] = []

    def getmaxyx(self):
        return (self.h, self.w)

    def erase(self):
        self.lines.clear()

    def refresh(self):
        pass

    def addnstr(self, y, x, s, n, attr=0):
        # Mimic curses: clip at n characters. x is an offset on the row.
        clipped = s[: max(0, n - x)]
        self.lines.append(clipped)

    # ignore unused methods
    def nodelay(self, *a, **k): pass
    def timeout(self, *a, **k): pass
    def getch(self): return -1


class _FakeStats:
    completed = 0


class _FakeAgent:
    runner = "claude"
    pool_size = 1
    context_window = 200_000


class _FakeCfg:
    agent = _FakeAgent()
    project_name = "test"


class _FakeDaemon:
    def __init__(self, alert: str | None = None) -> None:
        self.system_alert = alert
        self.workers: list = []
        self.stats = _FakeStats()
        # Main added the token-usage panel; the renderer reads
        # `daemon.started_at` to bound the "session" window. 0.0 is the
        # sentinel the real daemon uses pre-main-loop.
        self.started_at = 0.0


class _FakeStore:
    def count_by_status(self, st):
        return 0

    def list_by_status(self, st):
        return []

    def latest_transition_at(self, *a, **k):
        return None

    def aggregate_token_usage(self, *, since=None):
        # Returned shape matches Store.aggregate_token_usage; zeros keep
        # the token panel renderable without a real SQLite backend.
        return {"input": 0, "output": 0, "cache_read": 0,
                "cache_create": 0, "total": 0}


class TestRenderFitsWidth(unittest.TestCase):
    def _render_all(self, w: int, alert: str | None = None):
        scr = _StubScreen(24, w)
        # `_render` reaches for `curses.color_pair` to colour the alert
        # banner; without `initscr()` that raises. Stub it so we can
        # exercise the render path headless.
        with patch.object(curses, "color_pair", return_value=0):
            _render(scr, _FakeCfg(), _FakeStore(),
                    _FakeDaemon(alert=alert), deque(), 0)
        return scr.lines

    def test_no_line_exceeds_width(self):
        for w in (40, 60, 80, 120):
            lines = self._render_all(w)
            for ln in lines:
                self.assertLessEqual(
                    len(ln), w,
                    f"w={w}: line exceeds width: {ln!r}"
                )

    def test_alert_banner_narrow_no_crash(self):
        # Regression: the old `w - 30` truncation would produce a
        # negative slice at w<33 and garble the banner.
        lines = self._render_all(20, alert="git merge conflict: foo.py")
        for ln in lines:
            self.assertLessEqual(len(ln), 20, f"banner overflow: {ln!r}")

    def test_stale_session_banner_rendered(self):
        scr = _StubScreen(24, 100)
        daemon = _FakeDaemon()
        daemon.stale_session_alerts = {"abcd1234": time.time_ns() - 7 * 60_000_000_000}
        with patch.object(curses, "color_pair", return_value=0):
            _render(scr, _FakeCfg(), _FakeStore(), daemon, deque(), 0)
        joined = "\n".join(scr.lines)
        self.assertIn("stale session abcd1234", joined)
        self.assertIn("7m idle", joined)

    def test_stale_session_banner_caps_at_three(self):
        scr = _StubScreen(24, 100)
        daemon = _FakeDaemon()
        now_ns = time.time_ns()
        daemon.stale_session_alerts = {
            f"id{i:06d}": now_ns - (10 + i) * 60_000_000_000
            for i in range(5)
        }
        with patch.object(curses, "color_pair", return_value=0):
            _render(scr, _FakeCfg(), _FakeStore(), daemon, deque(), 0)
        joined = "\n".join(scr.lines)
        # Three banners plus one roll-up summary = 4 lines mentioning "stale"
        # or "+N more".
        self.assertIn("+2 more stale session", joined)


class TestRenderTokenRow(unittest.TestCase):
    """The token panel was collapsed to a single dim row emitted directly
    under the status line. Verify the renderer produces exactly one row
    carrying both windows (5h / week, mirroring claude.ai/settings/usage)
    at each width tier, and that no residual `_render_token_panel` rows
    remain."""

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
        _token_windows_invalidate()

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _render_at(self, width: int) -> list[tuple[int, str]]:
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

    def test_wide_emits_single_token_row(self):
        lines = self._render_at(120)
        token_lines = [s for _, s in lines if "usage" in s and "wk" in s]
        self.assertEqual(len(token_lines), 1)
        row = token_lines[0]
        self.assertIn("5h", row)
        self.assertIn("wk", row)
        # Both rolling windows include the only seeded item → totals 79.3k.
        self.assertIn("79.3k", row)

    def test_mid_emits_single_token_row(self):
        lines = self._render_at(70)
        token_lines = [s for _, s in lines if "usage" in s and "wk" in s]
        self.assertEqual(len(token_lines), 1)

    def test_narrow_uses_short_token_labels(self):
        lines = self._render_at(50)
        # Narrow drops `usage` prefix for `tok` and uses 5h=/wk= shorts.
        token_lines = [s for _, s in lines if "tok " in s and "wk=" in s]
        self.assertEqual(len(token_lines), 1)
        self.assertIn("5h=", token_lines[0])

    def test_no_residual_panel_header(self):
        # The one-liner combines the `usage` label with `5h`/`wk` cells on
        # the same row, so a lone ` usage` header must not appear.
        lines = self._render_at(120)
        stripped = [s.strip() for _, s in lines]
        self.assertNotIn("usage", stripped)


class TestRenderStatusLineTokenIndicator(unittest.TestCase):
    """The compact `tok 5h=… wk=…` indicator is appended to the main status
    line so rolling 5-hour and weekly spend is readable at a glance without
    scanning the full token panel — same windows surfaced by the headline
    cells on claude.ai/settings/usage."""

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
        # Both rolling windows include the only seeded item → totals match.
        self.assertIn("tok 5h=79.3k", joined)
        self.assertIn("wk=79.3k", joined)

    def test_indicator_lives_on_status_line_not_panel(self):
        # The panel row starts with " usage" (leading space from the
        # renderer). The compact indicator must be on the *preceding*
        # status line — i.e. the line with `pool=`.
        lines = self._render_once()
        status_lines = [s for _, s in lines if "pool=" in s]
        self.assertEqual(len(status_lines), 1)
        self.assertIn("tok 5h=", status_lines[0])
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


class TestDefaultFilter(unittest.TestCase):
    """Default dashboard filter (index 0 in FILTERS) must show active work
    only — QUEUED/WORKING/AWAITING_*/CONFLICTED/APPROVED — and hide
    terminal states and deferred. An explicit `all` entry still exists for
    operators who want the full set."""

    _ACTIVE_STATUSES = {
        ItemStatus.QUEUED,
        ItemStatus.WORKING,
        ItemStatus.AWAITING_PLAN_REVIEW,
        ItemStatus.AWAITING_REVIEW,
        ItemStatus.CONFLICTED,
        ItemStatus.APPROVED,
    }

    def test_default_filter_is_active(self):
        name, statuses = FILTERS[0]
        self.assertEqual(name, "active")
        self.assertIsNotNone(statuses)
        self.assertEqual(set(statuses), self._ACTIVE_STATUSES)

    def test_all_filter_covers_every_status(self):
        entry = next((e for e in FILTERS if e[0] == "all"), None)
        self.assertIsNotNone(entry, "FILTERS must expose an 'all' entry")
        _, statuses = entry
        # None resolves to `list(ItemStatus)` in _render; both representations
        # are valid "every status" sentinels.
        resolved = set(statuses) if statuses is not None else set(ItemStatus)
        self.assertEqual(resolved, set(ItemStatus))

    def test_terminal_statuses_hidden_by_default(self):
        td = TemporaryDirectory()
        try:
            store = Store(Path(td.name) / "state.db")
            hidden = {
                "m_id": ItemStatus.MERGED,
                "r_id": ItemStatus.REJECTED,
                "e_id": ItemStatus.ERRORED,
                "c_id": ItemStatus.CANCELLED,
                "d_id": ItemStatus.DEFERRED,
            }
            for item_id, st in hidden.items():
                store.upsert_discovered(Item(
                    id=item_id, title=item_id, body="",
                    source_file="s.md", source_line=1, tags={},
                ))
                store.transition(item_id, st)
            store.upsert_discovered(Item(
                id="q_id", title="q_id", body="",
                source_file="s.md", source_line=2, tags={},
            ))

            stdscr = _FakeStdscr(width=200)
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
            _token_windows_invalidate()
            with patch("agentor.dashboard.render.curses.color_pair",
                       return_value=0), \
                 patch("agentor.dashboard.render._set_terminal_title"):
                rendered = _render(stdscr, cfg, store, daemon, log_ring=[],
                                   filter_idx=0, selected_id=None)
            rendered_ids = {it.id for it in rendered}
            self.assertEqual(rendered_ids, {"q_id"})
            for hidden_id in hidden:
                self.assertNotIn(hidden_id, rendered_ids)
            store.close()
        finally:
            td.cleanup()

    def test_all_filter_reveals_hidden_statuses(self):
        td = TemporaryDirectory()
        try:
            store = Store(Path(td.name) / "state.db")
            store.upsert_discovered(Item(
                id="merged_id", title="merged_id", body="",
                source_file="s.md", source_line=1, tags={},
            ))
            store.transition("merged_id", ItemStatus.MERGED)

            all_idx = next(i for i, e in enumerate(FILTERS) if e[0] == "all")
            stdscr = _FakeStdscr(width=200)
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
            _token_windows_invalidate()
            with patch("agentor.dashboard.render.curses.color_pair",
                       return_value=0), \
                 patch("agentor.dashboard.render._set_terminal_title"):
                rendered = _render(stdscr, cfg, store, daemon, log_ring=[],
                                   filter_idx=all_idx, selected_id=None)
            self.assertIn("merged_id", {it.id for it in rendered})
            store.close()
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
