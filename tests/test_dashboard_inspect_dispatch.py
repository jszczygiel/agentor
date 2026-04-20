"""Exercise `_inspect_dispatch` end-to-end against a real Store. These
tests cover the action keys that don't open a curses prompt so stdscr
can be passed as None — they pin the state-transition contract the
unified inspect view offers."""

import curses
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from agentor.committer import AUTO_RESOLVE_NOTE_PREFIX
from agentor.dashboard.modes import (
    _inspect_dispatch,
    _inspect_footer,
    _inspect_render,
    _is_auto_resolve_chain,
)
from agentor.models import Item, ItemStatus
from agentor.store import Store


class _FakeProcRegistry:
    """Captures `kill_one` invocations so delete tests can assert that the
    WORKING-item teardown path fires. Mirrors the real `ProcRegistry` API
    surface touched by `delete_idea`."""

    def __init__(self) -> None:
        self.killed: list[str] = []

    def kill_one(self, key: str) -> bool:
        self.killed.append(key)
        return False


class _FakeDaemon:
    """Minimal daemon stub — `_inspect_dispatch` needs `try_fill_pool` for
    restore/approve paths and `proc_registry` for the unified delete
    path's WORKING-teardown branch."""

    def __init__(self) -> None:
        self.filled = 0
        self.proc_registry = _FakeProcRegistry()

    def try_fill_pool(self) -> None:
        self.filled += 1


def _mk(id: str, title: str = "t") -> Item:
    return Item(
        id=id, title=title, body="body",
        source_file="backlog.md", source_line=1, tags={},
    )


class TestInspectDispatch(unittest.TestCase):
    def setUp(self) -> None:
        self.td = TemporaryDirectory()
        self.store = Store(Path(self.td.name) / "state.db")
        self.daemon = _FakeDaemon()

    def tearDown(self) -> None:
        self.store.close()
        self.td.cleanup()

    def _seed(self, id: str, status: ItemStatus) -> None:
        self.store.upsert_discovered(_mk(id))
        if status != ItemStatus.QUEUED:
            self.store.transition(id, status, note="seed")

    def _fresh(self, id: str):
        got = self.store.get(id)
        assert got is not None
        return got

    def test_unknown_key_is_ignored(self):
        self._seed("plan1", ItemStatus.QUEUED)
        self.store.transition("plan1", ItemStatus.WORKING, note="t")
        self.store.transition(
            "plan1", ItemStatus.AWAITING_PLAN_REVIEW, note="t",
        )
        acted, msg = _inspect_dispatch(
            None, None, self.store, self.daemon,
            self._fresh("plan1"), "z",
        )
        self.assertFalse(acted)
        self.assertEqual(msg, "")
        self.assertEqual(
            self.store.get("plan1").status, ItemStatus.AWAITING_PLAN_REVIEW,
        )

    def test_plan_review_approve_transitions_to_queued(self):
        self._seed("plan1", ItemStatus.QUEUED)
        self.store.transition("plan1", ItemStatus.WORKING, note="t")
        self.store.transition(
            "plan1", ItemStatus.AWAITING_PLAN_REVIEW, note="t",
        )
        acted, _ = _inspect_dispatch(
            None, None, self.store, self.daemon,
            self._fresh("plan1"), "a",
        )
        self.assertTrue(acted)
        self.assertEqual(self.store.get("plan1").status, ItemStatus.QUEUED)
        self.assertEqual(self.daemon.filled, 1)

    def test_plan_review_approve_with_questions_prompts_for_answers(self):
        """When the agent's plan contained `## Open Questions`, pressing `a`
        on AWAITING_PLAN_REVIEW must open the multiline overlay seeded with
        the Q/A scaffold, then persist the parsed answers into
        `result_json["answers"]` so the runner can inject them during the
        resumed execute phase."""
        import json
        self._seed("plan1", ItemStatus.QUEUED)
        self.store.transition("plan1", ItemStatus.WORKING, note="t")
        self.store.transition(
            "plan1", ItemStatus.AWAITING_PLAN_REVIEW,
            result_json=json.dumps({
                "phase": "plan",
                "plan": "draft",
                "questions": [
                    "Should we keep the legacy flag?",
                    "Where does the lock file live?",
                ],
            }),
            note="t",
        )
        captured: dict = {}

        def fake_multiline(_stdscr, _label, **kwargs):
            captured["initial"] = kwargs.get("initial", "")
            return (
                "Q1: Should we keep the legacy flag?\n"
                "A1: yes, keep for one release\n"
                "\n"
                "Q2: Where does the lock file live?\n"
                "A2: under .agentor/merge.lock\n"
            )

        with patch(
            "agentor.dashboard.modes._prompt_multiline", fake_multiline,
        ):
            acted, msg = _inspect_dispatch(
                None, None, self.store, self.daemon,
                self._fresh("plan1"), "a",
            )
        self.assertTrue(acted)
        self.assertIn("answers", msg)
        self.assertIn("Q1:", captured["initial"])
        self.assertIn("A1: ", captured["initial"])
        self.assertIn("Q2:", captured["initial"])
        got = self.store.get("plan1")
        self.assertEqual(got.status, ItemStatus.QUEUED)
        data = json.loads(got.result_json)
        self.assertEqual(
            data["answers"],
            ["yes, keep for one release", "under .agentor/merge.lock"],
        )

    def test_plan_review_approve_with_questions_empty_reply_cancels(self):
        """Empty reply from the answers overlay must cancel the approve —
        no transition, item stays in AWAITING_PLAN_REVIEW so the operator
        can try again or switch to `r`-reject."""
        import json
        self._seed("plan1", ItemStatus.QUEUED)
        self.store.transition("plan1", ItemStatus.WORKING, note="t")
        self.store.transition(
            "plan1", ItemStatus.AWAITING_PLAN_REVIEW,
            result_json=json.dumps({
                "phase": "plan", "plan": "draft",
                "questions": ["keep flag?"],
            }),
            note="t",
        )
        with patch(
            "agentor.dashboard.modes._prompt_multiline",
            lambda *_a, **_kw: "",
        ):
            acted, msg = _inspect_dispatch(
                None, None, self.store, self.daemon,
                self._fresh("plan1"), "a",
            )
        self.assertFalse(acted)
        self.assertEqual(msg, "")
        self.assertEqual(
            self.store.get("plan1").status, ItemStatus.AWAITING_PLAN_REVIEW,
        )

    def test_plan_review_approve_without_questions_skips_overlay(self):
        """No questions → no overlay. Flow identical to pre-feature — a
        single `approve_plan(store, item)` with answers=None, no prompt."""
        self._seed("plan1", ItemStatus.QUEUED)
        self.store.transition("plan1", ItemStatus.WORKING, note="t")
        self.store.transition(
            "plan1", ItemStatus.AWAITING_PLAN_REVIEW,
            result_json='{"phase":"plan","plan":"draft"}',
            note="t",
        )
        called = {"multiline": 0}

        def fake_multiline(*_a, **_kw):
            called["multiline"] += 1
            return ""

        with patch(
            "agentor.dashboard.modes._prompt_multiline", fake_multiline,
        ):
            acted, _ = _inspect_dispatch(
                None, None, self.store, self.daemon,
                self._fresh("plan1"), "a",
            )
        self.assertTrue(acted)
        self.assertEqual(called["multiline"], 0)
        self.assertEqual(self.store.get("plan1").status, ItemStatus.QUEUED)

    def test_plan_review_feedback_key_requeues_with_note(self):
        """`r` in plan review calls `reject_and_retry`: item goes back to
        QUEUED with feedback set, result_json cleared, attempts reset.
        Runner's `_prepend_feedback` consumes it on the next plan pass."""
        self._seed("plan1", ItemStatus.QUEUED)
        self.store.transition("plan1", ItemStatus.WORKING, note="t")
        self.store.transition(
            "plan1", ItemStatus.AWAITING_PLAN_REVIEW,
            result_json='{"phase": "plan", "plan": "old"}', note="t",
        )
        with patch(
            "agentor.dashboard.modes._prompt_multiline",
            return_value="avoid rewriting store.py",
        ):
            acted, msg = _inspect_dispatch(
                None, None, self.store, self.daemon,
                self._fresh("plan1"), "r",
            )
        self.assertTrue(acted)
        self.assertEqual(msg, "plan requeued with feedback")
        got = self.store.get("plan1")
        self.assertEqual(got.status, ItemStatus.QUEUED)
        self.assertEqual(got.feedback, "avoid rewriting store.py")
        self.assertIsNone(got.result_json)
        self.assertEqual(got.attempts, 0)

    def test_plan_review_feedback_cancelled_is_noop(self):
        """Empty feedback from the prompt (user aborted) leaves the item in
        AWAITING_PLAN_REVIEW — no state change, no flash."""
        self._seed("plan1", ItemStatus.QUEUED)
        self.store.transition("plan1", ItemStatus.WORKING, note="t")
        self.store.transition(
            "plan1", ItemStatus.AWAITING_PLAN_REVIEW, note="t",
        )
        with patch(
            "agentor.dashboard.modes._prompt_multiline", return_value="",
        ):
            acted, msg = _inspect_dispatch(
                None, None, self.store, self.daemon,
                self._fresh("plan1"), "r",
            )
        self.assertFalse(acted)
        self.assertEqual(msg, "")
        self.assertEqual(
            self.store.get("plan1").status, ItemStatus.AWAITING_PLAN_REVIEW,
        )

    def test_plan_review_f_key_is_unbound(self):
        """The former `[f]approve+feedback` action was removed — plan
        review now mirrors the execute-review split (approve / feedback /
        defer / delete). Pressing `f` must be a no-op."""
        self._seed("plan1", ItemStatus.QUEUED)
        self.store.transition("plan1", ItemStatus.WORKING, note="t")
        self.store.transition(
            "plan1", ItemStatus.AWAITING_PLAN_REVIEW, note="t",
        )
        acted, msg = _inspect_dispatch(
            None, None, self.store, self.daemon,
            self._fresh("plan1"), "f",
        )
        self.assertFalse(acted)
        self.assertEqual(msg, "")
        self.assertEqual(
            self.store.get("plan1").status, ItemStatus.AWAITING_PLAN_REVIEW,
        )

    def test_plan_review_defer_transitions_to_deferred(self):
        self._seed("plan1", ItemStatus.QUEUED)
        self.store.transition("plan1", ItemStatus.WORKING, note="t")
        self.store.transition(
            "plan1", ItemStatus.AWAITING_PLAN_REVIEW, note="t",
        )
        acted, _ = _inspect_dispatch(
            None, None, self.store, self.daemon,
            self._fresh("plan1"), "s",
        )
        self.assertTrue(acted)
        self.assertEqual(
            self.store.get("plan1").status, ItemStatus.DEFERRED,
        )

    def test_errored_retry_resets_to_queued(self):
        self._seed("err1", ItemStatus.QUEUED)
        self.store.transition("err1", ItemStatus.WORKING, note="t")
        self.store.transition(
            "err1", ItemStatus.ERRORED, last_error="boom",
            attempts=3, note="t",
        )
        acted, _ = _inspect_dispatch(
            None, None, self.store, self.daemon,
            self._fresh("err1"), "a",
        )
        self.assertTrue(acted)
        got = self.store.get("err1")
        self.assertEqual(got.status, ItemStatus.QUEUED)
        self.assertIsNone(got.last_error)
        self.assertEqual(got.attempts, 0)

    def test_errored_defer_transitions_to_deferred(self):
        self._seed("err1", ItemStatus.QUEUED)
        self.store.transition("err1", ItemStatus.WORKING, note="t")
        self.store.transition(
            "err1", ItemStatus.ERRORED, last_error="boom", note="t",
        )
        acted, _ = _inspect_dispatch(
            None, None, self.store, self.daemon,
            self._fresh("err1"), "s",
        )
        self.assertTrue(acted)
        self.assertEqual(
            self.store.get("err1").status, ItemStatus.DEFERRED,
        )

    def test_rejected_retry_resets_to_queued(self):
        self._seed("rej1", ItemStatus.QUEUED)
        self.store.transition("rej1", ItemStatus.WORKING, note="t")
        self.store.transition(
            "rej1", ItemStatus.AWAITING_REVIEW, note="t",
        )
        self.store.transition(
            "rej1", ItemStatus.REJECTED, feedback="no", note="t",
        )
        acted, _ = _inspect_dispatch(
            None, None, self.store, self.daemon,
            self._fresh("rej1"), "a",
        )
        self.assertTrue(acted)
        self.assertEqual(self.store.get("rej1").status, ItemStatus.QUEUED)

    def test_deferred_restore_returns_to_prior_status(self):
        self._seed("def1", ItemStatus.QUEUED)
        self.store.transition("def1", ItemStatus.WORKING, note="t")
        self.store.transition(
            "def1", ItemStatus.AWAITING_PLAN_REVIEW, note="t",
        )
        self.store.transition(
            "def1", ItemStatus.DEFERRED, note="t",
        )
        acted, _ = _inspect_dispatch(
            None, None, self.store, self.daemon,
            self._fresh("def1"), "a",
        )
        self.assertTrue(acted)
        self.assertEqual(
            self.store.get("def1").status,
            ItemStatus.AWAITING_PLAN_REVIEW,
        )
        self.assertEqual(self.daemon.filled, 1)

    def test_deferred_delete_confirmed_removes_item(self):
        self._seed("del1", ItemStatus.DEFERRED)
        with patch("agentor.dashboard.modes._prompt_yn", return_value=True):
            acted, msg = _inspect_dispatch(
                None, None, self.store, self.daemon,
                self._fresh("del1"), "x",
            )
        self.assertTrue(acted)
        self.assertEqual(msg, "deleted")
        self.assertIsNone(self.store.get("del1"))
        self.assertTrue(self.store.is_deleted("del1"))

    def test_deferred_delete_cancelled_leaves_item(self):
        self._seed("del2", ItemStatus.DEFERRED)
        with patch("agentor.dashboard.modes._prompt_yn", return_value=False):
            acted, msg = _inspect_dispatch(
                None, None, self.store, self.daemon,
                self._fresh("del2"), "x",
            )
        self.assertFalse(acted)
        self.assertEqual(msg, "")
        got = self.store.get("del2")
        self.assertIsNotNone(got)
        self.assertEqual(got.status, ItemStatus.DEFERRED)
        self.assertFalse(self.store.is_deleted("del2"))

    def test_terminal_status_ignores_non_delete_keys(self):
        """MERGED is view-only for every action key except the new unified
        `x` delete, which must still work at every status."""
        self._seed("done1", ItemStatus.QUEUED)
        self.store.transition("done1", ItemStatus.WORKING, note="t")
        self.store.transition(
            "done1", ItemStatus.AWAITING_REVIEW, note="t",
        )
        self.store.transition("done1", ItemStatus.MERGED, note="t")
        for key in ("a", "s", "r", "m", "e", "f", "v"):
            with self.subTest(key=key):
                acted, _ = _inspect_dispatch(
                    None, None, self.store, self.daemon,
                    self._fresh("done1"), key,
                )
                self.assertFalse(acted)
        # MERGED → hard-deleted + tombstoned via `x`. stdscr=None works
        # because `_prompt_yn` is patched to auto-confirm.
        with patch(
            "agentor.dashboard.modes._prompt_yn", return_value=True,
        ):
            acted, msg = _inspect_dispatch(
                None, None, self.store, self.daemon,
                self._fresh("done1"), "x",
            )
        self.assertTrue(acted)
        self.assertEqual(msg, "deleted")
        self.assertIsNone(self.store.get("done1"))
        self.assertTrue(self.store.is_deleted("done1"))

    def test_delete_tombstones_item_from_every_status(self):
        """`x` must hard-delete regardless of where the item started.
        Seed one item per status and drive the dispatcher with the
        confirmation prompt patched to auto-yes."""
        cases = {
            ItemStatus.QUEUED: lambda sid: None,
            ItemStatus.WORKING: lambda sid: self.store.transition(
                sid, ItemStatus.WORKING, note="t"),
            ItemStatus.AWAITING_PLAN_REVIEW: lambda sid: (
                self.store.transition(sid, ItemStatus.WORKING, note="t"),
                self.store.transition(
                    sid, ItemStatus.AWAITING_PLAN_REVIEW, note="t"),
            ),
            ItemStatus.AWAITING_REVIEW: lambda sid: (
                self.store.transition(sid, ItemStatus.WORKING, note="t"),
                self.store.transition(
                    sid, ItemStatus.AWAITING_REVIEW, note="t"),
            ),
            ItemStatus.CONFLICTED: lambda sid: (
                self.store.transition(sid, ItemStatus.WORKING, note="t"),
                self.store.transition(
                    sid, ItemStatus.AWAITING_REVIEW, note="t"),
                self.store.transition(
                    sid, ItemStatus.CONFLICTED, note="t"),
            ),
            ItemStatus.ERRORED: lambda sid: (
                self.store.transition(sid, ItemStatus.WORKING, note="t"),
                self.store.transition(
                    sid, ItemStatus.ERRORED, note="t"),
            ),
            ItemStatus.REJECTED: lambda sid: (
                self.store.transition(sid, ItemStatus.WORKING, note="t"),
                self.store.transition(
                    sid, ItemStatus.AWAITING_REVIEW, note="t"),
                self.store.transition(
                    sid, ItemStatus.REJECTED, note="t"),
            ),
            ItemStatus.DEFERRED: lambda sid: (
                self.store.transition(sid, ItemStatus.WORKING, note="t"),
                self.store.transition(
                    sid, ItemStatus.DEFERRED, note="t"),
            ),
            ItemStatus.APPROVED: lambda sid: self.store.transition(
                sid, ItemStatus.APPROVED, note="t"),
            ItemStatus.MERGED: lambda sid: (
                self.store.transition(sid, ItemStatus.WORKING, note="t"),
                self.store.transition(
                    sid, ItemStatus.AWAITING_REVIEW, note="t"),
                self.store.transition(
                    sid, ItemStatus.MERGED, note="t"),
            ),
        }
        for idx, (status, setup) in enumerate(cases.items()):
            with self.subTest(status=status):
                sid = f"del{idx}"
                self._seed(sid, ItemStatus.QUEUED)
                setup(sid)
                self.assertEqual(
                    self.store.get(sid).status, status,
                    f"setup left {sid} in wrong state",
                )
                # Shrink the WORKING-teardown poll budget so the subTest
                # that seeds WORKING doesn't burn 5s of real time waiting
                # for a runner thread that doesn't exist.
                with patch(
                    "agentor.dashboard.modes._prompt_yn",
                    return_value=True,
                ), patch(
                    "agentor.committer._DELETE_WAIT_SECONDS", 0.2,
                ):
                    acted, msg = _inspect_dispatch(
                        None, None, self.store, self.daemon,
                        self._fresh(sid), "x",
                    )
                self.assertTrue(acted)
                self.assertEqual(msg, "deleted")
                self.assertIsNone(self.store.get(sid))
                self.assertTrue(self.store.is_deleted(sid))

    def test_delete_already_tombstoned_is_noop(self):
        """Pressing `x` on an id that's already been tombstoned reports
        the no-op without raising — `delete_idea` short-circuits when the
        row is gone."""
        self._seed("can1", ItemStatus.QUEUED)
        stale = self._fresh("can1")
        self.store.delete_item("can1", note="pre-tombstoned")
        self.assertTrue(self.store.is_deleted("can1"))
        with patch(
            "agentor.dashboard.modes._prompt_yn", return_value=True,
        ):
            acted, msg = _inspect_dispatch(
                None, None, self.store, self.daemon, stale, "x",
            )
        self.assertTrue(acted)
        self.assertEqual(msg, "already deleted")
        self.assertIsNone(self.store.get("can1"))

    def test_delete_prompt_cancel_leaves_item_alone(self):
        """User answers no to the confirm prompt → no transition, no proc
        kill, no flash."""
        self._seed("keep1", ItemStatus.QUEUED)
        self.store.transition("keep1", ItemStatus.WORKING, note="t")
        with patch(
            "agentor.dashboard.modes._prompt_yn", return_value=False,
        ):
            acted, msg = _inspect_dispatch(
                None, None, self.store, self.daemon,
                self._fresh("keep1"), "x",
            )
        self.assertFalse(acted)
        self.assertEqual(msg, "")
        self.assertEqual(
            self.store.get("keep1").status, ItemStatus.WORKING,
        )
        self.assertEqual(self.daemon.proc_registry.killed, [])

    def test_delete_working_kills_subprocess_and_tombstones(self):
        """WORKING delete must (a) invoke `proc_registry.kill_one(item.id)`,
        (b) hard-delete the row, (c) record a tombstone. cfg=None skips
        git cleanup — exercised separately by the committer-level test."""
        self._seed("live1", ItemStatus.QUEUED)
        self.store.transition(
            "live1", ItemStatus.WORKING,
            worktree_path="/tmp/nope", branch="agent/live1",
            session_id="sess-abc", note="t",
        )
        with patch(
            "agentor.dashboard.modes._prompt_yn", return_value=True,
        ), patch(
            "agentor.committer._DELETE_WAIT_SECONDS", 0.2,
        ):
            acted, _ = _inspect_dispatch(
                None, None, self.store, self.daemon,
                self._fresh("live1"), "x",
            )
        self.assertTrue(acted)
        self.assertEqual(self.daemon.proc_registry.killed, ["live1"])
        self.assertIsNone(self.store.get("live1"))
        self.assertTrue(self.store.is_deleted("live1"))

    def _seed_conflicted(self, id: str) -> None:
        """CONFLICTED is reached via AWAITING_REVIEW → CONFLICTED, matching
        the real committer flow. Required so the tested dispatch sees the
        status gate the inspect view presents."""
        self._seed(id, ItemStatus.QUEUED)
        self.store.transition(id, ItemStatus.WORKING, note="t")
        self.store.transition(id, ItemStatus.AWAITING_REVIEW, note="t")
        self.store.transition(
            id, ItemStatus.CONFLICTED,
            worktree_path="/tmp/nope", branch=f"agent/{id}",
            note="conflict",
        )

    def test_conflicted_m_invokes_retry_merge(self):
        """`[m]` is the sole CONFLICTED action after the [e] collapse — it
        routes through `_run_with_progress` to `retry_merge`. The dispatch
        surfaces the (ok, msg) tuple message as the flash string."""
        self._seed_conflicted("cm1")

        called = {}

        def fake_retry_merge(cfg, store, item, *, progress=None):
            called["args"] = (cfg, store, item.id)
            return True, "merged deadbeef into main"

        def fake_progress(stdscr, title, work, hint=None):
            return work(lambda _m: None)

        with patch(
            "agentor.committer.retry_merge", side_effect=fake_retry_merge,
        ), patch(
            "agentor.dashboard.modes._run_with_progress",
            side_effect=fake_progress,
        ):
            acted, msg = _inspect_dispatch(
                None, None, self.store, self.daemon,
                self._fresh("cm1"), "m",
            )
        self.assertTrue(acted)
        self.assertEqual(msg, "merged deadbeef into main")
        self.assertEqual(called["args"][2], "cm1")

    def test_conflicted_e_key_is_no_op_after_collapse(self):
        """`[e]resubmit to agent` was collapsed out; pressing `e` at a
        CONFLICTED row must not transition the item or fire the committer
        resubmit entry-point."""
        self._seed_conflicted("ce1")
        import agentor.committer as _committer
        with patch.object(
            _committer, "resubmit_conflicted",
        ) as mock_resubmit:
            acted, msg = _inspect_dispatch(
                None, None, self.store, self.daemon,
                self._fresh("ce1"), "e",
            )
        self.assertFalse(acted)
        self.assertEqual(msg, "")
        mock_resubmit.assert_not_called()
        self.assertEqual(
            self.store.get("ce1").status, ItemStatus.CONFLICTED,
        )


class TestIsAutoResolveChain(unittest.TestCase):
    """The inspect detail view uses `_is_auto_resolve_chain` to decide
    whether to surface the marker line. Drive transitions directly so the
    helper is covered without a full merge harness."""

    def setUp(self) -> None:
        self.td = TemporaryDirectory()
        self.store = Store(Path(self.td.name) / "state.db")

    def tearDown(self) -> None:
        self.store.close()
        self.td.cleanup()

    def _seed(self, id: str) -> None:
        self.store.upsert_discovered(_mk(id))

    def _fresh(self, id: str):
        got = self.store.get(id)
        assert got is not None
        return got

    def test_fresh_queued_has_no_chain(self):
        self._seed("a1")
        self.assertFalse(_is_auto_resolve_chain(self.store, self._fresh("a1")))

    def test_auto_marker_detected_on_queued(self):
        self._seed("a1")
        self.store.transition("a1", ItemStatus.WORKING, note="t")
        self.store.transition("a1", ItemStatus.AWAITING_REVIEW, note="t")
        self.store.transition("a1", ItemStatus.CONFLICTED, note="conflict")
        self.store.transition(
            "a1", ItemStatus.QUEUED,
            note=f"{AUTO_RESOLVE_NOTE_PREFIX}: resubmitted from CONFLICTED",
        )
        self.assertTrue(_is_auto_resolve_chain(self.store, self._fresh("a1")))

    def test_manual_resubmit_has_no_marker(self):
        self._seed("a1")
        self.store.transition("a1", ItemStatus.WORKING, note="t")
        self.store.transition("a1", ItemStatus.AWAITING_REVIEW, note="t")
        self.store.transition("a1", ItemStatus.CONFLICTED, note="conflict")
        self.store.transition(
            "a1", ItemStatus.QUEUED,
            note="resubmitted from CONFLICTED — agent will resolve",
        )
        self.assertFalse(_is_auto_resolve_chain(self.store, self._fresh("a1")))

    def test_chain_still_true_on_conflict_bounce_back(self):
        """If the agent's resolve attempt fails and the merge re-enters
        CONFLICTED, the last CONFLICTED → QUEUED row still carries the
        marker — the indicator should remain visible."""
        self._seed("a1")
        self.store.transition("a1", ItemStatus.WORKING, note="t")
        self.store.transition("a1", ItemStatus.AWAITING_REVIEW, note="t")
        self.store.transition("a1", ItemStatus.CONFLICTED, note="conflict")
        self.store.transition(
            "a1", ItemStatus.QUEUED,
            note=f"{AUTO_RESOLVE_NOTE_PREFIX}: resubmitted",
        )
        self.store.transition("a1", ItemStatus.WORKING, note="retry")
        self.store.transition("a1", ItemStatus.AWAITING_REVIEW, note="t")
        self.store.transition("a1", ItemStatus.CONFLICTED, note="still")
        self.assertTrue(_is_auto_resolve_chain(self.store, self._fresh("a1")))


class _KeyFeedStdscr:
    """Minimal stdscr fake that feeds `_inspect_render` a scripted key
    sequence. Returns each queued keycode once, then `ord("q")` forever so
    the render loop always terminates even if the test forgets to append
    the quit key."""

    def __init__(self, keys, h: int = 40, w: int = 120):
        self._keys = list(keys)
        self._h = h
        self._w = w

    def getmaxyx(self):
        return (self._h, self._w)

    def getch(self):
        return self._keys.pop(0) if self._keys else ord("q")

    def timeout(self, _ms):
        pass

    def nodelay(self, _flag):
        pass

    def erase(self):
        pass

    def addnstr(self, *_a, **_kw):
        pass

    def refresh(self):
        pass


def _fake_cfg(project_root: Path) -> SimpleNamespace:
    """Minimal Config stub — `_build_detail_lines` reads
    `cfg.project_root` (transcript path), `cfg.agent.max_attempts`, and
    `cfg.agent.runner` (via `_session_activity` → `detect_provider`)."""
    return SimpleNamespace(
        project_root=project_root,
        agent=SimpleNamespace(max_attempts=3, runner="stub"),
    )


class TestInspectPriorityKeys(unittest.TestCase):
    """`_inspect_render` must accept priority bump keys (P/O and
    Shift+Up/Shift+Down) without closing the view or changing item status.
    The main-dashboard loop already handles these — the inspect view
    mirrors the bindings so the operator doesn't need to close the detail
    screen to prioritize."""

    def setUp(self) -> None:
        self.td = TemporaryDirectory()
        self.store = Store(Path(self.td.name) / "state.db")
        self.store.upsert_discovered(_mk("pri1"))
        self.cfg = _fake_cfg(Path(self.td.name))

    def tearDown(self) -> None:
        self.store.close()
        self.td.cleanup()

    def _drive(self, *keys):
        fresh = self.store.get("pri1")
        stdscr = _KeyFeedStdscr(list(keys) + [ord("q")])
        # `_flash` sleeps 1200ms via curses.napms — skip that in tests.
        # `_show_item_screen` touches curses.color_pair which needs initscr().
        with patch("agentor.dashboard.render.curses.napms", lambda *_a: None), \
                patch("agentor.dashboard.render.curses.color_pair",
                      return_value=0):
            _inspect_render(stdscr, self.cfg, self.store, fresh, None)

    def test_capital_P_bumps_priority_up(self):
        self._drive(ord("P"))
        self.assertEqual(self.store.get("pri1").priority, 1)

    def test_capital_O_clamps_priority_at_zero(self):
        self._drive(ord("O"))
        self.assertEqual(self.store.get("pri1").priority, 0)

    def test_shift_up_bumps_priority_up(self):
        self._drive(curses.KEY_SR)
        self.assertEqual(self.store.get("pri1").priority, 1)

    def test_shift_down_reduces_priority(self):
        self.store.bump_priority("pri1", 3)
        self._drive(curses.KEY_SF)
        self.assertEqual(self.store.get("pri1").priority, 2)

    def test_repeated_bumps_accumulate_without_status_change(self):
        status_before = self.store.get("pri1").status
        self._drive(ord("P"), ord("P"), ord("P"))
        got = self.store.get("pri1")
        self.assertEqual(got.priority, 3)
        self.assertEqual(got.status, status_before)


class TestInspectFooterPriorityHint(unittest.TestCase):
    """The inspect footer must advertise `[P/O]priority` regardless of
    whether the current status has any action keys, so the binding stays
    discoverable on view-only screens (WORKING, QUEUED, MERGED)."""

    def test_priority_hint_present_on_view_only_status(self):
        footer = _inspect_footer(ItemStatus.WORKING, cycle=False)
        self.assertIn("[P/O]priority", footer)

    def test_priority_hint_present_alongside_actions(self):
        footer = _inspect_footer(ItemStatus.AWAITING_PLAN_REVIEW, cycle=False)
        self.assertIn("[P/O]priority", footer)
        self.assertIn("[a]approve→execute", footer)


class TestAnswersScaffoldAndParse(unittest.TestCase):
    """Pure-function tests for the helpers that translate between the
    agent's question list and the operator's seeded / filled-in overlay
    buffer. Decoupled from curses so they run quickly and pin the
    format contract the approve flow depends on."""

    def test_scaffold_formats_sequentially(self):
        from agentor.dashboard.modes import _answers_scaffold
        out = _answers_scaffold([
            "Should we keep the legacy flag?",
            "Where does the lock file live?",
        ])
        self.assertIn("Q1: Should we keep the legacy flag?\nA1: ", out)
        self.assertIn("Q2: Where does the lock file live?\nA2: ", out)
        # Blank line separates pairs so the overlay renders a paragraph
        # gap between questions.
        self.assertIn("\n\nQ2:", out)

    def test_parse_answers_happy_path(self):
        from agentor.dashboard.modes import _parse_answers
        reply = (
            "Q1: Should we keep the legacy flag?\n"
            "A1: yes, for one release\n"
            "\n"
            "Q2: Where does the lock file live?\n"
            "A2: under .agentor/\n"
        )
        self.assertEqual(
            _parse_answers(reply, 2),
            ["yes, for one release", "under .agentor/"],
        )

    def test_parse_answers_multiline_body(self):
        """Continuation lines between A1 and Q2 belong to A1 so the
        reviewer can expand on a single answer across several rows."""
        from agentor.dashboard.modes import _parse_answers
        reply = (
            "Q1: Big question?\n"
            "A1: first line\n"
            "  second line\n"
            "  third line\n"
            "\n"
            "Q2: Small question?\n"
            "A2: short\n"
        )
        parsed = _parse_answers(reply, 2)
        self.assertEqual(
            parsed[0], "first line\n  second line\n  third line",
        )
        self.assertEqual(parsed[1], "short")

    def test_parse_answers_blank_entries_pad_to_length(self):
        """Reviewer skipped A2 entirely. Parser still returns `n` strings
        so downstream alignment with the questions list stays intact."""
        from agentor.dashboard.modes import _parse_answers
        reply = "Q1: first?\nA1: yes\n\nQ2: second?\n"
        self.assertEqual(_parse_answers(reply, 2), ["yes", ""])

    def test_parse_answers_handles_missing_prefix(self):
        """Operator typed freeform without Q/A markers — parser returns all
        blanks rather than crashing. The approve flow treats this as
        `(no answer)` for every question, which the runner renders as the
        "proceed with best judgment" fallback."""
        from agentor.dashboard.modes import _parse_answers
        self.assertEqual(
            _parse_answers("just rambling text\n", 2), ["", ""],
        )

    def test_scaffold_wraps_long_question(self):
        """Long questions must soft-wrap onto indented continuation lines
        so the overlay Textbox (which clips at inner_cols) can still show
        the full prompt. Continuation lines use a 4-space indent so the
        Q/A regex in `_parse_answers` does not false-match them."""
        from agentor.dashboard.modes import _answers_scaffold, _parse_answers
        long_q = (
            "Should we keep the legacy authentication flag wired up for "
            "downstream integrations that have not yet migrated to the "
            "replacement middleware, or is it safe to rip it out now?"
        )
        out = _answers_scaffold([long_q], width=40)
        lines = out.splitlines()
        # First line starts with Q1:, at least one continuation, then A1:.
        self.assertTrue(lines[0].startswith("Q1: "))
        self.assertGreaterEqual(len(lines), 3)
        cont_lines = [ln for ln in lines[1:] if not ln.startswith("A1:")]
        self.assertTrue(cont_lines, "expected wrapped continuation lines")
        for ln in cont_lines:
            self.assertTrue(
                ln.startswith("    "),
                f"continuation must be indented: {ln!r}",
            )
            self.assertLessEqual(len(ln), 40)
        # Continuation lines must not match the Q/A marker regex.
        import re
        marker = re.compile(r"^\s*[QA]\d+\s*:")
        for ln in cont_lines:
            self.assertIsNone(marker.match(ln))
        # Reply still round-trips through _parse_answers.
        reply = out + "some answer text"
        self.assertEqual(_parse_answers(reply, 1), ["some answer text"])

    def test_scaffold_short_questions_unchanged(self):
        """Short questions that already fit must render exactly as before,
        preserving the wire format pinned by test_scaffold_formats_*."""
        from agentor.dashboard.modes import _answers_scaffold
        out = _answers_scaffold(["short one?", "also short?"], width=80)
        self.assertIn("Q1: short one?\nA1: ", out)
        self.assertIn("Q2: also short?\nA2: ", out)


class TestBuildDetailLinesQuestionWrap(unittest.TestCase):
    """`_build_detail_lines` feeds the inspect view. Long plan-phase
    questions must wrap so the row-level truncation in `_show_item_screen`
    doesn't cut off the tail of the prompt."""

    def _make_item_with_questions(self, tmp: Path, questions: list[str]):
        import json
        from agentor.dashboard.modes import _build_detail_lines
        store = Store(tmp / "agentor.db")
        item = Item(
            id="plan-wrap",
            title="long plan questions",
            body="body",
            source_file="backlog.md",
            source_line=1,
            tags={},
        )
        store.upsert_discovered(item)
        store.transition("plan-wrap", ItemStatus.WORKING, note="t")
        store.transition(
            "plan-wrap", ItemStatus.AWAITING_PLAN_REVIEW,
            result_json=json.dumps({
                "phase": "plan",
                "plan": "plan body",
                "questions": questions,
            }),
            note="t",
        )
        stored = store.get("plan-wrap")
        cfg = SimpleNamespace(
            agent=SimpleNamespace(max_attempts=3),
            git=SimpleNamespace(base_branch="main"),
            project=SimpleNamespace(root=tmp),
            project_root=tmp,
        )
        width = 60
        lines = _build_detail_lines(cfg, store, stored, width=width)
        return lines, width

    def test_long_question_wraps_in_inspect_view(self):
        long_q = (
            "Should we keep the legacy authentication flag wired up for "
            "downstream integrations that have not yet migrated to the "
            "replacement middleware, or is it safe to rip it out now?"
        )
        with TemporaryDirectory() as tmp:
            lines, width = self._make_item_with_questions(
                Path(tmp), [long_q],
            )
        # Locate the open-questions section.
        header_idx = next(
            i for i, ln in enumerate(lines) if "open questions" in ln
        )
        q_lines: list[str] = []
        for ln in lines[header_idx + 1:]:
            if not ln or ln.startswith("──"):
                break
            q_lines.append(ln)
        # First line is "  1. <first chunk>", followed by ≥1 continuation
        # lines prefixed with 5 spaces so they align under the text.
        self.assertGreaterEqual(len(q_lines), 2)
        self.assertTrue(q_lines[0].startswith("  1. "))
        for ln in q_lines[1:]:
            self.assertTrue(
                ln.startswith("     "),
                f"continuation must be 5-space-indented: {ln!r}",
            )
        for ln in q_lines:
            self.assertLessEqual(len(ln), width)

    def test_short_question_renders_single_line(self):
        with TemporaryDirectory() as tmp:
            lines, _ = self._make_item_with_questions(
                Path(tmp), ["short?"],
            )
        header_idx = next(
            i for i, ln in enumerate(lines) if "open questions" in ln
        )
        self.assertEqual(lines[header_idx + 1], "  1. short?")


if __name__ == "__main__":
    unittest.main()
