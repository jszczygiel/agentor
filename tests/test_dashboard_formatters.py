import json
import unittest

from agentor.dashboard.formatters import (
    _ctx_fill_pct,
    _fmt_elapsed,
    _fmt_tokens,
    _token_breakdown,
)
from agentor.models import ItemStatus
from agentor.store import StoredItem


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


if __name__ == "__main__":
    unittest.main()
