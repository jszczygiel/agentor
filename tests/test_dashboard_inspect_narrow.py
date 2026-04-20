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


_mk_item_counter = 0


def _mk_item(
    result: dict, status: ItemStatus = ItemStatus.WORKING,
    last_error: str | None = None,
) -> StoredItem:
    # Each call gets a unique `updated_at` so the `_result_data` cache
    # (keyed on `(id, updated_at)`) can't return a prior test's payload.
    global _mk_item_counter
    _mk_item_counter += 1
    return StoredItem(
        id="abc12345", title="t", body="", source_file="s.md",
        source_line=1, tags={}, status=status,
        worktree_path=None, branch=None, attempts=0, last_error=last_error,
        feedback=None, result_json=json.dumps(result), agent_ref=None,
        agentor_version=None, priority=0, created_at=0.0,
        updated_at=float(_mk_item_counter),
    )


class _Agent:
    max_attempts = 3
    runner = "claude"
    pool_size = 1
    context_window = 200_000
    auto_execute_model = True


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


_REVIEW_TOKEN_RESULT = {
    "modelUsage": {
        "claude-opus-4-7": {
            "inputTokens": 1000, "outputTokens": 500,
            "cacheReadInputTokens": 2000,
            "cacheCreationInputTokens": 300,
        },
    },
    "num_turns": 7,
    "duration_ms": 42_000,
    "duration_api_ms": 30_000,
    "stop_reason": "end_turn",
    "phase": "plan",
    "plan": "draft plan body here",
    "files_changed": ["agentor/foo.py", "tests/test_foo.py"],
    "summary": "implementation summary",
}


class TestApproveModeStripsRunMechanics(unittest.TestCase):
    """AWAITING_PLAN_REVIEW / AWAITING_REVIEW screens drop run-mechanics
    (transcript, token breakdown, agent-run stats, failure history,
    last_error, live progress) and keep only metadata + decision content
    (plan / files-changed / summary / pending feedback)."""

    def setUp(self) -> None:
        self.td = TemporaryDirectory()
        self.cfg = _Cfg()
        self.cfg.project_root = Path(self.td.name)
        self.store = Store(Path(self.td.name) / "state.db")

    def tearDown(self) -> None:
        self.store.close()
        self.td.cleanup()

    def _assert_no_run_mechanics(self, lines: list[str]) -> None:
        forbidden = (
            "── agent run ──",
            "── per-model tokens ──",
            "── failure history ──",
            "── session activity ──",
            "── transcript tail ──",
        )
        for marker in forbidden:
            self.assertFalse(
                any(marker in ln for ln in lines),
                f"review view should not contain {marker!r}; got: {lines}",
            )
        self.assertFalse(any(ln.startswith("log:") for ln in lines))
        self.assertFalse(any(ln.startswith("live:") for ln in lines))
        self.assertFalse(any(ln.startswith("tokens:") for ln in lines))
        self.assertFalse(any(ln.startswith("last_error:") for ln in lines))

    def test_plan_review_hides_run_mechanics(self):
        item = _mk_item(
            _REVIEW_TOKEN_RESULT,
            status=ItemStatus.AWAITING_PLAN_REVIEW,
            last_error="stale error from prior attempt",
        )
        lines = _build_detail_lines(self.cfg, self.store, item, width=120)
        self._assert_no_run_mechanics(lines)
        self.assertTrue(any("── plan ──" in ln for ln in lines))
        self.assertTrue(any("draft plan body here" in ln for ln in lines))

    def test_code_review_hides_run_mechanics(self):
        item = _mk_item(
            _REVIEW_TOKEN_RESULT,
            status=ItemStatus.AWAITING_REVIEW,
        )
        lines = _build_detail_lines(self.cfg, self.store, item, width=120)
        self._assert_no_run_mechanics(lines)
        self.assertTrue(
            any("── files changed (2) ──" in ln for ln in lines)
        )
        self.assertTrue(any("── summary ──" in ln for ln in lines))
        self.assertTrue(any("implementation summary" in ln for ln in lines))

    def test_plan_review_without_plan_text_shows_placeholder(self):
        item = _mk_item({}, status=ItemStatus.AWAITING_PLAN_REVIEW)
        lines = _build_detail_lines(self.cfg, self.store, item, width=120)
        self._assert_no_run_mechanics(lines)
        self.assertTrue(any("── plan ──" in ln for ln in lines))
        self.assertTrue(
            any("(no plan text captured)" in ln for ln in lines)
        )

    def test_working_keeps_run_mechanics(self):
        item = _mk_item(
            _REVIEW_TOKEN_RESULT, status=ItemStatus.WORKING,
        )
        lines = _build_detail_lines(self.cfg, self.store, item, width=120)
        self.assertTrue(any("── agent run ──" in ln for ln in lines))
        self.assertTrue(any("── per-model tokens ──" in ln for ln in lines))


_PLAN_WITH_TIER = "## Execute tier\nsuggested_model: haiku\nreason: trivial"
_PLAN_WITHOUT_TIER = "## Implementation\nDo the thing."


class TestExecuteModelInspectLine(unittest.TestCase):
    def setUp(self) -> None:
        self.td = TemporaryDirectory()
        self.cfg = _Cfg()
        self.cfg.project_root = Path(self.td.name)
        self.store = Store(Path(self.td.name) / "state.db")

    def tearDown(self) -> None:
        self.store.close()
        self.td.cleanup()

    def test_plan_review_shows_suggestion_when_trailer_present(self):
        item = _mk_item(
            {"plan": _PLAN_WITH_TIER},
            status=ItemStatus.AWAITING_PLAN_REVIEW,
        )
        lines = _build_detail_lines(self.cfg, self.store, item, width=120)
        self.assertTrue(
            any(ln.startswith("suggested: haiku") for ln in lines),
            f"expected 'suggested: haiku' in lines; got: {lines}",
        )

    def test_plan_review_no_trailer_omits_suggestion(self):
        item = _mk_item(
            {"plan": _PLAN_WITHOUT_TIER},
            status=ItemStatus.AWAITING_PLAN_REVIEW,
        )
        lines = _build_detail_lines(self.cfg, self.store, item, width=120)
        self.assertFalse(any("suggested:" in ln for ln in lines))

    def test_plan_review_advisory_when_auto_execute_model_off(self):
        cfg = _Cfg()
        cfg.project_root = Path(self.td.name)
        cfg.agent = type("A", (), {
            "max_attempts": 3, "runner": "claude", "pool_size": 1,
            "context_window": 200_000, "auto_execute_model": False,
        })()
        item = _mk_item(
            {"plan": _PLAN_WITH_TIER},
            status=ItemStatus.AWAITING_PLAN_REVIEW,
        )
        lines = _build_detail_lines(cfg, self.store, item, width=120)
        self.assertTrue(
            any("auto_execute_model=false" in ln for ln in lines),
            f"expected advisory suffix; got: {lines}",
        )

    def test_post_execute_shows_both_lines(self):
        item = _mk_item(
            {
                "plan": _PLAN_WITH_TIER,
                "execute_model": "sonnet",
                "execute_model_source": "tag",
            },
            status=ItemStatus.AWAITING_REVIEW,
        )
        lines = _build_detail_lines(self.cfg, self.store, item, width=120)
        self.assertTrue(any(ln.startswith("suggested: haiku") for ln in lines))
        self.assertTrue(
            any("execute:   sonnet (source: tag)" in ln for ln in lines),
            f"expected execute line; got: {lines}",
        )

    def test_post_execute_without_execute_model_omits_execute_line(self):
        item = _mk_item(
            {"plan": _PLAN_WITH_TIER},
            status=ItemStatus.AWAITING_REVIEW,
        )
        lines = _build_detail_lines(self.cfg, self.store, item, width=120)
        self.assertTrue(any(ln.startswith("suggested: haiku") for ln in lines))
        self.assertFalse(any(ln.startswith("execute:") for ln in lines))


if __name__ == "__main__":
    unittest.main()
