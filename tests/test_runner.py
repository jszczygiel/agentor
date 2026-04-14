import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentor.committer import approve_and_commit, reject, retry
from agentor.config import (AgentConfig, Config, GitConfig, ParsingConfig,
                            ReviewConfig, SourcesConfig)
from agentor.models import ItemStatus
from agentor.recovery import recover_on_startup
from agentor.runner import StubRunner, plan_worktree
from agentor.store import Store
from agentor.watcher import scan_once


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_project(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@test")
    _git(root, "config", "user.name", "test")
    (root / "README.md").write_text("# project\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")


def _mk_config(root: Path, mode: str = "checkbox",
               watch: list[str] | None = None,
               mark_done: bool = True) -> Config:
    return Config(
        project_name=root.name,
        project_root=root,
        sources=SourcesConfig(
            watch=watch or ["backlog.md"], exclude=[], mark_done=mark_done,
        ),
        parsing=ParsingConfig(mode=mode),
        agent=AgentConfig(pool_size=1),
        git=GitConfig(base_branch="main", branch_prefix="agent/"),
        review=ReviewConfig(),
    )


class TestStubRunner(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] Fix a bug\n  details\n")
        self.cfg = _mk_config(self.root)
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _claim_first(self):
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        return self.store.claim_next_queued(str(wt), br)

    def test_stub_runner_transitions_to_awaiting_review(self):
        claimed = self._claim_first()
        result = StubRunner(self.cfg, self.store).run(claimed)
        self.assertIsNone(result.error)
        self.assertTrue(result.worktree_path.exists())
        self.assertTrue(result.diff)  # untracked file should show
        refreshed = self.store.get(claimed.id)
        self.assertEqual(refreshed.status, ItemStatus.AWAITING_REVIEW)
        self.assertIsNotNone(refreshed.result_json)
        data = json.loads(refreshed.result_json)
        self.assertIn("summary", data)

    def test_approve_commits_and_removes_worktree(self):
        claimed = self._claim_first()
        StubRunner(self.cfg, self.store).run(claimed)
        item = self.store.get(claimed.id)
        wt = Path(item.worktree_path)
        self.assertTrue(wt.exists())
        sha = approve_and_commit(self.cfg, self.store, item, "test commit")
        self.assertTrue(sha)
        self.assertFalse(wt.exists())
        final = self.store.get(claimed.id)
        self.assertEqual(final.status, ItemStatus.MERGED)

    def test_reject_keeps_worktree_and_marks_rejected(self):
        claimed = self._claim_first()
        StubRunner(self.cfg, self.store).run(claimed)
        item = self.store.get(claimed.id)
        wt = Path(item.worktree_path)
        reject(self.store, item, "not what I wanted")
        self.assertEqual(self.store.get(claimed.id).status, ItemStatus.REJECTED)
        self.assertTrue(wt.exists())  # worktree preserved for retry

    def test_retry_requeues(self):
        claimed = self._claim_first()
        StubRunner(self.cfg, self.store).run(claimed)
        item = self.store.get(claimed.id)
        reject(self.store, item, "fb")
        retry(self.store, self.store.get(claimed.id))
        self.assertEqual(self.store.get(claimed.id).status, ItemStatus.QUEUED)


class TestFrontmatterMarkDone(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "backlog").mkdir()
        self.src = self.root / "docs" / "backlog" / "bug-a.md"
        self.src.write_text(
            "---\ntitle: Bug A\nstate: available\n---\nDescription.\n"
        )
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-q", "-m", "add bug")
        self.cfg = _mk_config(
            self.root, mode="frontmatter",
            watch=["docs/backlog/*.md"], mark_done=True,
        )
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_approve_deletes_source_file_in_frontmatter_mode(self):
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        StubRunner(self.cfg, self.store).run(claimed)
        item = self.store.get(claimed.id)
        approve_and_commit(self.cfg, self.store, item, "fix bug A")
        # source file still exists on main (we only removed in worktree, then committed there)
        self.assertTrue(self.src.exists())
        # but on the agent branch, the file is gone
        cp = subprocess.run(
            ["git", "show", f"{item.branch}:docs/backlog/bug-a.md"],
            cwd=self.root, capture_output=True, text=True,
        )
        self.assertNotEqual(cp.returncode, 0)


class TestRecovery(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] do X\n")
        self.cfg = _mk_config(self.root)
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_recovery_requeues_stuck_working(self):
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        # simulate crash: item is WORKING but no worktree created
        self.assertEqual(claimed.status, ItemStatus.WORKING)
        recovered = recover_on_startup(self.cfg, self.store)
        self.assertEqual(recovered, [claimed.id])
        self.assertEqual(self.store.get(claimed.id).status, ItemStatus.QUEUED)
        self.assertIsNone(self.store.get(claimed.id).worktree_path)


if __name__ == "__main__":
    unittest.main()
