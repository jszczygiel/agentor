import json
import subprocess
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentor.committer import (approve_and_commit, approve_plan,
                                resubmit_conflicted, retry, retry_merge)
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

    def test_manual_resubmit_preserves_result_json(self):
        """Default (force_execute=False) path — e.g. dashboard `[e]resubmit`
        — must leave result_json byte-identical so the next dispatch re-runs
        the plan phase as before."""
        item = self._to_conflicted()
        original_json = '{"phase":"execute","summary":"done","plan":"p"}'
        self.store.transition(
            item.id, ItemStatus.CONFLICTED,
            result_json=original_json,
        )
        item = self.store.get(item.id)

        resubmit_conflicted(self.cfg, self.store, item)

        final = self.store.get(item.id)
        self.assertEqual(final.result_json, original_json)
        last = self.store.transitions_for(final.id)[-1]
        self.assertNotIn("force_execute", last.note or "")

    def test_force_execute_flips_phase_and_preserves_other_keys(self):
        item = self._to_conflicted()
        original_json = (
            '{"phase":"execute","summary":"done","plan":"orig plan",'
            '"num_turns":12}'
        )
        self.store.transition(
            item.id, ItemStatus.CONFLICTED,
            result_json=original_json,
        )
        item = self.store.get(item.id)

        resubmit_conflicted(self.cfg, self.store, item, force_execute=True)

        final = self.store.get(item.id)
        data = json.loads(final.result_json)
        self.assertEqual(data["phase"], "plan")
        self.assertEqual(data["plan"], "orig plan")
        self.assertEqual(data["summary"], "done")
        self.assertEqual(data["num_turns"], 12)

    def test_force_execute_sets_fallback_plan_when_missing(self):
        """Items that ran in single_phase mode have no `plan` key in
        result_json; force_execute must still yield a non-empty plan
        string so the execute prompt's {plan} placeholder substitutes."""
        item = self._to_conflicted()
        self.store.transition(
            item.id, ItemStatus.CONFLICTED,
            result_json='{"phase":"execute","summary":"done"}',
        )
        item = self.store.get(item.id)

        resubmit_conflicted(self.cfg, self.store, item, force_execute=True)

        final = self.store.get(item.id)
        data = json.loads(final.result_json)
        self.assertEqual(data["phase"], "plan")
        self.assertTrue(data.get("plan"))
        self.assertIn("conflict resolution", data["plan"])

    def test_force_execute_with_absent_result_json(self):
        """Defensive: null/missing/invalid result_json must still produce
        a well-formed phase=plan envelope."""
        item = self._to_conflicted()
        self.store.transition(
            item.id, ItemStatus.CONFLICTED, result_json=None,
        )
        item = self.store.get(item.id)

        resubmit_conflicted(self.cfg, self.store, item, force_execute=True)

        final = self.store.get(item.id)
        data = json.loads(final.result_json)
        self.assertEqual(data["phase"], "plan")
        self.assertTrue(data.get("plan"))


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

    def test_auto_resolve_on_marks_transition_note(self):
        """Chained resubmit tags the CONFLICTED → QUEUED transition so the
        dashboard can distinguish an auto-chain from a manual resubmit AND
        flips result_json so the runner skips the plan phase on the next
        dispatch (conflict resolution is pure execute work)."""
        from agentor.committer import AUTO_RESOLVE_NOTE_PREFIX
        cfg = _mk_config(self.root)
        cfg.git.auto_resolve_conflicts = True
        final = self._drive_to_conflict(cfg)

        self.assertEqual(final.status, ItemStatus.QUEUED)
        history = self.store.transitions_for(final.id)
        chain = [
            t for t in history
            if t.from_status == ItemStatus.CONFLICTED
            and t.to_status == ItemStatus.QUEUED
        ]
        self.assertEqual(len(chain), 1)
        self.assertTrue(
            (chain[0].note or "").startswith(AUTO_RESOLVE_NOTE_PREFIX),
            f"expected auto-resolve marker, got note={chain[0].note!r}",
        )
        # Force-execute leg: result_json must be rewritten so the runner's
        # two-phase dispatch takes the prior-plan branch → _do_execute.
        data = json.loads(final.result_json)
        self.assertEqual(data["phase"], "plan",
                         "phase must be 'plan' so the runner routes "
                         "straight to _do_execute")
        self.assertTrue(data.get("plan"),
                        "plan text must be non-empty so the execute "
                        "prompt template substitutes cleanly")
        # Transition note also records the force flag for greppable history.
        self.assertIn("force_execute", chain[0].note or "")

    def test_manual_resubmit_has_no_auto_marker(self):
        """Manual `[e]` resubmit (auto off) must not carry the marker —
        the dashboard uses its absence to keep the indicator silent."""
        from agentor.committer import AUTO_RESOLVE_NOTE_PREFIX
        cfg = _mk_config(self.root)
        # auto_resolve_conflicts stays False — drive reaches CONFLICTED only.
        conflicted = self._drive_to_conflict(cfg)
        self.assertEqual(conflicted.status, ItemStatus.CONFLICTED)

        resubmit_conflicted(cfg, self.store, conflicted)

        final = self.store.get(conflicted.id)
        self.assertEqual(final.status, ItemStatus.QUEUED)
        history = self.store.transitions_for(final.id)
        chain = [
            t for t in history
            if t.from_status == ItemStatus.CONFLICTED
            and t.to_status == ItemStatus.QUEUED
        ]
        self.assertEqual(len(chain), 1)
        self.assertFalse(
            (chain[0].note or "").startswith(AUTO_RESOLVE_NOTE_PREFIX),
            f"manual resubmit should not carry the auto marker: "
            f"note={chain[0].note!r}",
        )


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
    """`approve_plan` accepts optional feedback that the runner consumes on
    the next prompt via _prepend_feedback. No feedback → no prompt
    override; the split lets the dashboard keep approve pure and gate
    feedback behind a separate action."""

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

    def _plan_review_item(self):
        queued = self.store.list_by_status(ItemStatus.QUEUED)[0]
        self.store.transition(
            queued.id, ItemStatus.AWAITING_PLAN_REVIEW,
            result_json='{"phase":"plan","plan":"draft"}',
        )
        return self.store.get(queued.id)

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


class TestConcurrentIntegration(unittest.TestCase):
    """Two AWAITING_REVIEW items approved simultaneously must both reach
    MERGED with distinct commits on base. Without `_INTEGRATION_LOCK` this
    races: both threads capture the same `base_sha` via `rev-parse`, the
    first wins the CAS `update-ref OLD NEW`, and the second transitions
    CONFLICTED with a spurious ref-changed summary for work that didn't
    actually conflict."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text(
            "- [ ] First item\n  details A\n"
            "- [ ] Second item\n  details B\n"
        )
        self.cfg = _mk_config(self.root)
        self.cfg.agent.pool_size = 2
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _drive_to_awaiting_review(self) -> list:
        queued = self.store.list_by_status(ItemStatus.QUEUED)
        self.assertEqual(len(queued), 2)
        items = []
        for q in queued:
            wt, br = plan_worktree(self.cfg, q)
            claimed = self.store.claim_next_queued(str(wt), br)
            StubRunner(self.cfg, self.store).run(claimed)
            items.append(self.store.get(claimed.id))
        for item in items:
            self.assertEqual(item.status, ItemStatus.AWAITING_REVIEW)
        return items

    def test_concurrent_approvals_both_merge(self):
        import threading as _t
        items = self._drive_to_awaiting_review()
        base_before = _main_sha(self.root)
        barrier = _t.Barrier(len(items))
        results: dict[str, object] = {}
        errors: dict[str, BaseException] = {}

        def work(it):
            try:
                barrier.wait(timeout=5)
                results[it.id] = approve_and_commit(
                    self.cfg, self.store, it, f"stub commit {it.id[:6]}",
                )
            except BaseException as e:  # noqa: BLE001 — surface in test
                errors[it.id] = e

        threads = [_t.Thread(target=work, args=(it,)) for it in items]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive(), "worker thread stuck")

        self.assertEqual(errors, {}, f"unexpected errors: {errors}")
        finals = [self.store.get(it.id) for it in items]
        for f in finals:
            self.assertEqual(
                f.status, ItemStatus.MERGED,
                f"{f.id[:8]} not MERGED: {f.status} last_error={f.last_error!r}",
            )
        # Both returned feature SHAs exist and differ.
        shas = list(results.values())
        self.assertEqual(len(set(shas)), 2, f"feature SHAs collided: {shas}")
        # Main has advanced by two commits (merge commits in "merge" mode),
        # each pulling in one feature SHA.
        self.assertNotEqual(_main_sha(self.root), base_before)
        log = subprocess.run(
            ["git", "log", "--pretty=%H", f"{base_before}..main"],
            cwd=self.root, capture_output=True, text=True, check=True,
        ).stdout.split()
        for sha in shas:
            self.assertIn(
                str(sha), log,
                "feature commit should be reachable from main",
            )
        # Feature branches and worktrees cleaned up for both.
        for it in items:
            self.assertFalse(
                _branch_exists(self.root, it.branch),
                f"feature branch {it.branch} should be deleted",
            )
            self.assertFalse(
                Path(it.worktree_path).exists(),
                f"worktree {it.worktree_path} should be removed",
            )

    def test_integration_lock_serialises_base_branch_updates(self):
        """Directly verify mutual exclusion: instrument
        `git_ops.merge_feature_into_base` to track concurrent entries. With
        the lock in place the counter never exceeds 1."""
        import threading as _t
        from agentor import git_ops as _git_ops

        items = self._drive_to_awaiting_review()
        entry_counter = {"live": 0, "max": 0}
        entry_lock = _t.Lock()
        original = _git_ops.merge_feature_into_base

        def instrumented(*a, **kw):
            with entry_lock:
                entry_counter["live"] += 1
                if entry_counter["live"] > entry_counter["max"]:
                    entry_counter["max"] = entry_counter["live"]
            try:
                # Give the other thread a chance to race in if the
                # integration lock were missing.
                time.sleep(0.1)
                return original(*a, **kw)
            finally:
                with entry_lock:
                    entry_counter["live"] -= 1

        # Patch both the module and the local import inside committer.
        from agentor import committer as _committer
        _git_ops.merge_feature_into_base = instrumented
        _committer.git_ops.merge_feature_into_base = instrumented
        try:
            barrier = _t.Barrier(len(items))

            def work(it):
                barrier.wait(timeout=5)
                approve_and_commit(
                    self.cfg, self.store, it, f"stub commit {it.id[:6]}",
                )

            threads = [_t.Thread(target=work, args=(it,)) for it in items]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
        finally:
            _git_ops.merge_feature_into_base = original
            _committer.git_ops.merge_feature_into_base = original

        self.assertEqual(
            entry_counter["max"], 1,
            "integration lock must serialise merge_feature_into_base; "
            f"observed {entry_counter['max']} concurrent entries",
        )


if __name__ == "__main__":
    unittest.main()
