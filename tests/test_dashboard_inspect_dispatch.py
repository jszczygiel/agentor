"""Exercise `_inspect_dispatch` end-to-end against a real Store. These
tests cover the action keys that don't open a curses prompt so stdscr
can be passed as None — they pin the state-transition contract the
unified inspect view offers."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentor.committer import AUTO_RESOLVE_NOTE_PREFIX
from agentor.dashboard.modes import _inspect_dispatch, _is_auto_resolve_chain
from agentor.models import Item, ItemStatus
from agentor.store import Store


class _FakeDaemon:
    """Minimal daemon stub — `_inspect_dispatch` only needs
    `try_fill_pool` to exist."""

    def __init__(self) -> None:
        self.filled = 0

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

    def test_backlog_approve_moves_to_queued(self):
        # Legacy BACKLOG row — the unified view must still accept approval.
        self.store.upsert_discovered(_mk("bl1"))
        # Force DB status to legacy BACKLOG via a raw transition.
        self.store.transition("bl1", ItemStatus.BACKLOG, note="legacy seed")
        acted, _ = _inspect_dispatch(
            None, None, self.store, self.daemon,
            self._fresh("bl1"), "a",
        )
        self.assertTrue(acted)
        self.assertEqual(self.store.get("bl1").status, ItemStatus.QUEUED)

    def test_terminal_status_ignores_all_keys(self):
        self._seed("done1", ItemStatus.QUEUED)
        self.store.transition("done1", ItemStatus.WORKING, note="t")
        self.store.transition(
            "done1", ItemStatus.AWAITING_REVIEW, note="t",
        )
        self.store.transition("done1", ItemStatus.MERGED, note="t")
        for key in ("a", "s", "r", "m", "e", "x", "f", "v"):
            with self.subTest(key=key):
                acted, _ = _inspect_dispatch(
                    None, None, self.store, self.daemon,
                    self._fresh("done1"), key,
                )
                self.assertFalse(acted)


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


if __name__ == "__main__":
    unittest.main()
