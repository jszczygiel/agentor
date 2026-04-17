import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentor.committer import approve_and_commit, retry_merge
from agentor.config import (AgentConfig, Config, GitConfig, ParsingConfig,
                            ReviewConfig, SourcesConfig)
from agentor.models import ItemStatus
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


def _mk_config(
    root: Path, *, auto_merge: bool, merge_mode: str = "merge",
) -> Config:
    return Config(
        project_name=root.name,
        project_root=root,
        sources=SourcesConfig(watch=["backlog.md"], exclude=[]),
        parsing=ParsingConfig(mode="checkbox"),
        agent=AgentConfig(pool_size=1),
        git=GitConfig(base_branch="main", branch_prefix="agent/",
                      auto_merge=auto_merge, merge_mode=merge_mode),
        review=ReviewConfig(),
    )


def _branch_exists(repo: Path, branch: str) -> bool:
    cp = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        cwd=repo, capture_output=True, text=True,
    )
    return cp.returncode == 0


def _main_sha(repo: Path) -> str:
    cp = subprocess.run(
        ["git", "rev-parse", "main"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return cp.stdout.strip()


class TestAutoMerge(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text(
            "- [ ] Touch a file\n  details\n"
        )
        self.cfg = _mk_config(self.root, auto_merge=True)
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _claim_and_stub(self):
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        StubRunner(self.cfg, self.store).run(claimed)
        return self.store.get(claimed.id)

    def test_clean_merge_advances_base_and_deletes_feature_branch(self):
        item = self._claim_and_stub()
        branch = item.branch
        wt = Path(item.worktree_path)
        base_before = _main_sha(self.root)

        sha = approve_and_commit(self.cfg, self.store, item, "stub note")
        self.assertTrue(sha)

        final = self.store.get(item.id)
        self.assertEqual(final.status, ItemStatus.MERGED)
        self.assertFalse(wt.exists(), "feature worktree should be removed")
        self.assertFalse(_branch_exists(self.root, branch),
                         "feature branch should be deleted on clean merge")
        self.assertNotEqual(_main_sha(self.root), base_before,
                            "main should advance to the merge commit")
        ls = subprocess.run(
            ["git", "ls-tree", "-r", "main", "--name-only"],
            cwd=self.root, capture_output=True, text=True, check=True,
        )
        self.assertIn(".agentor-note-", ls.stdout,
                      "stub note should be reachable from main")

    def test_conflict_keeps_feature_and_marks_conflicted(self):
        item = self._claim_and_stub()
        wt = Path(item.worktree_path)

        # Conflicting edit on the feature worktree…
        (wt / "README.md").write_text("# project\n\nFEATURE LINE\n")
        _git(wt, "add", "README.md")
        _git(wt, "commit", "-q", "-m", "feat readme")
        # …and on main, so the merge has something to clash with.
        (self.root / "README.md").write_text("# project\n\nMAIN LINE\n")
        _git(self.root, "add", "README.md")
        _git(self.root, "commit", "-q", "-m", "main readme")
        base_before = _main_sha(self.root)

        approve_and_commit(self.cfg, self.store, item, "stub commit")

        final = self.store.get(item.id)
        self.assertEqual(final.status, ItemStatus.CONFLICTED)
        self.assertTrue(wt.exists(), "worktree must be kept for resolution")
        self.assertTrue(_branch_exists(self.root, item.branch),
                        "feature branch must be kept for resolution")
        self.assertEqual(_main_sha(self.root), base_before,
                         "main must not advance when the merge conflicts")
        self.assertIn("README.md", final.last_error or "",
                      "conflict summary should name the clashing file")


class TestNoAutoMerge(unittest.TestCase):
    """Confirm the legacy path (auto_merge=False) still commits on the
    feature branch and transitions MERGED without touching base."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text(
            "- [ ] Touch a file\n  details\n"
        )
        self.cfg = _mk_config(self.root, auto_merge=False)
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_commit_stays_on_feature_branch(self):
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        StubRunner(self.cfg, self.store).run(claimed)
        item = self.store.get(claimed.id)
        base_before = _main_sha(self.root)

        approve_and_commit(self.cfg, self.store, item, "stub note")

        final = self.store.get(claimed.id)
        self.assertEqual(final.status, ItemStatus.MERGED)
        self.assertEqual(_main_sha(self.root), base_before,
                         "main must not advance when auto_merge=False")
        self.assertTrue(_branch_exists(self.root, item.branch),
                        "feature branch must be kept for manual merge")


class TestRebaseMode(unittest.TestCase):
    """merge_mode="rebase" — linear history, no merge commits."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text(
            "- [ ] Touch a file\n  details\n"
        )
        self.cfg = _mk_config(self.root, auto_merge=True, merge_mode="rebase")
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _claim_and_stub(self):
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        StubRunner(self.cfg, self.store).run(claimed)
        return self.store.get(claimed.id)

    def test_rebase_produces_linear_history(self):
        item = self._claim_and_stub()
        approve_and_commit(self.cfg, self.store, item, "stub note")

        self.assertEqual(self.store.get(item.id).status, ItemStatus.MERGED)
        # Linear means every commit reachable from main has at most one
        # parent — no merge commits.
        cp = subprocess.run(
            ["git", "log", "--pretty=%P", "main"],
            cwd=self.root, capture_output=True, text=True, check=True,
        )
        for line in cp.stdout.splitlines():
            parents = line.split()
            self.assertLessEqual(
                len(parents), 1,
                f"merge commit in linear rebase history: parents={parents}",
            )

    def test_rebase_conflict_keeps_feature_intact(self):
        item = self._claim_and_stub()
        wt = Path(item.worktree_path)

        (wt / "README.md").write_text("# project\n\nFEAT\n")
        _git(wt, "add", "README.md")
        _git(wt, "commit", "-q", "-m", "feat readme")
        (self.root / "README.md").write_text("# project\n\nMAIN\n")
        _git(self.root, "add", "README.md")
        _git(self.root, "commit", "-q", "-m", "main readme")
        base_sha_before = _main_sha(self.root)

        approve_and_commit(self.cfg, self.store, item, "stub commit")

        final = self.store.get(item.id)
        self.assertEqual(final.status, ItemStatus.CONFLICTED)
        self.assertEqual(_main_sha(self.root), base_sha_before)
        # If rebase had rewritten feature onto the new base, `main` (with
        # the "main readme" commit) would be an ancestor of the feature.
        # The detached-temp-worktree strategy prevents that — feature
        # should still sit on top of the original fork point.
        cp = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base_sha_before,
             item.branch],
            cwd=self.root, capture_output=True, text=True,
        )
        self.assertNotEqual(
            cp.returncode, 0,
            "rebase must not rewrite feature branch to include base",
        )


class TestRetryMerge(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text(
            "- [ ] Touch a file\n  details\n"
        )
        self.cfg = _mk_config(self.root, auto_merge=True)
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _to_conflicted(self):
        """Drive an item into CONFLICTED with a README.md clash."""
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        StubRunner(self.cfg, self.store).run(claimed)
        item = self.store.get(claimed.id)
        wt = Path(item.worktree_path)
        (wt / "README.md").write_text("# project\n\nFEAT\n")
        _git(wt, "add", "README.md")
        _git(wt, "commit", "-q", "-m", "feat readme")
        (self.root / "README.md").write_text("# project\n\nMAIN\n")
        _git(self.root, "add", "README.md")
        _git(self.root, "commit", "-q", "-m", "main readme")
        approve_and_commit(self.cfg, self.store, item, "stub commit")
        conflicted = self.store.get(item.id)
        assert conflicted.status == ItemStatus.CONFLICTED
        return conflicted

    def test_retry_merges_after_resolving_in_worktree(self):
        item = self._to_conflicted()
        wt = Path(item.worktree_path)
        # User resolves by taking main's version of the file.
        (wt / "README.md").write_text("# project\n\nMAIN\n")
        base_before = _main_sha(self.root)

        ok, msg = retry_merge(self.cfg, self.store, item)

        self.assertTrue(ok, msg)
        final = self.store.get(item.id)
        self.assertEqual(final.status, ItemStatus.MERGED)
        self.assertIsNone(final.last_error)
        self.assertFalse(wt.exists())
        self.assertFalse(_branch_exists(self.root, item.branch))
        self.assertNotEqual(_main_sha(self.root), base_before)

    def test_retry_stays_conflicted_when_still_clashing(self):
        item = self._to_conflicted()
        # No resolution — retry hits the same clash.
        ok, _msg = retry_merge(self.cfg, self.store, item)

        self.assertFalse(ok)
        final = self.store.get(item.id)
        self.assertEqual(final.status, ItemStatus.CONFLICTED)
        self.assertTrue(Path(item.worktree_path).exists())
        self.assertTrue(_branch_exists(self.root, item.branch))


if __name__ == "__main__":
    unittest.main()
