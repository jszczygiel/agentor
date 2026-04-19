"""Width-aware rendering of the inspect detail view. At narrow terminal
widths the token-breakdown table must reflow to one field per line so
nothing wraps catastrophically."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentor.dashboard.modes import _build_detail_lines
from agentor.models import ItemStatus
from agentor.store import Store, StoredItem


def _mk_item(result: dict) -> StoredItem:
    return StoredItem(
        id="abc12345", title="t", body="", source_file="s.md",
        source_line=1, tags={}, status=ItemStatus.WORKING,
        worktree_path=None, branch=None, attempts=0, last_error=None,
        feedback=None, result_json=json.dumps(result), session_id=None,
        agentor_version=None, priority=0, created_at=0.0, updated_at=0.0,
    )


class _Agent:
    max_attempts = 3
    runner = "claude"
    pool_size = 1
    context_window = 200_000


class _Git:
    base_branch = "main"


class _Cfg:
    agent = _Agent()
    git = _Git()
    project_name = "p"


class TestInspectTokenBreakdownWidth(unittest.TestCase):
    def setUp(self) -> None:
        self.td = TemporaryDirectory()
        self.cfg = _Cfg()
        self.cfg.project_root = Path(self.td.name)
        self.store = Store(Path(self.td.name) / "state.db")
        self.item = _mk_item({
            "modelUsage": {
                "claude-opus-4-7": {
                    "inputTokens": 1000, "outputTokens": 500,
                    "cacheReadInputTokens": 2000,
                    "cacheCreationInputTokens": 300,
                },
            },
        })

    def tearDown(self) -> None:
        self.store.close()
        self.td.cleanup()

    def test_wide_uses_tabular_form(self):
        lines = _build_detail_lines(self.cfg, self.store, self.item, width=120)
        self.assertTrue(any("MODEL" in ln and "CACHE_R" in ln for ln in lines))

    def test_mid_uses_compact_two_line_form(self):
        lines = _build_detail_lines(self.cfg, self.store, self.item, width=60)
        self.assertFalse(any(ln.startswith("MODEL") for ln in lines))
        # Compact stacks `in= out= cr= cw=` on one line.
        self.assertTrue(any("cr=" in ln and "cw=" in ln for ln in lines))

    def test_narrow_one_field_per_line(self):
        lines = _build_detail_lines(self.cfg, self.store, self.item, width=40)
        # One field per line — the dedicated in/out/cache_r/cache_w rows
        # should each appear on their own indented line.
        in_lines = [ln for ln in lines if ln.strip().startswith("in:")]
        out_lines = [ln for ln in lines if ln.strip().startswith("out:")]
        cr_lines = [ln for ln in lines if ln.strip().startswith("cache_r:")]
        cw_lines = [ln for ln in lines if ln.strip().startswith("cache_w:")]
        self.assertEqual(len(in_lines), 1)
        self.assertEqual(len(out_lines), 1)
        self.assertEqual(len(cr_lines), 1)
        self.assertEqual(len(cw_lines), 1)

    def test_narrow_lines_fit_width(self):
        lines = _build_detail_lines(self.cfg, self.store, self.item, width=40)
        # No line in the per-model block should exceed 40 cols.
        in_block = False
        for ln in lines:
            if ln == "── per-model tokens ──":
                in_block = True
                continue
            if in_block:
                if ln.startswith("──"):
                    break
                self.assertLessEqual(
                    len(ln), 40,
                    f"narrow token line exceeds 40 cols: {ln!r}"
                )


if __name__ == "__main__":
    unittest.main()
