import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from agentor.config import (AgentConfig, Config, GitConfig, ParsingConfig,
                            ReviewConfig, SourcesConfig)
from agentor.fold import maybe_enqueue_fold_item
from agentor.models import Item, ItemStatus
from agentor.store import Store
from agentor.watcher import scan_once


def _mk_config(root: Path, fold_threshold: int = 10) -> Config:
    return Config(
        project_name="t",
        project_root=root,
        sources=SourcesConfig(),
        parsing=ParsingConfig(),
        agent=AgentConfig(fold_threshold=fold_threshold),
        git=GitConfig(),
        review=ReviewConfig(),
    )


def _seed_logs(root: Path, n: int) -> None:
    d = root / "docs" / "agent-logs"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"2026-04-{i:02d}-note.md").write_text(f"# note {i}\n")


def _fold_item(store: Store, title: str, status: ItemStatus) -> None:
    """Seed a fake Fold item directly into the store at the target status
    so the double-queue guard has something to find."""
    item = Item(
        id=f"f-{status.value}",
        title=title,
        body="b",
        source_file="docs/backlog/fold-prev.md",
        source_line=1,
        tags={},
    )
    store.upsert_discovered(item)
    if status != ItemStatus.QUEUED:
        store.transition(item.id, status)


class TestFoldThreshold(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        self.store = Store(self.root / ".agentor" / "state.db")

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_below_threshold_no_file(self):
        _seed_logs(self.root, 9)
        cfg = _mk_config(self.root, fold_threshold=10)
        result = maybe_enqueue_fold_item(cfg, self.store)
        self.assertIsNone(result)
        self.assertFalse((self.root / "docs" / "backlog").exists())

    def test_threshold_zero_disabled(self):
        _seed_logs(self.root, 50)
        cfg = _mk_config(self.root, fold_threshold=0)
        self.assertIsNone(maybe_enqueue_fold_item(cfg, self.store))
        self.assertFalse((self.root / "docs" / "backlog").exists())

    def test_missing_logs_dir(self):
        cfg = _mk_config(self.root, fold_threshold=1)
        self.assertIsNone(maybe_enqueue_fold_item(cfg, self.store))

    def test_creates_backlog_file_at_threshold(self):
        _seed_logs(self.root, 10)
        cfg = _mk_config(self.root, fold_threshold=10)
        result = maybe_enqueue_fold_item(cfg, self.store)
        self.assertIsNotNone(result)
        today = date.today().isoformat()
        expected = (
            self.root / "docs" / "backlog"
            / f"fold-agent-lessons-{today}.md"
        )
        self.assertEqual(result, expected)
        self.assertTrue(expected.exists())
        text = expected.read_text()
        self.assertIn(f"title: Fold agent log lessons ({today})", text)
        self.assertIn("category: meta", text)
        self.assertIn("state: available", text)
        # Every seeded log path should be listed in the body.
        for i in range(10):
            self.assertIn(f"docs/agent-logs/2026-04-{i:02d}-note.md", text)
        # Expected-output block mentions deletion + no auto-merge.
        self.assertIn("git rm", text)
        self.assertIn("do not", text.lower())

    def test_skips_when_existing_non_terminal_item(self):
        for st in (
            ItemStatus.QUEUED, ItemStatus.WORKING,
            ItemStatus.AWAITING_PLAN_REVIEW, ItemStatus.AWAITING_REVIEW,
            ItemStatus.APPROVED, ItemStatus.CONFLICTED,
            ItemStatus.DEFERRED,
        ):
            # Fresh DB per status so prior seeded items don't stack up.
            with TemporaryDirectory() as td:
                inner = Path(td)
                _seed_logs(inner, 10)
                store = Store(inner / ".agentor" / "state.db")
                try:
                    _fold_item(store, "Fold agent log lessons (2026-01-01)", st)
                    c = _mk_config(inner, fold_threshold=10)
                    self.assertIsNone(
                        maybe_enqueue_fold_item(c, store),
                        f"should skip when prior item at {st.value}",
                    )
                    self.assertFalse(
                        (inner / "docs" / "backlog").exists(),
                        f"no backlog file for status {st.value}",
                    )
                finally:
                    store.close()

    def test_proceeds_when_previous_fold_terminal(self):
        for st in (
            ItemStatus.MERGED, ItemStatus.REJECTED,
            ItemStatus.CANCELLED, ItemStatus.ERRORED,
        ):
            with TemporaryDirectory() as td:
                inner = Path(td)
                _seed_logs(inner, 10)
                store = Store(inner / ".agentor" / "state.db")
                try:
                    _fold_item(store, "Fold agent log lessons (old)", st)
                    c = _mk_config(inner, fold_threshold=10)
                    result = maybe_enqueue_fold_item(c, store)
                    self.assertIsNotNone(
                        result,
                        f"should create when prior is {st.value}",
                    )
                    self.assertTrue(result.exists())
                finally:
                    store.close()

    def test_idempotent_same_day(self):
        _seed_logs(self.root, 10)
        cfg = _mk_config(self.root, fold_threshold=10)
        first = maybe_enqueue_fold_item(cfg, self.store)
        self.assertIsNotNone(first)
        original = first.read_text()
        # Second call: same-day file already exists; the helper MUST NOT
        # rewrite it (no in-flight edits) and MUST NOT raise.
        second = maybe_enqueue_fold_item(cfg, self.store)
        self.assertEqual(second, first)
        self.assertEqual(first.read_text(), original)
        # Exactly one file in the backlog dir.
        backlog_files = list((self.root / "docs" / "backlog").glob("*.md"))
        self.assertEqual(len(backlog_files), 1)

    def test_created_file_parsed_by_scan_once(self):
        """End-to-end: fold helper writes the backlog file, watcher.scan_once
        picks it up as a normal QUEUED item with the expected title, and the
        guard then blocks a second helper call on the next tick."""
        _seed_logs(self.root, 10)
        cfg = Config(
            project_name="t",
            project_root=self.root,
            sources=SourcesConfig(watch=["docs/backlog/*.md"]),
            parsing=ParsingConfig(mode="frontmatter"),
            agent=AgentConfig(fold_threshold=10),
            git=GitConfig(),
            review=ReviewConfig(),
        )
        created = maybe_enqueue_fold_item(cfg, self.store)
        self.assertIsNotNone(created)
        result = scan_once(cfg, self.store)
        self.assertEqual(result.new_items, 1)
        queued = self.store.list_by_status(ItemStatus.QUEUED)
        self.assertEqual(len(queued), 1)
        today = date.today().isoformat()
        self.assertEqual(
            queued[0].title, f"Fold agent log lessons ({today})",
        )
        self.assertEqual(queued[0].tags.get("category"), "meta")
        # Now the guard sees a QUEUED fold item; helper must no-op and
        # return None (no action taken this tick).
        self.assertIsNone(maybe_enqueue_fold_item(cfg, self.store))
        self.assertEqual(
            len(self.store.list_by_status(ItemStatus.QUEUED)), 1,
        )

    def test_guard_matches_title_prefix_only(self):
        """The guard scans by title prefix — an unrelated non-terminal
        item titled e.g. "Refactor fold helper" must NOT block creation."""
        _seed_logs(self.root, 10)
        _fold_item(self.store, "Refactor fold helper", ItemStatus.QUEUED)
        cfg = _mk_config(self.root, fold_threshold=10)
        result = maybe_enqueue_fold_item(cfg, self.store)
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
