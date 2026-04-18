import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentor.committer import (approve_and_commit, approve_backlog,
                                approve_plan, resubmit_conflicted, retry,
                                retry_merge)
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


def _mk_config(root: Path, *, merge_mode: str = "merge") -> Config:
    return Config(
        project_name=root.name,
        project_root=root,
        sources=SourcesConfig(watch=["backlog.md"], exclude=[]),
        parsing=ParsingConfig(mode="checkbox"),
        agent=AgentConfig(pool_size=1),
        git=GitConfig(base_branch="main", branch_prefix="agent/",
                      merge_mode=merge_mode),
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
        self.cfg = _mk_config(self.root)
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
        # Feature context leads; merge-mechanics trail.
        err = final.last_error or ""
        self.assertIn(f"Feature: {item.title}", err)
        self.assertIn(f"Branch:  {item.branch}", err)
        self.assertLess(
            err.index(item.title), err.index("── merge conflict"),
            "feature context must appear before the merge-conflict block",
        )
        self.assertLess(
            err.index("── merge conflict"), err.index("README.md"),
            "conflicted-files mechanics must appear in the trailing block",
        )
        self.assertIn("merge into main", err)


class TestRebaseMode(unittest.TestCase):
    """merge_mode="rebase" — linear history, no merge commits."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text(
            "- [ ] Touch a file\n  details\n"
        )
        self.cfg = _mk_config(self.root, merge_mode="rebase")
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
        err = final.last_error or ""
        self.assertIn(f"Feature: {item.title}", err)
        self.assertIn(f"Branch:  {item.branch}", err)
        self.assertIn("rebase into main", err)
        self.assertLess(
            err.index(item.title), err.index("── merge conflict"),
            "feature context must appear before the merge-conflict block",
        )


class TestRetryMerge(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text(
            "- [ ] Touch a file\n  details\n"
        )
        self.cfg = _mk_config(self.root)
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

    def test_resubmit_conflicted_requeues_with_feedback(self):
        item = self._to_conflicted()
        self.store.transition(
            item.id, ItemStatus.CONFLICTED,
            session_id="sess-abc", result_json='{"phase":"plan","plan":"p"}',
        )
        item = self.store.get(item.id)
        original_wt = item.worktree_path
        original_branch = item.branch

        resubmit_conflicted(self.cfg, self.store, item)

        final = self.store.get(item.id)
        self.assertEqual(final.status, ItemStatus.QUEUED)
        self.assertEqual(final.attempts, 0)
        self.assertIsNone(final.last_error)
        # Worktree + branch + session preserved so the runner can resume.
        self.assertEqual(final.worktree_path, original_wt)
        self.assertEqual(final.branch, original_branch)
        self.assertEqual(final.session_id, "sess-abc")
        self.assertEqual(final.result_json, '{"phase":"plan","plan":"p"}')
        self.assertTrue(Path(original_wt).exists())
        self.assertTrue(_branch_exists(self.root, original_branch))
        self.assertIn("conflict", (final.feedback or "").lower())
        self.assertIn("main", final.feedback or "")


class TestConflictSummaryFormat(unittest.TestCase):
    """Feature-context framing of the CONFLICTED `last_error`."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text(
            "- [ ] Reshape README intro\n"
            "  line one of intent\n"
            "  line two describing why\n"
            "  line three tying it back\n"
        )
        self.cfg = _mk_config(self.root)
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _to_conflicted(self):
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
        return self.store.get(item.id)

    def test_summary_includes_body_before_trailing_mechanics(self):
        item = self._to_conflicted()
        err = item.last_error or ""
        for line in ("line one of intent",
                     "line two describing why",
                     "line three tying it back"):
            self.assertIn(line, err, f"item body line missing: {line!r}")
        marker = "── merge conflict"
        # Body sits between the header and the trailing mechanics block.
        self.assertLess(err.index("line one of intent"), err.index(marker))
        self.assertLess(err.index("line three tying it back"),
                        err.index(marker))
        # The trailing block is actually short — conflicted files + a
        # short git-output tail, no feature material below it.
        tail = err[err.index(marker):]
        self.assertIn("README.md", tail)
        self.assertNotIn("line one of intent", tail)

    def test_retry_summary_marks_retry(self):
        item = self._to_conflicted()
        # No resolution — retry hits the same clash.
        ok, _ = retry_merge(self.cfg, self.store, item)
        self.assertFalse(ok)
        err = self.store.get(item.id).last_error or ""
        self.assertIn("Feature: Reshape README intro", err)
        self.assertIn("merge into main, retry", err,
                      "retry trailing block should be labeled 'retry'")


class TestAutoResolveConflicts(unittest.TestCase):
    """`git.auto_resolve_conflicts` chains resubmit_conflicted into
    approve_and_commit so a CONFLICTED transition immediately becomes
    QUEUED with conflict-resolution feedback."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text(
            "- [ ] Touch a file\n  details\n"
        )
        self.store = Store(self.root / ".agentor" / "state.db")

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _drive_to_conflict(self, cfg: Config):
        """Run the stub through AWAITING_REVIEW with a README clash queued
        against main, then call approve_and_commit under `cfg`."""
        scan_once(cfg, self.store)
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        StubRunner(cfg, self.store).run(claimed)
        item = self.store.get(claimed.id)
        # Seed a fake session_id so we can verify the runner-resume state
        # survives the chained resubmit.
        self.store.transition(item.id, item.status, session_id="sess-auto")
        item = self.store.get(item.id)
        wt = Path(item.worktree_path)
        (wt / "README.md").write_text("# project\n\nFEAT\n")
        _git(wt, "add", "README.md")
        _git(wt, "commit", "-q", "-m", "feat readme")
        (self.root / "README.md").write_text("# project\n\nMAIN\n")
        _git(self.root, "add", "README.md")
        _git(self.root, "commit", "-q", "-m", "main readme")
        approve_and_commit(cfg, self.store, item, "stub commit")
        return self.store.get(item.id)

    def test_auto_resolve_off_leaves_conflicted(self):
        cfg = _mk_config(self.root)
        self.assertFalse(cfg.git.auto_resolve_conflicts)
        final = self._drive_to_conflict(cfg)
        self.assertEqual(final.status, ItemStatus.CONFLICTED)
        self.assertIsNone(final.feedback)
        self.assertIsNotNone(final.last_error)

    def test_auto_resolve_on_requeues_with_feedback(self):
        cfg = _mk_config(self.root)
        cfg.git.auto_resolve_conflicts = True
        final = self._drive_to_conflict(cfg)

        self.assertEqual(final.status, ItemStatus.QUEUED)
        self.assertIsNone(final.last_error)
        self.assertEqual(final.attempts, 0)
        # Worktree, branch, session all preserved so the runner resumes.
        self.assertTrue(Path(final.worktree_path).exists())
        self.assertTrue(_branch_exists(self.root, final.branch))
        self.assertEqual(final.session_id, "sess-auto")
        # Feedback carries the conflict summary + base branch guidance.
        fb = final.feedback or ""
        self.assertIn("conflict", fb.lower())
        self.assertIn("main", fb)
        self.assertIn("README.md", fb)


class TestRetryErrored(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] Task\n")
        self.cfg = _mk_config(self.root)
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_errored_requeues_and_resets_attempts(self):
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        self.store.transition(
            claimed.id, ItemStatus.ERRORED,
            attempts=2, last_error="do_work: claude timed out after 1800s",
        )

        retry(self.store, self.store.get(claimed.id))

        final = self.store.get(claimed.id)
        self.assertEqual(final.status, ItemStatus.QUEUED)
        self.assertIsNone(final.last_error)
        self.assertEqual(final.attempts, 0)


class TestApproveFeedbackSplit(unittest.TestCase):
    """`approve_plan` and `approve_backlog` accept optional feedback that
    the runner consumes on the next prompt via _prepend_feedback. No
    feedback → no prompt override; the split lets the dashboard keep
    approve pure and gate feedback behind a separate action."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text(
            "- [ ] Split item\n  details\n"
        )
        self.cfg = _mk_config(self.root)
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _backlog_item(self):
        queued = self.store.list_by_status(ItemStatus.QUEUED)[0]
        # scan_once inserts at QUEUED under auto pickup; drop back to
        # BACKLOG so approve_backlog's precondition holds.
        self.store.transition(queued.id, ItemStatus.BACKLOG)
        return self.store.get(queued.id)

    def _plan_review_item(self):
        queued = self.store.list_by_status(ItemStatus.QUEUED)[0]
        self.store.transition(
            queued.id, ItemStatus.AWAITING_PLAN_REVIEW,
            result_json='{"phase":"plan","plan":"draft"}',
        )
        return self.store.get(queued.id)

    def test_approve_backlog_no_feedback_leaves_field_none(self):
        item = self._backlog_item()
        approve_backlog(self.store, item)

        final = self.store.get(item.id)
        self.assertEqual(final.status, ItemStatus.QUEUED)
        self.assertIsNone(final.feedback)
        last = self.store.transitions_for(item.id)[-1]
        self.assertNotIn("with feedback", last.note or "")

    def test_approve_backlog_with_feedback_sets_field(self):
        item = self._backlog_item()
        approve_backlog(self.store, item, feedback="use pytest")

        final = self.store.get(item.id)
        self.assertEqual(final.status, ItemStatus.QUEUED)
        self.assertEqual(final.feedback, "use pytest")
        last = self.store.transitions_for(item.id)[-1]
        self.assertIn("with feedback", last.note or "")

    def test_approve_plan_without_feedback_preserves_prior_feedback(self):
        item = self._plan_review_item()
        approve_plan(self.store, item)

        final = self.store.get(item.id)
        self.assertEqual(final.status, ItemStatus.QUEUED)
        self.assertIsNone(final.feedback)
        last = self.store.transitions_for(item.id)[-1]
        self.assertNotIn("with feedback", last.note or "")

    def test_approve_plan_with_feedback_sets_field(self):
        item = self._plan_review_item()
        approve_plan(self.store, item, feedback="avoid touching store.py")

        final = self.store.get(item.id)
        self.assertEqual(final.status, ItemStatus.QUEUED)
        self.assertEqual(final.feedback, "avoid touching store.py")
        last = self.store.transitions_for(item.id)[-1]
        self.assertIn("with feedback", last.note or "")

    def test_approve_plan_empty_feedback_is_noop(self):
        item = self._plan_review_item()
        approve_plan(self.store, item, feedback="")

        final = self.store.get(item.id)
        self.assertEqual(final.status, ItemStatus.QUEUED)
        self.assertIsNone(final.feedback)


if __name__ == "__main__":
    unittest.main()
