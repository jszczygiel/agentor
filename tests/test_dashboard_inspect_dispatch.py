"""Exercise `_inspect_dispatch` end-to-end against a real Store. These
tests cover the action keys that don't open a curses prompt so stdscr
can be passed as None — they pin the state-transition contract the
unified inspect view offers."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentor.dashboard.modes import _inspect_dispatch
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


if __name__ == "__main__":
    unittest.main()
