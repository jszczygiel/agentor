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
    _result_data,
    _result_data_invalidate,
    _token_breakdown,
    _token_windows,
    _token_windows_invalidate,
)
from agentor.models import Item, ItemStatus
from agentor.store import Store, StoredItem


_item_counter = 0


def _item(result_json: str | None, *,
          item_id: str = "abc12345",
          updated_at: float | None = None) -> StoredItem:
    """Build a minimal StoredItem with only the fields the formatters read.

    Each call gets a unique synthetic `updated_at` by default so the
    `_result_data` cache (keyed on `(id, updated_at)`) can't leak between
    tests that reuse the same item id with different payloads. Tests that
    want to exercise cache-hit behaviour pass an explicit `updated_at`."""
    global _item_counter
    if updated_at is None:
        _item_counter += 1
        updated_at = float(_item_counter)
    return StoredItem(
        id=item_id,
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
        agent_ref=None,
        agentor_version=None,
        priority=0,
        created_at=0.0,
        updated_at=updated_at,
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

    def test_codex_caps_short_circuits_to_emdash(self):
        """`_ctx_fill_pct` gates on `caps.reports_context_window`. With
        `CODEX_CAPS` the result is always `—` regardless of iteration
        data — proves the capability flag is the authoritative gate,
        not the empty-modelUsage path that happened to give the same
        answer before. A non-zero `fallback_window` and populated
        iterations prove no fallback path leaks through."""
        from agentor.capabilities import CLAUDE_CAPS, CODEX_CAPS

        payload = {
            "modelUsage": {},
            "iterations": [
                {"input_tokens": 50_000, "cache_read_input_tokens": 50_000},
            ],
        }
        item = _item(json.dumps(payload))

        # Codex cap → short-circuit even though iterations would compute.
        self.assertEqual(_ctx_fill_pct(item, 200_000, CODEX_CAPS), "—")
        # Claude cap → existing behaviour computes a % from the last
        # iteration against the fallback window (100k / 200k = 50%).
        self.assertEqual(_ctx_fill_pct(item, 200_000, CLAUDE_CAPS), "50%")


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
    """The token readout mirrors claude.ai/settings/usage's two cells —
    rolling 5-hour and rolling weekly windows — and leads with `NN%` when
    the matching budget is configured."""

    def test_wide_line_contains_5h_and_weekly_totals_no_budget(self):
        windows = {
            "5h": {"total": 11_700_000},
            "week": {"total": 174_100_000},
        }
        line = _fmt_token_row(windows)
        self.assertIn("usage", line)
        self.assertIn("5h 11.7M", line)
        self.assertIn("wk 174.1M", line)
        self.assertNotIn("%", line)

    def test_missing_windows_render_zero(self):
        self.assertEqual(
            _fmt_token_row({}),
            "usage  5h 0  wk 0",
        )

    def test_wide_no_suffix_when_budgets_zero(self):
        windows = {"5h": {"total": 500_000},
                   "week": {"total": 5_000_000}}
        cfg = _FakeAgentCfg()
        line = _fmt_token_row(windows, cfg)
        self.assertNotIn("%", line)

    def test_wide_pct_leads_when_budgets_set(self):
        windows = {"5h": {"total": 500_000},
                   "week": {"total": 5_000_000}}
        cfg = _FakeAgentCfg(
            session_token_budget=1_000_000,
            weekly_token_budget=10_000_000,
        )
        line = _fmt_token_row(windows, cfg)
        # Percent leads each cell, raw counts follow in parens.
        self.assertIn("5h 50% (500.0k / 1.0M)", line)
        self.assertIn("wk 50% (5.0M / 10.0M)", line)

    def test_agent_cfg_none_behaves_like_unconfigured(self):
        windows = {"5h": {"total": 1500},
                   "week": {"total": 2_300_000}}
        line = _fmt_token_row(windows, None)
        self.assertEqual(line, "usage  5h 1.5k  wk 2.3M")

    def test_narrow_uses_compact_pct_only(self):
        windows = {"5h": {"total": 1500},
                   "week": {"total": 2_300_000}}
        line = _fmt_token_row(windows, None, tier="narrow")
        self.assertIn("tok", line)
        self.assertIn("5h=1.5k", line)
        self.assertIn("wk=2.3M", line)
        self.assertNotIn("usage", line)

    def test_narrow_fits_50_cols_with_m_scale_totals(self):
        # M-scale totals plus 5h + weekly pct suffixes (the widest shape
        # the narrow formatter emits) must still fit a 50-col terminal
        # with the leading space the renderer prepends.
        windows = {"5h": {"total": 174_100_000},
                   "week": {"total": 174_100_000}}
        cfg = _FakeAgentCfg(
            session_token_budget=200_000_000,
            weekly_token_budget=200_000_000,
        )
        line = _fmt_token_row(windows, cfg, tier="narrow")
        self.assertLessEqual(len(line) + 1, 50)


class TestFmtTokenCompact(unittest.TestCase):
    def test_5h_and_weekly_totals(self):
        windows = {
            "5h": {"total": 1500},
            "week": {"total": 2_300_000},
        }
        self.assertEqual(
            _fmt_token_compact(windows),
            "tok 5h=1.5k  wk=2.3M",
        )

    def test_missing_windows_render_zero(self):
        self.assertEqual(
            _fmt_token_compact({}),
            "tok 5h=0  wk=0",
        )

    def test_missing_total_key_renders_zero(self):
        # `total` may be absent if aggregate returned an empty dict for the
        # window; must not raise.
        self.assertEqual(
            _fmt_token_compact({"5h": {}, "week": {}}),
            "tok 5h=0  wk=0",
        )


class TestFmtTokenCompactPct(unittest.TestCase):
    def test_no_pct_when_budgets_zero(self):
        windows = {"5h": {"total": 1500}, "week": {"total": 2_300_000}}
        cfg = _FakeAgentCfg()
        self.assertEqual(
            _fmt_token_compact(windows, cfg),
            "tok 5h=1.5k  wk=2.3M",
        )

    def test_pct_replaces_total_when_budgets_set(self):
        windows = {
            "5h": {"total": 500_000},
            "week": {"total": 5_000_000},
        }
        cfg = _FakeAgentCfg(
            session_token_budget=1_000_000,
            weekly_token_budget=10_000_000,
        )
        self.assertEqual(
            _fmt_token_compact(windows, cfg),
            "tok 5h=50%  wk=50%",
        )

    def test_zero_totals_render_zero_pct(self):
        windows = {"5h": {"total": 0}, "week": {"total": 0}}
        cfg = _FakeAgentCfg(
            session_token_budget=1_000_000,
            weekly_token_budget=10_000_000,
        )
        self.assertEqual(
            _fmt_token_compact(windows, cfg),
            "tok 5h=0%  wk=0%",
        )

    def test_overbudget_clamps_to_gt99(self):
        windows = {
            "5h": {"total": 2_000_000},
            "week": {"total": 20_000_000},
        }
        cfg = _FakeAgentCfg(
            session_token_budget=1_000_000,
            weekly_token_budget=10_000_000,
        )
        self.assertEqual(
            _fmt_token_compact(windows, cfg),
            "tok 5h=>99%  wk=>99%",
        )

    def test_only_session_budget_set(self):
        # Partial config: only one budget is honored, the other stays
        # raw-total so operators aren't forced to set both.
        windows = {"5h": {"total": 500_000}, "week": {"total": 5_000_000}}
        cfg = _FakeAgentCfg(session_token_budget=1_000_000)
        self.assertEqual(
            _fmt_token_compact(windows, cfg),
            "tok 5h=50%  wk=5.0M",
        )

    def test_agent_cfg_none_behaves_like_unconfigured(self):
        windows = {"5h": {"total": 1500}, "week": {"total": 2_300_000}}
        self.assertEqual(
            _fmt_token_compact(windows, None),
            "tok 5h=1.5k  wk=2.3M",
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

    def test_two_keys_present(self):
        windows = _token_windows(self.store, daemon_started_at=0.0)
        self.assertEqual(set(windows.keys()), {"5h", "week"})

    def test_5h_window_excludes_older_rows(self):
        import time as _time
        now = _time.time()
        # 6h ago is outside the 5h rolling window; 1h ago is inside.
        self._seed_item("old", 99, updated_at=now - 6 * 3600)
        self._seed_item("recent", 7, updated_at=now - 3600)
        windows = _token_windows(self.store, daemon_started_at=0.0)
        # Only "recent" rolls into the 5h window.
        self.assertEqual(windows["5h"]["input"], 7)
        # Both fall inside the 7-day window.
        self.assertEqual(windows["week"]["input"], 106)

    def test_windows_are_rolling_not_session_anchored(self):
        # A non-zero daemon_started_at must not gate the 5h window —
        # both windows are rolling against now() so the dashboard mirrors
        # claude.ai/settings/usage's rolling-window semantics.
        import time as _time
        now = _time.time()
        self._seed_item("a", 11, updated_at=now - 3600)
        self._seed_item("b", 22, updated_at=now - 60)
        # daemon_started_at=now would have hidden "a" under the old
        # session-anchored windowing; under the new rolling 5h window
        # both rows count.
        windows = _token_windows(self.store, daemon_started_at=now)
        self.assertEqual(windows["5h"]["input"], 33)


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

    def test_first_call_delegates_twice(self):
        store = _FakeStore()
        _token_windows(store, daemon_started_at=0.0)
        # One aggregate per window (5h, week).
        self.assertEqual(len(store.calls), 2)

    def test_second_call_within_ttl_is_cached(self):
        store = _FakeStore()
        first = _token_windows(store, daemon_started_at=0.0)
        second = _token_windows(store, daemon_started_at=0.0)
        self.assertEqual(len(store.calls), 2)  # still 2, not 4
        # Same dict object returned — confirms the cache hit path.
        self.assertIs(first, second)

    def test_invalidate_forces_recompute(self):
        store = _FakeStore()
        _token_windows(store, daemon_started_at=0.0)
        _token_windows_invalidate()
        _token_windows(store, daemon_started_at=0.0)
        self.assertEqual(len(store.calls), 4)

    def test_ttl_expiry_forces_recompute(self):
        store = _FakeStore()
        base = 1_000_000.0
        with mock.patch.object(formatters.time, "time", return_value=base):
            _token_windows(store, daemon_started_at=0.0)
            self.assertEqual(len(store.calls), 2)
        # Jump past the TTL window (2.0s).
        with mock.patch.object(formatters.time, "time", return_value=base + 5.0):
            _token_windows(store, daemon_started_at=0.0)
        self.assertEqual(len(store.calls), 4)

    def test_different_store_identity_busts_cache(self):
        store_a = _FakeStore()
        store_b = _FakeStore()
        _token_windows(store_a, daemon_started_at=0.0)
        _token_windows(store_b, daemon_started_at=0.0)
        # Each store computed its own two aggregates.
        self.assertEqual(len(store_a.calls), 2)
        self.assertEqual(len(store_b.calls), 2)

    def test_different_daemon_start_busts_cache(self):
        store = _FakeStore()
        _token_windows(store, daemon_started_at=0.0)
        _token_windows(store, daemon_started_at=123.0)
        self.assertEqual(len(store.calls), 4)

    def test_5h_since_threshold_is_rolling(self):
        store = _FakeStore()
        base = 1_000_000.0
        with mock.patch.object(formatters.time, "time", return_value=base):
            _token_windows(store, daemon_started_at=0.0)
        # Exactly two calls; first is 5h-since (now - 5*3600), second is
        # week-since (now - 7*86400).
        self.assertEqual(store.calls[0], base - 5 * 3600)
        self.assertEqual(store.calls[1], base - 7 * 86400)


class TestResultDataCache(unittest.TestCase):
    def setUp(self):
        _result_data_invalidate()

    def tearDown(self):
        _result_data_invalidate()

    def test_same_updated_at_reuses_cached_parse(self):
        item = _item(json.dumps({"phase": "plan"}),
                     item_id="item-1", updated_at=1.0)
        with mock.patch.object(formatters.json, "loads",
                               wraps=json.loads) as spy:
            first = _result_data(item)
            second = _result_data(item)
            self.assertEqual(spy.call_count, 1)
        self.assertEqual(first, {"phase": "plan"})
        self.assertIs(first, second)

    def test_bumping_updated_at_evicts_prior_key(self):
        item_v1 = _item(json.dumps({"phase": "plan"}),
                        item_id="item-1", updated_at=1.0)
        item_v2 = _item(json.dumps({"phase": "execute"}),
                        item_id="item-1", updated_at=2.0)
        _result_data(item_v1)
        _result_data(item_v2)
        # Exactly one entry retained for this id.
        keys_for_item = [k for k in formatters._result_cache
                         if k[0] == "item-1"]
        self.assertEqual(keys_for_item, [("item-1", 2.0)])

    def test_invalidate_clears_cache(self):
        item = _item(json.dumps({"phase": "plan"}),
                     item_id="item-1", updated_at=1.0)
        _result_data(item)
        self.assertEqual(len(formatters._result_cache), 1)
        _result_data_invalidate()
        self.assertEqual(len(formatters._result_cache), 0)

    def test_invalid_json_not_cached(self):
        bad = _item("{not json", item_id="item-1", updated_at=1.0)
        self.assertIsNone(_result_data(bad))
        self.assertEqual(len(formatters._result_cache), 0)
        # A later call still returns None — no cache poisoning.
        self.assertIsNone(_result_data(bad))

    def test_empty_result_json_returns_none_without_caching(self):
        empty = _item(None, item_id="item-1", updated_at=1.0)
        self.assertIsNone(_result_data(empty))
        self.assertEqual(len(formatters._result_cache), 0)

    def test_distinct_items_retain_separate_entries(self):
        a = _item(json.dumps({"phase": "plan"}),
                  item_id="item-a", updated_at=1.0)
        b = _item(json.dumps({"phase": "execute"}),
                  item_id="item-b", updated_at=1.0)
        _result_data(a)
        _result_data(b)
        self.assertEqual(len(formatters._result_cache), 2)


if __name__ == "__main__":
    unittest.main()
