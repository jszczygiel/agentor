import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentor.dashboard.modes import _next_review_item
from agentor.models import Item, ItemStatus
from agentor.store import Store


def _mk(id: str, title: str = "t") -> Item:
    return Item(
        id=id, title=title, body="b",
        source_file="backlog.md", source_line=1, tags={},
    )


class TestNextReviewItem(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.store = Store(Path(self.td.name) / "state.db")

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _seed(self, id: str, status: ItemStatus) -> None:
        self.store.upsert_discovered(_mk(id))
        self.store.transition(id, status, note="test seed")

    def test_empty_returns_none(self):
        self.assertIsNone(_next_review_item(self.store, set()))

    def test_plan_returned_before_code(self):
        self._seed("code1", ItemStatus.QUEUED)
        self.store.transition("code1", ItemStatus.WORKING, note="t")
        self.store.transition("code1", ItemStatus.AWAITING_REVIEW, note="t")
        self._seed("plan1", ItemStatus.QUEUED)
        self.store.transition("plan1", ItemStatus.WORKING, note="t")
        self.store.transition(
            "plan1", ItemStatus.AWAITING_PLAN_REVIEW, note="t"
        )
        nxt = _next_review_item(self.store, set())
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt.id, "plan1")

    def test_seen_ids_skipped(self):
        # Two code-review items; seen set should exclude the first.
        for tid in ("a", "b"):
            self._seed(tid, ItemStatus.QUEUED)
            self.store.transition(tid, ItemStatus.WORKING, note="t")
            self.store.transition(
                tid, ItemStatus.AWAITING_REVIEW, note="t"
            )
        # list_by_status orders by priority DESC, created_at — a was inserted
        # first so it comes first.
        first = _next_review_item(self.store, set())
        self.assertEqual(first.id, "a")
        second = _next_review_item(self.store, {"a"})
        self.assertEqual(second.id, "b")
        self.assertIsNone(_next_review_item(self.store, {"a", "b"}))

    def test_newly_awaiting_picked_up(self):
        self._seed("first", ItemStatus.QUEUED)
        self.store.transition("first", ItemStatus.WORKING, note="t")
        self.store.transition(
            "first", ItemStatus.AWAITING_REVIEW, note="t"
        )
        # Simulate prior visit.
        seen = {"first"}
        self.assertIsNone(_next_review_item(self.store, seen))
        # A second item transitions into AWAITING_REVIEW mid-cycle.
        self._seed("second", ItemStatus.QUEUED)
        self.store.transition("second", ItemStatus.WORKING, note="t")
        self.store.transition(
            "second", ItemStatus.AWAITING_REVIEW, note="t"
        )
        nxt = _next_review_item(self.store, seen)
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt.id, "second")

    def test_items_in_other_statuses_ignored(self):
        # Merged/errored/queued items must not leak into the cycle.
        self._seed("merged", ItemStatus.QUEUED)
        self.store.transition("merged", ItemStatus.WORKING, note="t")
        self.store.transition(
            "merged", ItemStatus.AWAITING_REVIEW, note="t"
        )
        self.store.transition("merged", ItemStatus.MERGED, note="t")
        self._seed("queued", ItemStatus.QUEUED)
        self.assertIsNone(_next_review_item(self.store, set()))


if __name__ == "__main__":
    unittest.main()
