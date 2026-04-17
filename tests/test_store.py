import sqlite3
import time
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

    def _upsert_and_queue(self, id: str) -> None:
        """upsert_discovered now lands items in BACKLOG (so humans can gate
        new work). Tests that exercise the downstream flow want items already
        in QUEUED, so promote them explicitly here."""
        self.store.upsert_discovered(_mk_item(id=id))
        self.store.transition(id, ItemStatus.QUEUED, note="test promote")

    def test_upsert_new_then_duplicate(self):
        item = _mk_item()
        self.assertTrue(self.store.upsert_discovered(item))
        self.assertFalse(self.store.upsert_discovered(item))
        stored = self.store.get(item.id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, ItemStatus.BACKLOG)
        self.assertEqual(stored.tags, {"priority": "high"})

    def test_claim_next_queued_transitions_to_working(self):
        self._upsert_and_queue("a")
        self._upsert_and_queue("b")
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
        self._upsert_and_queue("a")
        self._upsert_and_queue("b")
        self.assertTrue(self.store.pool_has_slot(1))
        self.store.claim_next_queued("/wt/a", "agent/a")
        self.assertFalse(self.store.pool_has_slot(1))
        self.assertTrue(self.store.pool_has_slot(2))

    def test_transition_records_history(self):
        self._upsert_and_queue("a")
        self.store.claim_next_queued("/wt/a", "agent/a")
        self.store.transition("a", ItemStatus.AWAITING_REVIEW, note="build passed",
                              result_json='{"files": ["x.py"]}')
        stored = self.store.get("a")
        self.assertEqual(stored.status, ItemStatus.AWAITING_REVIEW)
        self.assertEqual(stored.result_json, '{"files": ["x.py"]}')
        history = self.store.transitions_for("a")
        statuses = [(t.from_status, t.to_status) for t in history]
        self.assertEqual(statuses, [
            (None, ItemStatus.BACKLOG),
            (ItemStatus.BACKLOG, ItemStatus.QUEUED),
            (ItemStatus.QUEUED, ItemStatus.WORKING),
            (ItemStatus.WORKING, ItemStatus.AWAITING_REVIEW),
        ])
        self.assertEqual(history[-1].note, "build passed")

    def test_transition_rejects_unknown_field(self):
        self.store.upsert_discovered(_mk_item(id="a"))
        with self.assertRaises(ValueError):
            self.store.transition("a", ItemStatus.WORKING, bogus="x")

    def test_transition_unknown_item(self):
        with self.assertRaises(KeyError):
            self.store.transition("nope", ItemStatus.WORKING)

    def test_claim_respects_priority(self):
        # Seed older `a` (created first), then younger `b` with higher
        # priority. FIFO alone would pick `a`; priority must flip it.
        self._upsert_and_queue("a")
        # Force a measurable created_at gap so the FIFO tie-break is
        # deterministic if priority were ignored.
        time.sleep(0.01)
        self._upsert_and_queue("b")
        self.store.bump_priority("b", 1)
        claimed = self.store.claim_next_queued("/wt/b", "agent/b")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, "b")
        self.assertEqual(claimed.priority, 1)

    def test_claim_same_priority_fifo(self):
        self._upsert_and_queue("a")
        time.sleep(0.01)
        self._upsert_and_queue("b")
        self.store.bump_priority("a", 2)
        self.store.bump_priority("b", 2)
        claimed = self.store.claim_next_queued("/wt/x", "agent/x")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, "a")

    def test_bump_priority_clamps_at_zero(self):
        self._upsert_and_queue("a")
        self.assertEqual(self.store.bump_priority("a", -1), 0)
        self.assertEqual(self.store.bump_priority("a", -5), 0)
        stored = self.store.get("a")
        self.assertEqual(stored.priority, 0)
        # Bump up then back down confirms clamp is not a hard-wire to 0.
        self.assertEqual(self.store.bump_priority("a", 3), 3)
        self.assertEqual(self.store.bump_priority("a", -1), 2)

    def test_bump_priority_unknown_item_raises(self):
        with self.assertRaises(KeyError):
            self.store.bump_priority("nope", 1)

    def test_list_by_status_sorted_by_priority_desc(self):
        # Three queued items in insertion order a, b, c; bump c highest,
        # b middle. Expect c, b, a in the listing.
        self._upsert_and_queue("a")
        time.sleep(0.01)
        self._upsert_and_queue("b")
        time.sleep(0.01)
        self._upsert_and_queue("c")
        self.store.bump_priority("b", 1)
        self.store.bump_priority("c", 5)
        ids = [it.id for it in self.store.list_by_status(ItemStatus.QUEUED)]
        self.assertEqual(ids, ["c", "b", "a"])

    def test_persistence_across_reopen(self):
        item = _mk_item(id="persist")
        self.store.upsert_discovered(item)
        self.store.close()
        self.store = Store(Path(self.td.name) / "state.db")
        stored = self.store.get("persist")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.title, "A thing")


class TestStatusRoundTrip(unittest.TestCase):
    """Every ItemStatus value must survive a write-then-read through the store
    without drift. Catches any future enum-rename that forgets to migrate the
    serializer (encode/decode) — a `.value` that doesn't match the DB column
    would round-trip to a ValueError at read time."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.store = Store(Path(self.td.name) / "state.db")

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_every_status_round_trips_on_items(self):
        for status in ItemStatus:
            item_id = f"rt-{status.value}"
            self.store.upsert_discovered(_mk_item(id=item_id))
            self.store.transition(item_id, status, note=f"to {status.value}")
            stored = self.store.get(item_id)
            self.assertIsNotNone(stored, f"missing row for {status}")
            self.assertEqual(stored.status, status)
            self.assertIsInstance(stored.status, ItemStatus)
            listed = self.store.list_by_status(status)
            self.assertIn(item_id, [s.id for s in listed],
                          f"{status} not returned by list_by_status")
            self.assertEqual(self.store.count_by_status(status),
                             len(listed))

    def test_every_status_round_trips_on_transitions(self):
        item_id = "history"
        self.store.upsert_discovered(_mk_item(id=item_id))
        # Walk every status in declaration order. from_status on row N is the
        # to_status of row N-1, so both columns see every value that can ever
        # appear in them.
        for status in ItemStatus:
            if status == ItemStatus.BACKLOG:
                continue  # seeded by upsert_discovered
            self.store.transition(item_id, status, note=status.value)
        history = self.store.transitions_for(item_id)
        seen_to = {t.to_status for t in history}
        self.assertEqual(seen_to, set(ItemStatus))
        for t in history:
            self.assertIsInstance(t.to_status, ItemStatus)
            if t.from_status is not None:
                self.assertIsInstance(t.from_status, ItemStatus)


class TestPreviousSettledStatus(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.store = Store(Path(self.td.name) / "state.db")

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _seed(self, id: str = "i1") -> None:
        self.store.upsert_discovered(_mk_item(id=id))
        self.store.transition(id, ItemStatus.QUEUED, note="promote")

    def test_returns_queued_after_first_working(self):
        self._seed("i1")
        self.store.claim_next_queued("/wt", "br")
        # current = WORKING; previous settled = QUEUED
        self.assertEqual(
            self.store.previous_settled_status("i1"),
            ItemStatus.QUEUED,
        )

    def test_returns_awaiting_after_rejection_cascade(self):
        self._seed("i1")
        self.store.claim_next_queued("/wt", "br")
        self.store.transition("i1", ItemStatus.AWAITING_PLAN_REVIEW)
        self.store.transition("i1", ItemStatus.QUEUED, note="approved")
        # rejection cascade: queued→working→queued→working→rejected
        self.store.transition("i1", ItemStatus.WORKING)
        self.store.transition("i1", ItemStatus.QUEUED)
        self.store.transition("i1", ItemStatus.WORKING)
        self.store.transition("i1", ItemStatus.REJECTED, note="max_attempts")
        # Most recent settled state ≠ rejected ≠ working = QUEUED
        # (the "approved" QUEUED, hit on every cascade entry).
        self.assertEqual(
            self.store.previous_settled_status("i1"),
            ItemStatus.QUEUED,
        )

    def test_skips_working_returns_awaiting_plan(self):
        self._seed("i1")
        self.store.claim_next_queued("/wt", "br")
        self.store.transition("i1", ItemStatus.AWAITING_PLAN_REVIEW)
        # Now in awaiting_plan_review — previous settled = QUEUED
        self.assertEqual(
            self.store.previous_settled_status("i1"),
            ItemStatus.QUEUED,
        )

    def test_returns_none_with_only_initial_transition(self):
        self.store.upsert_discovered(_mk_item(id="i1"))
        # Only one transition exists (None→backlog). No prior settled state.
        self.assertIsNone(self.store.previous_settled_status("i1"))

    def test_returns_none_for_unknown_item(self):
        self.assertIsNone(self.store.previous_settled_status("nope"))


class TestNoteInfraFailure(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.store = Store(Path(self.td.name) / "state.db")
        self.store.upsert_discovered(_mk_item(id="x"))
        self.store.transition("x", ItemStatus.QUEUED)
        self.claimed = self.store.claim_next_queued("/wt", "br")  # attempts=1

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_keeps_status_and_refunds_attempt(self):
        self.assertEqual(self.claimed.attempts, 1)
        self.store.note_infra_failure("x", "fatal: not a git repository")
        item = self.store.get("x")
        self.assertEqual(item.status, ItemStatus.WORKING)  # unchanged
        self.assertEqual(item.attempts, 0)  # refunded
        self.assertEqual(item.last_error, "fatal: not a git repository")

    def test_records_self_loop_transition(self):
        self.store.note_infra_failure("x", "fatal: bad object")
        history = self.store.transitions_for("x")
        last = history[-1]
        # from==to (status didn't change), note tagged.
        self.assertEqual(last.from_status, ItemStatus.WORKING)
        self.assertEqual(last.to_status, ItemStatus.WORKING)
        self.assertIn("infra failure", last.note)

    def test_attempts_clamped_at_zero(self):
        self.store.note_infra_failure("x", "err")
        self.store.note_infra_failure("x", "err")  # would go negative
        self.assertEqual(self.store.get("x").attempts, 0)


class TestRecentFailureNotes(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.store = Store(Path(self.td.name) / "state.db")
        self.store.upsert_discovered(_mk_item(id="x"))
        self.store.transition("x", ItemStatus.QUEUED)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_returns_recent_working_to_queued_notes_newest_first(self):
        for i in range(3):
            self.store.claim_next_queued("/wt", "br")
            self.store.transition("x", ItemStatus.QUEUED, note=f"fail {i}")
        notes = self.store.recent_failure_notes("x", n=3)
        self.assertEqual(notes, ["fail 2", "fail 1", "fail 0"])

    def test_filters_to_working_to_queued(self):
        self.store.claim_next_queued("/wt", "br")
        self.store.transition("x", ItemStatus.AWAITING_REVIEW, note="ignored")
        self.store.transition("x", ItemStatus.QUEUED, note="should not match")
        # only working→queued transitions count
        self.assertEqual(self.store.recent_failure_notes("x", n=5), [])

    def test_respects_limit(self):
        for i in range(5):
            self.store.claim_next_queued("/wt", "br")
            self.store.transition("x", ItemStatus.QUEUED, note=str(i))
        self.assertEqual(len(self.store.recent_failure_notes("x", n=2)), 2)


class TestFailures(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.store = Store(Path(self.td.name) / "state.db")
        self.store.upsert_discovered(_mk_item(id="x"))
        self.store.transition("x", ItemStatus.QUEUED)
        self.store.claim_next_queued("/wt", "br")

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_record_and_list_newest_first(self):
        self.store.record_failure(
            "x", attempt=1, phase="plan", error="boom one",
            error_sig="boom", num_turns=5, duration_ms=1234,
            files_changed=["a.py"],
            transcript_path="/tmp/x.plan.log",
        )
        self.store.record_failure(
            "x", attempt=2, phase="execute", error="boom two",
            error_sig="boom",
        )
        rows = self.store.list_failures("x")
        self.assertEqual(len(rows), 2)
        # newest first
        self.assertEqual(rows[0]["error"], "boom two")
        self.assertEqual(rows[0]["phase"], "execute")
        self.assertEqual(rows[1]["num_turns"], 5)
        self.assertEqual(rows[1]["files_changed_json"], '["a.py"]')
        self.assertEqual(self.store.count_failures("x"), 2)

    def test_count_returns_zero_when_none(self):
        self.assertEqual(self.store.count_failures("x"), 0)
        self.assertEqual(self.store.list_failures("x"), [])


class TestIdsWithErrors(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.store = Store(Path(self.td.name) / "state.db")

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _mkq(self, id: str, err: str | None) -> None:
        self.store.upsert_discovered(_mk_item(id=id))
        self.store.transition(id, ItemStatus.QUEUED, last_error=err)

    def test_returns_only_items_with_last_error(self):
        self._mkq("a", "err!")
        self._mkq("b", None)
        self._mkq("c", "bad")
        got = self.store.ids_with_errors()
        self.assertEqual(got, {"a", "c"})

    def test_status_filter(self):
        self._mkq("a", "err!")
        self._mkq("b", "err!")
        # move b to awaiting_review
        self.store.transition("b", ItemStatus.AWAITING_REVIEW,
                              last_error="err!")
        got = self.store.ids_with_errors([ItemStatus.QUEUED])
        self.assertEqual(got, {"a"})


class TestMigratePriority(unittest.TestCase):
    """Opening a DB that predates the `priority` column must heal the schema
    via `_migrate`. Simulates an older install by dropping the column on a
    sidecar connection between open cycles."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.db_path = Path(self.td.name) / "state.db"

    def tearDown(self):
        self.td.cleanup()

    def test_missing_priority_column_heals_on_open(self):
        store = Store(self.db_path)
        store.upsert_discovered(_mk_item(id="pre"))
        store.close()

        # Sidecar conn: sqlite can't DROP COLUMN without full-rebuild gymnastics,
        # so rebuild the table without the priority column to emulate the
        # pre-migration schema.
        raw = sqlite3.connect(self.db_path)
        raw.row_factory = sqlite3.Row
        cols = [r["name"] for r in raw.execute("PRAGMA table_info(items)")]
        self.assertIn("priority", cols)
        raw.executescript("""
            BEGIN;
            CREATE TABLE items_new (
                id            TEXT PRIMARY KEY,
                title         TEXT NOT NULL,
                body          TEXT NOT NULL,
                source_file   TEXT NOT NULL,
                source_line   INTEGER NOT NULL,
                tags_json     TEXT NOT NULL DEFAULT '{}',
                status        TEXT NOT NULL,
                worktree_path TEXT,
                branch        TEXT,
                attempts      INTEGER NOT NULL DEFAULT 0,
                last_error    TEXT,
                feedback      TEXT,
                result_json   TEXT,
                session_id    TEXT,
                agentor_version TEXT,
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL
            );
            INSERT INTO items_new
                SELECT id, title, body, source_file, source_line, tags_json,
                       status, worktree_path, branch, attempts, last_error,
                       feedback, result_json, session_id, agentor_version,
                       created_at, updated_at FROM items;
            DROP TABLE items;
            ALTER TABLE items_new RENAME TO items;
            COMMIT;
        """)
        cols = [r["name"] for r in raw.execute("PRAGMA table_info(items)")]
        self.assertNotIn("priority", cols)
        raw.close()

        # Reopening should run _migrate and re-add priority with default 0.
        store = Store(self.db_path)
        try:
            stored = store.get("pre")
            self.assertIsNotNone(stored)
            self.assertEqual(stored.priority, 0)
            # Downstream queries that order by priority must still work.
            self.assertEqual(
                [it.id for it in store.list_by_status(ItemStatus.BACKLOG)],
                ["pre"],
            )
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
