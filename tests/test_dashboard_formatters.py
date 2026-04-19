import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from unittest import mock

from agentor.dashboard import formatters
from agentor.dashboard.formatters import (
    _ctx_fill_pct,
    _fmt_elapsed,
    _fmt_token_compact,
    _fmt_token_row,
    _fmt_tokens,
    _token_breakdown,
    _token_windows,
    _token_windows_invalidate,
)
from agentor.models import Item, ItemStatus
from agentor.store import Store, StoredItem


def _item(result_json: str | None) -> StoredItem:
    """Build a minimal StoredItem with only the fields the formatters read."""
    return StoredItem(
        id="abc12345",
        title="t",
        body="",
        source_file="src.md",
        source_line=1,
        tags={},
        status=ItemStatus.WORKING,
        worktree_path=None,
        branch=None,
        attempts=0,
        last_error=None,
        feedback=None,
        result_json=result_json,
        session_id=None,
        agentor_version=None,
        priority=0,
        created_at=0.0,
        updated_at=0.0,
    )


class TestFmtElapsed(unittest.TestCase):
    def test_none_is_emdash(self):
        self.assertEqual(_fmt_elapsed(None), "—:—")

    def test_sub_minute(self):
        self.assertEqual(_fmt_elapsed(45), "00:45")

    def test_minutes_seconds(self):
        self.assertEqual(_fmt_elapsed(125), "02:05")

    def test_hours(self):
        self.assertEqual(_fmt_elapsed(3725), "01:02:05")

    def test_exact_minute(self):
        self.assertEqual(_fmt_elapsed(60), "01:00")


class TestFmtTokens(unittest.TestCase):
    def test_below_thousand(self):
        self.assertEqual(_fmt_tokens(500), "500")

    def test_just_below_thousand(self):
        self.assertEqual(_fmt_tokens(999), "999")

    def test_thousands(self):
        self.assertEqual(_fmt_tokens(1500), "1.5k")

    def test_millions(self):
        self.assertEqual(_fmt_tokens(1_500_000), "1.5M")


class TestCtxFillPct(unittest.TestCase):
    def test_no_result_json(self):
        self.assertEqual(_ctx_fill_pct(_item(None), 200_000), "—")

    def test_reported_window_and_last_turn(self):
        payload = {
            "modelUsage": {
                "claude-opus-4-6": {"contextWindow": 200_000},
            },
            "iterations": [
                {"input_tokens": 10_000, "cache_read_input_tokens": 0},
                {"input_tokens": 50_000, "cache_read_input_tokens": 50_000},
            ],
        }
        self.assertEqual(
            _ctx_fill_pct(_item(json.dumps(payload)), 200_000),
            "50%",
        )

    def test_observed_max_bumps_window_to_1m(self):
        # No contextWindow in modelUsage; last turn exceeds 200k so the
        # window estimator should pick the 1M variant, not divide by 200k.
        payload = {
            "modelUsage": {"claude-opus-4-6": {}},
            "iterations": [
                {"input_tokens": 600_000, "cache_read_input_tokens": 0},
            ],
        }
        self.assertEqual(
            _ctx_fill_pct(_item(json.dumps(payload)), 200_000),
            "60%",
        )

    def test_usage_fallback_when_no_iterations(self):
        # No `iterations` — falls back to flat usage block. Window stays
        # at fallback_window (200k); input+cache_create = 20k → 10%.
        payload = {
            "usage": {
                "input_tokens": 10_000,
                "cache_creation_input_tokens": 10_000,
                "cache_read_input_tokens": 9_999_999,
            },
        }
        self.assertEqual(
            _ctx_fill_pct(_item(json.dumps(payload)), 200_000),
            "10%",
        )

    def test_usage_missing_entirely(self):
        # Data present but neither iterations nor usage — unknowable.
        self.assertEqual(
            _ctx_fill_pct(_item(json.dumps({"modelUsage": {}})), 200_000),
            "—",
        )


class TestTokenBreakdown(unittest.TestCase):
    def test_empty_when_no_data(self):
        self.assertEqual(_token_breakdown(_item(None)), [])

    def test_empty_when_no_model_usage(self):
        self.assertEqual(
            _token_breakdown(_item(json.dumps({"usage": {}}))),
            [],
        )

    def test_multi_model_sorted_descending(self):
        payload = {
            "modelUsage": {
                "claude-haiku-4-5": {
                    "inputTokens": 10,
                    "outputTokens": 5,
                    "cacheReadInputTokens": 100,
                    "cacheCreationInputTokens": 50,
                },
                "claude-opus-4-6": {
                    "inputTokens": 1000,
                    "outputTokens": 500,
                    "cacheReadInputTokens": 2000,
                    "cacheCreationInputTokens": 1000,
                },
            },
        }
        rows = _token_breakdown(_item(json.dumps(payload)))
        self.assertEqual([r["model"] for r in rows],
                         ["claude-opus-4-6", "claude-haiku-4-5"])
        self.assertEqual(rows[0]["input"], 1000)
        self.assertEqual(rows[0]["output"], 500)
        self.assertEqual(rows[0]["cache_read"], 2000)
        self.assertEqual(rows[0]["cache_create"], 1000)

    def test_non_dict_entries_ignored(self):
        payload = {
            "modelUsage": {
                "claude-opus-4-6": {"inputTokens": 10},
                "garbage": "not-a-dict",
                "also-garbage": 42,
            },
        }
        rows = _token_breakdown(_item(json.dumps(payload)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "claude-opus-4-6")


class _FakeAgentCfg:
    def __init__(self, session_token_budget=0, weekly_token_budget=0):
        self.session_token_budget = session_token_budget
        self.weekly_token_budget = weekly_token_budget


class TestFmtTokenRow(unittest.TestCase):
    """The new one-line token readout replaces the old 4-row panel. Must
    show session/today/7d totals in a single line, append `(NN%)` suffixes
    when budgets set (session + 7d only; today has no budget knob), and
    stay under 50 cols at the narrow tier."""

    def test_wide_line_contains_three_windows_and_totals(self):
        windows = {
            "session": {"total": 0},
            "today": {"total": 11_700_000},
            "7d": {"total": 174_100_000},
        }
        line = _fmt_token_row(windows)
        self.assertIn("tokens", line)
        self.assertIn("session 0", line)
        self.assertIn("today 11.7M", line)
        self.assertIn("7d 174.1M", line)

    def test_missing_windows_render_zero(self):
        self.assertEqual(
            _fmt_token_row({}),
            "tokens  session 0  today 0  7d 0",
        )

    def test_wide_no_suffix_when_budgets_zero(self):
        windows = {"session": {"total": 500_000},
                   "today": {"total": 100_000},
                   "7d": {"total": 5_000_000}}
        cfg = _FakeAgentCfg()
        line = _fmt_token_row(windows, cfg)
        self.assertNotIn("%", line)

    def test_wide_pct_suffix_when_budgets_set(self):
        windows = {"session": {"total": 500_000},
                   "today": {"total": 100_000},
                   "7d": {"total": 5_000_000}}
        cfg = _FakeAgentCfg(
            session_token_budget=1_000_000,
            weekly_token_budget=10_000_000,
        )
        line = _fmt_token_row(windows, cfg)
        self.assertIn("session 500.0k (50%)", line)
        self.assertIn("7d 5.0M (50%)", line)
        # today has no budget knob — never carries a suffix.
        self.assertIn("today 100.0k  ", line)

    def test_agent_cfg_none_behaves_like_unconfigured(self):
        windows = {"session": {"total": 1500},
                   "today": {"total": 900},
                   "7d": {"total": 2_300_000}}
        line = _fmt_token_row(windows, None)
        self.assertEqual(
            line,
            "tokens  session 1.5k  today 900  7d 2.3M",
        )

    def test_narrow_uses_short_labels(self):
        windows = {"session": {"total": 1500},
                   "today": {"total": 900},
                   "7d": {"total": 2_300_000}}
        line = _fmt_token_row(windows, None, tier="narrow")
        self.assertIn("tok", line)
        self.assertIn("s=1.5k", line)
        self.assertIn("t=900", line)
        self.assertIn("w=2.3M", line)
        self.assertNotIn("session", line)

    def test_narrow_fits_50_cols_with_m_scale_totals(self):
        # M-scale everywhere plus session + weekly pct suffixes (the
        # widest shape the formatter emits) must still fit a 50-col
        # terminal with the leading space the renderer prepends.
        windows = {"session": {"total": 174_100_000},
                   "today": {"total": 11_700_000},
                   "7d": {"total": 174_100_000}}
        cfg = _FakeAgentCfg(
            session_token_budget=200_000_000,
            weekly_token_budget=200_000_000,
        )
        line = _fmt_token_row(windows, cfg, tier="narrow")
        self.assertLessEqual(len(line) + 1, 50)


class TestFmtTokenCompact(unittest.TestCase):
    def test_session_and_weekly_totals(self):
        windows = {
            "session": {"total": 1500},
            "today": {"total": 900},
            "7d": {"total": 2_300_000},
        }
        self.assertEqual(
            _fmt_token_compact(windows),
            "tok sess=1.5k  wk=2.3M",
        )

    def test_missing_windows_render_zero(self):
        self.assertEqual(
            _fmt_token_compact({}),
            "tok sess=0  wk=0",
        )

    def test_missing_total_key_renders_zero(self):
        # `total` may be absent if aggregate returned an empty dict for the
        # window; must not raise.
        self.assertEqual(
            _fmt_token_compact({"session": {}, "7d": {}}),
            "tok sess=0  wk=0",
        )


class TestFmtTokenCompactPct(unittest.TestCase):
    def test_no_suffix_when_budgets_zero(self):
        windows = {"session": {"total": 1500}, "7d": {"total": 2_300_000}}
        cfg = _FakeAgentCfg()
        self.assertEqual(
            _fmt_token_compact(windows, cfg),
            "tok sess=1.5k  wk=2.3M",
        )

    def test_pct_suffix_when_budgets_set(self):
        windows = {
            "session": {"total": 500_000},
            "7d": {"total": 5_000_000},
        }
        cfg = _FakeAgentCfg(
            session_token_budget=1_000_000,
            weekly_token_budget=10_000_000,
        )
        self.assertEqual(
            _fmt_token_compact(windows, cfg),
            "tok sess=500.0k (50%)  wk=5.0M (50%)",
        )

    def test_zero_totals_render_zero_pct(self):
        windows = {"session": {"total": 0}, "7d": {"total": 0}}
        cfg = _FakeAgentCfg(
            session_token_budget=1_000_000,
            weekly_token_budget=10_000_000,
        )
        self.assertEqual(
            _fmt_token_compact(windows, cfg),
            "tok sess=0 (0%)  wk=0 (0%)",
        )

    def test_overbudget_clamps_to_gt99(self):
        windows = {
            "session": {"total": 2_000_000},
            "7d": {"total": 20_000_000},
        }
        cfg = _FakeAgentCfg(
            session_token_budget=1_000_000,
            weekly_token_budget=10_000_000,
        )
        self.assertEqual(
            _fmt_token_compact(windows, cfg),
            "tok sess=2.0M (>99%)  wk=20.0M (>99%)",
        )

    def test_only_session_budget_set(self):
        # Partial config: only one budget is honored, the other stays
        # suffix-less so operators aren't forced to set both.
        windows = {"session": {"total": 500_000}, "7d": {"total": 5_000_000}}
        cfg = _FakeAgentCfg(session_token_budget=1_000_000)
        self.assertEqual(
            _fmt_token_compact(windows, cfg),
            "tok sess=500.0k (50%)  wk=5.0M",
        )

    def test_agent_cfg_none_behaves_like_unconfigured(self):
        windows = {"session": {"total": 1500}, "7d": {"total": 2_300_000}}
        self.assertEqual(
            _fmt_token_compact(windows, None),
            "tok sess=1.5k  wk=2.3M",
        )


class TestTokenWindows(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.store = Store(Path(self.td.name) / "state.db")

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _seed_item(self, item_id: str, input_tokens: int,
                   updated_at: float) -> None:
        item = Item(
            id=item_id, title="t", body="",
            source_file="s.md", source_line=1, tags={},
        )
        self.store.upsert_discovered(item)
        self.store.update_result_json(
            item_id,
            json.dumps({"usage": {"input_tokens": input_tokens}}),
        )
        self.store.conn.execute(
            "UPDATE items SET updated_at = ? WHERE id = ?",
            (updated_at, item_id),
        )

    def test_three_keys_present(self):
        windows = _token_windows(self.store, daemon_started_at=0.0)
        self.assertEqual(set(windows.keys()), {"session", "today", "7d"})

    def test_session_since_daemon_start(self):
        import time as _time
        now = _time.time()
        self._seed_item("before", 99, updated_at=now - 60)
        self._seed_item("after", 7, updated_at=now + 1)
        windows = _token_windows(self.store, daemon_started_at=now)
        # session starts now → only "after" row counts.
        self.assertEqual(windows["session"]["input"], 7)
        # 7d spans both.
        self.assertEqual(windows["7d"]["input"], 106)

    def test_session_falls_back_to_today_when_not_started(self):
        # daemon_started_at == 0 → session mirrors today so the panel is
        # populated even when the Daemon.run() loop hasn't fired yet.
        import time as _time
        now = _time.time()
        self._seed_item("a", 11, updated_at=now)
        windows = _token_windows(self.store, daemon_started_at=0.0)
        self.assertEqual(windows["session"], windows["today"])


class _FakeStore:
    """Minimal stand-in for Store that counts aggregate_token_usage calls.
    `_token_windows` only needs that one method on its collaborator."""

    def __init__(self) -> None:
        self.calls: list[float | None] = []

    def aggregate_token_usage(self, since: float | None = None) -> dict:
        self.calls.append(since)
        return {"input": 1, "output": 0, "cache_read": 0,
                "cache_create": 0, "total": 1}


class TestTokenWindowsCache(unittest.TestCase):
    def setUp(self):
        _token_windows_invalidate()

    def tearDown(self):
        _token_windows_invalidate()

    def test_first_call_delegates_three_times(self):
        store = _FakeStore()
        _token_windows(store, daemon_started_at=0.0)
        # One aggregate per window (session, today, 7d).
        self.assertEqual(len(store.calls), 3)

    def test_second_call_within_ttl_is_cached(self):
        store = _FakeStore()
        first = _token_windows(store, daemon_started_at=0.0)
        second = _token_windows(store, daemon_started_at=0.0)
        self.assertEqual(len(store.calls), 3)  # still 3, not 6
        # Same dict object returned — confirms the cache hit path.
        self.assertIs(first, second)

    def test_invalidate_forces_recompute(self):
        store = _FakeStore()
        _token_windows(store, daemon_started_at=0.0)
        _token_windows_invalidate()
        _token_windows(store, daemon_started_at=0.0)
        self.assertEqual(len(store.calls), 6)

    def test_ttl_expiry_forces_recompute(self):
        store = _FakeStore()
        base = 1_000_000.0
        with mock.patch.object(formatters.time, "time", return_value=base):
            _token_windows(store, daemon_started_at=0.0)
            self.assertEqual(len(store.calls), 3)
        # Jump past the TTL window (2.0s).
        with mock.patch.object(formatters.time, "time", return_value=base + 5.0):
            _token_windows(store, daemon_started_at=0.0)
        self.assertEqual(len(store.calls), 6)

    def test_different_store_identity_busts_cache(self):
        store_a = _FakeStore()
        store_b = _FakeStore()
        _token_windows(store_a, daemon_started_at=0.0)
        _token_windows(store_b, daemon_started_at=0.0)
        # Each store computed its own three aggregates.
        self.assertEqual(len(store_a.calls), 3)
        self.assertEqual(len(store_b.calls), 3)

    def test_different_daemon_start_busts_cache(self):
        store = _FakeStore()
        _token_windows(store, daemon_started_at=0.0)
        _token_windows(store, daemon_started_at=123.0)
        self.assertEqual(len(store.calls), 6)


if __name__ == "__main__":
    unittest.main()
