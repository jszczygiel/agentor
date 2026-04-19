import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentor.dashboard.formatters import (
    _ctx_fill_pct,
    _fmt_elapsed,
    _fmt_token_line,
    _fmt_tokens,
    _token_breakdown,
    _token_windows,
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


class TestFmtTokenLine(unittest.TestCase):
    def test_contains_all_buckets(self):
        totals = {
            "input": 100, "output": 50,
            "cache_read": 2000, "cache_create": 300,
            "total": 2450,
        }
        line = _fmt_token_line("session", totals)
        self.assertIn("session", line)
        self.assertIn("in ", line)
        self.assertIn("out ", line)
        self.assertIn("cache_r ", line)
        self.assertIn("cache_c ", line)
        # Values rendered via _fmt_tokens — 2000 stays as "2.0k".
        self.assertIn("2.0k", line)

    def test_zero_totals_render_zeros(self):
        totals = {"input": 0, "output": 0,
                  "cache_read": 0, "cache_create": 0, "total": 0}
        line = _fmt_token_line("today", totals)
        # All buckets should read 0, not "—".
        self.assertEqual(line.count("     0"), 5)


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


if __name__ == "__main__":
    unittest.main()
