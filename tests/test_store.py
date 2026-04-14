import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentor.models import Item, ItemStatus
from agentor.store import Store


def _mk_item(id: str = "abc123", title: str = "A thing", body: str = "body") -> Item:
    return Item(
        id=id, title=title, body=body,
        source_file="backlog.md", source_line=1,
        tags={"priority": "high"},
    )


class TestStore(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.store = Store(Path(self.td.name) / "state.db")

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_upsert_new_then_duplicate(self):
        item = _mk_item()
        self.assertTrue(self.store.upsert_discovered(item))
        self.assertFalse(self.store.upsert_discovered(item))
        stored = self.store.get(item.id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, ItemStatus.QUEUED)
        self.assertEqual(stored.tags, {"priority": "high"})

    def test_claim_next_queued_transitions_to_working(self):
        self.store.upsert_discovered(_mk_item(id="a"))
        self.store.upsert_discovered(_mk_item(id="b"))
        claimed = self.store.claim_next_queued("/wt/a", "agent/a")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, "a")  # oldest first
        self.assertEqual(claimed.status, ItemStatus.WORKING)
        self.assertEqual(claimed.worktree_path, "/wt/a")
        self.assertEqual(claimed.attempts, 1)
        # b still queued
        queued = self.store.list_by_status(ItemStatus.QUEUED)
        self.assertEqual([q.id for q in queued], ["b"])

    def test_claim_returns_none_when_empty(self):
        self.assertIsNone(self.store.claim_next_queued("/wt", "br"))

    def test_pool_cap(self):
        self.store.upsert_discovered(_mk_item(id="a"))
        self.store.upsert_discovered(_mk_item(id="b"))
        self.assertTrue(self.store.pool_has_slot(1))
        self.store.claim_next_queued("/wt/a", "agent/a")
        self.assertFalse(self.store.pool_has_slot(1))
        self.assertTrue(self.store.pool_has_slot(2))

    def test_transition_records_history(self):
        self.store.upsert_discovered(_mk_item(id="a"))
        self.store.claim_next_queued("/wt/a", "agent/a")
        self.store.transition("a", ItemStatus.AWAITING_REVIEW, note="build passed",
                              result_json='{"files": ["x.py"]}')
        stored = self.store.get("a")
        self.assertEqual(stored.status, ItemStatus.AWAITING_REVIEW)
        self.assertEqual(stored.result_json, '{"files": ["x.py"]}')
        history = self.store.transitions_for("a")
        statuses = [(t["from_status"], t["to_status"]) for t in history]
        self.assertEqual(statuses, [
            (None, "queued"),
            ("queued", "working"),
            ("working", "awaiting_review"),
        ])
        self.assertEqual(history[-1]["note"], "build passed")

    def test_transition_rejects_unknown_field(self):
        self.store.upsert_discovered(_mk_item(id="a"))
        with self.assertRaises(ValueError):
            self.store.transition("a", ItemStatus.WORKING, bogus="x")

    def test_transition_unknown_item(self):
        with self.assertRaises(KeyError):
            self.store.transition("nope", ItemStatus.WORKING)

    def test_persistence_across_reopen(self):
        item = _mk_item(id="persist")
        self.store.upsert_discovered(item)
        self.store.close()
        self.store = Store(Path(self.td.name) / "state.db")
        stored = self.store.get("persist")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.title, "A thing")


if __name__ == "__main__":
    unittest.main()
