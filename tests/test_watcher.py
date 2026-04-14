import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentor.config import (AgentConfig, Config, GitConfig, ParsingConfig,
                            ReviewConfig, SourcesConfig)
from agentor.models import ItemStatus
from agentor.store import Store
from agentor.watcher import resolve_watched_files, scan_once


def _mk_config(root: Path, watch: list[str], mode: str = "checkbox",
               exclude: list[str] | None = None) -> Config:
    return Config(
        project_name="t",
        project_root=root,
        sources=SourcesConfig(watch=watch, exclude=exclude or []),
        parsing=ParsingConfig(mode=mode),
        agent=AgentConfig(),
        git=GitConfig(),
        review=ReviewConfig(),
    )


class TestWatcher(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        self.store = Store(self.root / ".agentor" / "state.db")

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_glob_expansion(self):
        (self.root / "docs").mkdir()
        (self.root / "docs" / "backlog.md").write_text("- [ ] A\n")
        (self.root / "docs" / "ideas.md").write_text("- [ ] B\n")
        (self.root / "docs" / "readme.md").write_text("nothing\n")
        cfg = _mk_config(self.root, ["docs/backlog.md", "docs/ideas.md"])
        files = resolve_watched_files(cfg)
        self.assertEqual(len(files), 2)

    def test_glob_pattern(self):
        (self.root / "inbox").mkdir()
        (self.root / "inbox" / "a.md").write_text("- [ ] A\n")
        (self.root / "inbox" / "b.md").write_text("- [ ] B\n")
        cfg = _mk_config(self.root, ["inbox/*.md"])
        files = resolve_watched_files(cfg)
        self.assertEqual(len(files), 2)

    def test_scan_enqueues_new_items(self):
        (self.root / "backlog.md").write_text("- [ ] First\n- [ ] Second\n")
        cfg = _mk_config(self.root, ["backlog.md"])
        result = scan_once(cfg, self.store)
        self.assertEqual(result.scanned_files, 1)
        self.assertEqual(result.new_items, 2)
        self.assertEqual(len(self.store.list_by_status(ItemStatus.QUEUED)), 2)

    def test_rescan_idempotent(self):
        (self.root / "backlog.md").write_text("- [ ] First\n")
        cfg = _mk_config(self.root, ["backlog.md"])
        scan_once(cfg, self.store)
        result = scan_once(cfg, self.store)
        self.assertEqual(result.new_items, 0)
        self.assertEqual(len(self.store.list_by_status(ItemStatus.QUEUED)), 1)

    def test_new_item_appended_picked_up(self):
        f = self.root / "backlog.md"
        f.write_text("- [ ] First\n")
        cfg = _mk_config(self.root, ["backlog.md"])
        scan_once(cfg, self.store)
        f.write_text("- [ ] First\n- [ ] Second\n")
        result = scan_once(cfg, self.store)
        self.assertEqual(result.new_items, 1)

    def test_existing_status_preserved_on_rescan(self):
        (self.root / "backlog.md").write_text("- [ ] First\n")
        cfg = _mk_config(self.root, ["backlog.md"])
        scan_once(cfg, self.store)
        claimed = self.store.claim_next_queued("/wt", "br")
        self.assertIsNotNone(claimed)
        scan_once(cfg, self.store)
        still = self.store.get(claimed.id)
        self.assertEqual(still.status, ItemStatus.WORKING)


    def test_exclude_filters_readme(self):
        (self.root / "docs" / "backlog").mkdir(parents=True)
        (self.root / "docs" / "backlog" / "README.md").write_text(
            "---\ntitle: Readme\n---\nignored\n"
        )
        (self.root / "docs" / "backlog" / "bug.md").write_text(
            "---\ntitle: Real bug\n---\nbody\n"
        )
        cfg = _mk_config(
            self.root,
            watch=["docs/backlog/*.md"],
            mode="frontmatter",
            exclude=["**/README.md"],
        )
        files = resolve_watched_files(cfg)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "bug.md")

    def test_scan_frontmatter_dir(self):
        (self.root / "docs" / "backlog").mkdir(parents=True)
        (self.root / "docs" / "backlog" / "README.md").write_text(
            "---\ntitle: Readme\n---\nignored\n"
        )
        (self.root / "docs" / "backlog" / "a.md").write_text(
            "---\ntitle: A\nstate: available\n---\nbody\n"
        )
        (self.root / "docs" / "backlog" / "b.md").write_text(
            "---\ntitle: B\nstate: in_progress\n---\nbody\n"
        )
        cfg = _mk_config(
            self.root,
            watch=["docs/backlog/*.md"],
            mode="frontmatter",
            exclude=["**/README.md"],
        )
        result = scan_once(cfg, self.store)
        self.assertEqual(result.scanned_files, 2)  # A + B scanned, README excluded
        self.assertEqual(result.new_items, 1)  # only A (B skipped by state)


if __name__ == "__main__":
    unittest.main()
