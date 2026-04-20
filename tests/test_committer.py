import json
import subprocess
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agentor.committer import (approve_and_commit, approve_plan,
                                resubmit_conflicted, retry, retry_merge)


def _suppress_auto_chain(test: unittest.TestCase) -> None:
    """Disable the committer's unconditional CONFLICTED→QUEUED auto-chain
    for tests that need to observe the CONFLICTED state directly. The
    auto-chain has dedicated coverage in `TestAutoResolveConflicts`."""
    p = patch("agentor.committer.resubmit_conflicted")
    p.start()
    test.addCleanup(p.stop)
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


def _head_sha(repo: Path) -> str:
    cp = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return cp.stdout.strip()


class TestAutoMerge(unittest.TestCase):
    def setUp(self):
        _suppress_auto_chain(self)
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
        _suppress_auto_chain(self)
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
        _suppress_auto_chain(self)
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
        """Default (force_execute=False) path — direct `resubmit_conflicted`
        call — must leave result_json byte-identical so the next dispatch
        re-runs the plan phase as before."""
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


class TestResubmitConflictedFeedback(unittest.TestCase):
    """`resubmit_conflicted` picks its feedback template by cause:
    `last_error="agent-log missing"` (the `require_agent_log` gate)
    gets a log-generation prompt; anything else gets the merge-conflict
    prompt. Runner's `_prepend_feedback` concatenates this verbatim into
    the next prompt, so the exact wording matters."""

    def setUp(self):
        _suppress_auto_chain(self)
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

    def _seed_conflicted(self, *, last_error: str):
        """Drive an item to CONFLICTED with a given `last_error`, without
        needing a real merge clash — `resubmit_conflicted` only reads
        `status`, `worktree_path`, `branch`, and `last_error`."""
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        StubRunner(self.cfg, self.store).run(claimed)
        # StubRunner lands in AWAITING_REVIEW; jump straight to CONFLICTED
        # with the desired cause.
        self.store.transition(
            claimed.id, ItemStatus.CONFLICTED,
            last_error=last_error,
            note="seeded for feedback test",
        )
        return self.store.get(claimed.id)

    def test_feedback_is_log_generation_when_cause_is_agent_log_missing(self):
        item = self._seed_conflicted(last_error="agent-log missing")

        resubmit_conflicted(self.cfg, self.store, item)

        final = self.store.get(item.id)
        self.assertEqual(final.status, ItemStatus.QUEUED)
        fb = final.feedback or ""
        self.assertIn("docs/agent-logs/", fb)
        self.assertIn("Surprises", fb)
        self.assertIn("Outcome", fb)
        # Log-absence cause must NOT instruct the agent to run
        # `git merge <base>` or resolve conflict markers — that was
        # the bug the dedicated prompt fixes.
        self.assertNotIn("git merge main", fb)
        self.assertNotIn("conflict markers", fb)
        self.assertNotIn("Conflict summary", fb)

    def test_feedback_is_merge_conflict_when_cause_is_generic(self):
        summary = (
            "── merge conflict ──\nFeature: Touch a file\n"
            "Branch:  agent/touch-a-file\nCONFLICT (content): README.md\n"
        )
        item = self._seed_conflicted(last_error=summary)

        resubmit_conflicted(self.cfg, self.store, item)

        final = self.store.get(item.id)
        self.assertEqual(final.status, ItemStatus.QUEUED)
        fb = final.feedback or ""
        self.assertIn("git merge main", fb)
        self.assertIn("Conflict summary", fb)
        self.assertIn("CONFLICT (content): README.md", fb)
        self.assertNotIn("docs/agent-logs/", fb)

    def test_feedback_falls_back_to_merge_conflict_when_last_error_is_none(self):
        """Defensive: `last_error=None` (rare but legal) must not trigger
        the agent-log prompt — the sentinel match is exact-string."""
        item = self._seed_conflicted(last_error="")
        # Clear the seeded last_error to None via a second transition.
        self.store.transition(
            item.id, ItemStatus.CONFLICTED, last_error=None,
            note="clear last_error",
        )
        item = self.store.get(item.id)
        self.assertIsNone(item.last_error)

        resubmit_conflicted(self.cfg, self.store, item)

        fb = self.store.get(item.id).feedback or ""
        self.assertIn("git merge main", fb)
        self.assertNotIn("docs/agent-logs/", fb)


class TestConflictSummaryFormat(unittest.TestCase):
    """Feature-context framing of the CONFLICTED `last_error`."""

    def setUp(self):
        _suppress_auto_chain(self)
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
    """`approve_and_commit` always chains `resubmit_conflicted` on a
    CONFLICTED transition — the item lands back in QUEUED with
    conflict-resolution feedback so the agent fixes the merge in-place."""

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

    def _drive_through_conflict(self, cfg: Config):
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

    def test_chain_requeues_with_feedback(self):
        cfg = _mk_config(self.root)
        final = self._drive_through_conflict(cfg)

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

    def test_chain_marks_transition_note(self):
        """Chained resubmit tags the CONFLICTED → QUEUED transition so the
        dashboard can distinguish an auto-chain from a manual resubmit AND
        flips result_json so the runner skips the plan phase on the next
        dispatch (conflict resolution is pure execute work)."""
        from agentor.committer import AUTO_RESOLVE_NOTE_PREFIX
        cfg = _mk_config(self.root)
        final = self._drive_through_conflict(cfg)

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
        """Manual `resubmit_conflicted` (e.g. after `[m]` retry_merge still
        conflicts) must not carry the auto marker — the dashboard uses its
        absence to keep the indicator silent."""
        from agentor.committer import AUTO_RESOLVE_NOTE_PREFIX
        cfg = _mk_config(self.root)
        # Drive through approve_and_commit, then walk the item back to
        # CONFLICTED (as retry_merge would on continued conflict) so we can
        # exercise the manual resubmit path directly.
        requeued = self._drive_through_conflict(cfg)
        self.store.transition(
            requeued.id, ItemStatus.CONFLICTED,
            last_error="still conflicting on README.md",
            note="retry merge still conflicts on main",
        )
        conflicted = self.store.get(requeued.id)

        resubmit_conflicted(cfg, self.store, conflicted)

        final = self.store.get(conflicted.id)
        self.assertEqual(final.status, ItemStatus.QUEUED)
        history = self.store.transitions_for(final.id)
        chain = [
            t for t in history
            if t.from_status == ItemStatus.CONFLICTED
            and t.to_status == ItemStatus.QUEUED
        ]
        # Two CONFLICTED → QUEUED transitions now exist: the auto-chain
        # from approve_and_commit and the manual one we just triggered.
        self.assertEqual(len(chain), 2)
        latest = max(chain, key=lambda t: t.at)
        self.assertFalse(
            (latest.note or "").startswith(AUTO_RESOLVE_NOTE_PREFIX),
            f"manual resubmit should not carry the auto marker: "
            f"note={latest.note!r}",
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


class TestDeleteIdea(unittest.TestCase):
    """`delete_idea` is the committer-layer wrapper the dashboard calls
    when the operator confirms `x` in the inspect view. It must
    hard-delete (not just CANCELLED-transition) and tombstone the id,
    tearing down any live runner state on the way."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.store = Store(Path(self.td.name) / "state.db")

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _seed_deferred(self, id: str = "d1") -> None:
        from agentor.models import Item
        self.store.upsert_discovered(Item(
            id=id, title="idea", body="b",
            source_file="backlog.md", source_line=1, tags={},
        ))
        self.store.transition(id, ItemStatus.DEFERRED, note="park")

    def test_delete_idea_removes_row_and_records_tombstone(self):
        from agentor.committer import delete_idea
        self._seed_deferred("d1")
        item = self.store.get("d1")
        self.assertIsNotNone(item)

        # DEFERRED items have no worktree and no live subprocess, so
        # config/daemon can be None — the cross-status inspect delete
        # exercises the other branches.
        delete_idea(None, self.store, None, item)

        self.assertIsNone(self.store.get("d1"))
        self.assertTrue(self.store.is_deleted("d1"))
        row = self.store.conn.execute(
            "SELECT note, last_status FROM deletions WHERE item_id = ?",
            ("d1",),
        ).fetchone()
        self.assertEqual(row["last_status"], ItemStatus.DEFERRED.value)
        self.assertEqual(row["note"], "deleted from deferred")


class TestAdvanceUserCheckout(unittest.TestCase):
    """After a clean auto-merge CAS-advances refs/heads/<base>, the user's
    primary checkout at `project.root` should fast-forward so its index
    and working tree match the new tip — unless any of the safety guards
    trip, in which case it's left untouched silently."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        # Gitignore + commit backlog so self.root starts truly clean —
        # the guard rejects any `status --porcelain` output, including
        # untracked files like the `.agentor/` state dir.
        (self.root / ".gitignore").write_text(".agentor/\n")
        (self.root / "backlog.md").write_text(
            "- [ ] Touch a file\n  details\n"
        )
        _git(self.root, "add", ".gitignore", "backlog.md")
        _git(self.root, "commit", "-q", "-m", "seed backlog")
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

    def test_happy_path_advances_checkout_to_new_base_tip(self):
        """Default config, checkout on main + clean: HEAD, index, and
        working tree all land at the new merge commit."""
        item = self._claim_and_stub()
        base_before = _main_sha(self.root)
        self.assertEqual(_head_sha(self.root), base_before)

        approve_and_commit(self.cfg, self.store, item, "stub note")

        new_main = _main_sha(self.root)
        self.assertNotEqual(new_main, base_before, "ref should advance")
        self.assertEqual(
            _head_sha(self.root), new_main,
            "HEAD at user checkout should follow new base tip",
        )
        # Working tree has the stub's note file — reachable because the
        # reset pulled in the new tree.
        ls = subprocess.run(
            ["git", "ls-files"],
            cwd=self.root, capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn(".agentor-note-", ls,
                      "stub note should be in the user's working tree")
        # No spurious staged/unstaged diff vs HEAD.
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.root, capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(status, "",
                         f"checkout should be clean post-advance: {status!r}")

    def test_config_off_skipped(self):
        self.cfg.git.advance_user_checkout = False
        item = self._claim_and_stub()
        base_before = _main_sha(self.root)

        approve_and_commit(self.cfg, self.store, item, "stub note")

        self.assertNotEqual(_main_sha(self.root), base_before,
                            "ref must still advance")
        # HEAD symbolically follows the ref on the base branch, so
        # rev-parse HEAD returns the new sha regardless — the real signal
        # for "skipped" is the stale index: status --porcelain reports
        # the merge commit's diff as staged changes.
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.root, capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertNotEqual(
            status, "",
            "without advance, checkout's index should be stale vs new HEAD",
        )

    def test_dirty_worktree_skipped(self):
        """Uncommitted user work at `project.root` must survive the merge."""
        (self.root / "user_scratch.txt").write_text("uncommitted\n")
        item = self._claim_and_stub()
        base_before = _main_sha(self.root)

        approve_and_commit(self.cfg, self.store, item, "stub note")

        self.assertNotEqual(_main_sha(self.root), base_before)
        self.assertTrue(
            (self.root / "user_scratch.txt").exists(),
            "uncommitted user file must not be clobbered",
        )
        self.assertEqual(
            (self.root / "user_scratch.txt").read_text(), "uncommitted\n",
        )

    def test_different_branch_skipped(self):
        """Operator parked on a different branch → advance must skip so
        the sidecar branch isn't silently reset."""
        _git(self.root, "checkout", "-b", "sidecar")
        item = self._claim_and_stub()
        sidecar_before = _head_sha(self.root)
        base_before = _main_sha(self.root)

        approve_and_commit(self.cfg, self.store, item, "stub note")

        self.assertNotEqual(_main_sha(self.root), base_before,
                            "main ref must still advance")
        self.assertEqual(
            _head_sha(self.root), sidecar_before,
            "HEAD on sidecar must be untouched",
        )
        cp = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=self.root, capture_output=True, text=True, check=True,
        )
        self.assertEqual(cp.stdout.strip(), "sidecar")

    def test_detached_head_skipped(self):
        """Detached HEAD at `project.root` must not be touched."""
        base_before = _main_sha(self.root)
        _git(self.root, "checkout", "--detach", base_before)
        item = self._claim_and_stub()

        approve_and_commit(self.cfg, self.store, item, "stub note")

        self.assertNotEqual(_main_sha(self.root), base_before,
                            "ref must advance")
        self.assertEqual(
            _head_sha(self.root), base_before,
            "detached HEAD must stay pinned",
        )
        # Still detached (symbolic-ref fails with non-zero rc).
        cp = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "HEAD"],
            cwd=self.root, capture_output=True, text=True,
        )
        self.assertNotEqual(cp.returncode, 0,
                            "HEAD must remain detached")

    def test_diverged_head_skipped(self):
        """Local commit on base above the dispatched base_sha_before →
        advance must skip (unit-level check on the guard)."""
        from agentor import git_ops

        # Repo starts on main, clean. Capture base_sha_before, then add
        # a local commit so HEAD diverges from the captured sha.
        base_sha_before = _main_sha(self.root)
        (self.root / "local.txt").write_text("local work\n")
        _git(self.root, "add", "local.txt")
        _git(self.root, "commit", "-q", "-m", "local commit")
        self.assertNotEqual(_head_sha(self.root), base_sha_before)

        allowed, reason = git_ops.advance_user_checkout_allowed(
            self.root, "main", base_sha_before,
        )
        self.assertFalse(
            allowed,
            "guard must reject when HEAD diverges from captured base sha",
        )
        self.assertEqual(reason, "HEAD diverged from pre-merge base")

    def test_guard_allows_only_when_all_checks_pass(self):
        """Direct unit test of the guard helper — every branch returns
        the right `(allowed, reason)` tuple so callers can surface the
        skip cause to the operator."""
        from agentor import git_ops

        base = _main_sha(self.root)
        # Happy: on main, clean, HEAD == base.
        self.assertEqual(
            git_ops.advance_user_checkout_allowed(self.root, "main", base),
            (True, None),
        )
        # Dirty TRACKED file modification — `git reset --hard` would clobber.
        # Untracked files are intentionally NOT considered dirty here: reset
        # preserves them, and the daemon's own backlog drops live as
        # untracked files in `docs/backlog/`.
        (self.root / "README.md").write_text("dirty edit\n")
        self.assertEqual(
            git_ops.advance_user_checkout_allowed(self.root, "main", base),
            (False, "dirty worktree"),
        )
        _git(self.root, "checkout", "--", "README.md")
        # Untracked file alone — does NOT trip the dirty guard.
        (self.root / "scratch.txt").write_text("untracked\n")
        self.assertEqual(
            git_ops.advance_user_checkout_allowed(self.root, "main", base),
            (True, None),
        )
        (self.root / "scratch.txt").unlink()
        # Wrong branch.
        _git(self.root, "checkout", "-b", "other")
        self.assertEqual(
            git_ops.advance_user_checkout_allowed(self.root, "main", base),
            (False, "checkout on other"),
        )
        _git(self.root, "checkout", "main")
        # Detached HEAD reports specifically as "detached HEAD" rather
        # than the generic "checkout on HEAD", for operator legibility.
        _git(self.root, "checkout", "--detach")
        self.assertEqual(
            git_ops.advance_user_checkout_allowed(self.root, "main", base),
            (False, "detached HEAD"),
        )
        _git(self.root, "checkout", "main")
        # Wrong base_sha_before.
        self.assertEqual(
            git_ops.advance_user_checkout_allowed(
                self.root, "main", "0" * 40,
            ),
            (False, "HEAD diverged from pre-merge base"),
        )


class TestAdvanceUserCheckoutNoteSurfacing(unittest.TestCase):
    """Every MERGED transition carries a visible suffix recording the
    checkout-advance outcome so operators can grep history for the skip
    cause. Gate-off stays silent (matches main's pre-surfacing behavior
    for opt-outs)."""

    def setUp(self):
        _suppress_auto_chain(self)
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / ".gitignore").write_text(".agentor/\n")
        (self.root / "backlog.md").write_text(
            "- [ ] Touch a file\n  details\n"
        )
        _git(self.root, "add", ".gitignore", "backlog.md")
        _git(self.root, "commit", "-q", "-m", "seed backlog")
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

    def _last_note(self, item_id: str) -> str:
        return self.store.transitions_for(item_id)[-1].note or ""

    def test_happy_path_note_records_advance(self):
        item = self._claim_and_stub()

        approve_and_commit(self.cfg, self.store, item, "stub note")

        self.assertIn(", checkout advanced", self._last_note(item.id))

    def test_dirty_tracked_worktree_blocks_merge(self):
        # A modified TRACKED file in the user's base-branch checkout
        # blocks the merge entirely — `git reset --hard` would clobber
        # the operator's edits, so the gate refuses rather than silently
        # skipping the advance. CONFLICTED is terminal here (no
        # auto-resolve chain) since the agent can't fix the operator's
        # dirty checkout.
        (self.root / ".gitattributes").write_text("seed\n")
        _git(self.root, "add", ".gitattributes")
        _git(self.root, "commit", "-q", "-m", "track gitattributes")
        (self.root / ".gitattributes").write_text("dirty edit\n")
        item = self._claim_and_stub()

        approve_and_commit(self.cfg, self.store, item, "stub note")

        refreshed = self.store.get(item.id)
        self.assertEqual(refreshed.status, ItemStatus.CONFLICTED)
        self.assertIn("dirty base-branch checkout",
                      self._last_note(item.id))
        self.assertIn("Refusing to merge", refreshed.last_error or "")

    def test_untracked_file_does_not_block_merge(self):
        # Untracked files are preserved by `git reset --hard`, so the
        # gate ignores them — operators routinely have untracked
        # `docs/backlog/*.md` drops and other scratch files in the
        # base-branch checkout while the daemon is running.
        (self.root / "scratch.txt").write_text("wip\n")
        item = self._claim_and_stub()

        approve_and_commit(self.cfg, self.store, item, "stub note")

        refreshed = self.store.get(item.id)
        self.assertEqual(refreshed.status, ItemStatus.MERGED)
        self.assertIn(", checkout advanced", self._last_note(item.id))
        self.assertTrue((self.root / "scratch.txt").exists())

    def test_other_branch_note_records_skip_reason(self):
        _git(self.root, "checkout", "-b", "sidecar")
        item = self._claim_and_stub()

        approve_and_commit(self.cfg, self.store, item, "stub note")

        self.assertIn(", checkout skipped: checkout on sidecar",
                      self._last_note(item.id))

    def test_detached_head_note_records_skip_reason(self):
        _git(self.root, "checkout", "--detach", _main_sha(self.root))
        item = self._claim_and_stub()

        approve_and_commit(self.cfg, self.store, item, "stub note")

        self.assertIn(", checkout skipped: detached HEAD",
                      self._last_note(item.id))

    def test_diverged_head_note_records_skip_reason(self):
        """The "HEAD diverged" branch is unreachable end-to-end (the
        committer captures base_sha_before inside _INTEGRATION_LOCK, so
        any concurrent user commit on base would have to race with
        microsecond-level precision). Exercise the committer's wiring by
        monkeypatching the guard to return the diverged reason, then
        verify the suffix lands on the MERGED note."""
        from agentor import git_ops as _git_ops

        original = _git_ops.advance_user_checkout_allowed

        def stub(*_a, **_kw):
            return (False, "HEAD diverged from pre-merge base")

        _git_ops.advance_user_checkout_allowed = stub
        try:
            item = self._claim_and_stub()
            approve_and_commit(self.cfg, self.store, item, "stub note")
        finally:
            _git_ops.advance_user_checkout_allowed = original

        self.assertIn(
            ", checkout skipped: HEAD diverged from pre-merge base",
            self._last_note(item.id),
        )

    def test_gate_off_note_is_silent(self):
        """Gate off → no suffix at all. Matches main's pre-surfacing
        behavior when the operator explicitly opts out."""
        self.cfg.git.advance_user_checkout = False
        item = self._claim_and_stub()

        approve_and_commit(self.cfg, self.store, item, "stub note")

        note = self._last_note(item.id)
        self.assertNotIn("checkout advanced", note)
        self.assertNotIn("checkout skipped", note)

    def test_retry_merge_note_records_advance(self):
        """retry_merge's MERGED transition also carries the suffix so
        post-retry history shows whether the checkout caught up."""
        # Drive to CONFLICTED via a README clash, then resolve.
        item = self._claim_and_stub()
        wt = Path(item.worktree_path)
        (wt / "README.md").write_text("# project\n\nFEAT\n")
        _git(wt, "add", "README.md")
        _git(wt, "commit", "-q", "-m", "feat readme")
        (self.root / "README.md").write_text("# project\n\nMAIN\n")
        _git(self.root, "add", "README.md")
        _git(self.root, "commit", "-q", "-m", "main readme")
        approve_and_commit(self.cfg, self.store, item, "stub commit")
        conflicted = self.store.get(item.id)
        self.assertEqual(conflicted.status, ItemStatus.CONFLICTED)
        (wt / "README.md").write_text("# project\n\nMAIN\n")

        ok, _msg = retry_merge(self.cfg, self.store, conflicted)

        self.assertTrue(ok)
        self.assertIn(", checkout advanced", self._last_note(item.id))


class _NoLogStubRunner(StubRunner):
    """StubRunner variant that does NOT add a file under
    `docs/agent-logs/` — exercises the committer's compliance gate
    miss path."""

    def do_work(self, item, worktree):
        note_path = worktree / f".agentor-note-{item.id[:8]}.md"
        note_path.write_text("stub, no log\n")
        return "stub: no log", [str(note_path.relative_to(worktree))]


class TestAgentLogCompliance(unittest.TestCase):
    """Verifies `approve_and_commit`'s per-run findings log gate:
    a feature branch must add at least one `docs/agent-logs/*.md`
    file. Default path appends `, no agent-log written` to the
    MERGED note; `agent.require_agent_log=True` blocks by
    transitioning CONFLICTED with `last_error="agent-log missing"`."""

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

    def _claim(self, runner_cls=StubRunner):
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        runner_cls(self.cfg, self.store).run(claimed)
        return self.store.get(claimed.id)

    def _last_note(self, item_id):
        return self.store.transitions_for(item_id)[-1].note or ""

    def test_missing_log_appends_suffix(self):
        """Default knob, feature branch adds no agent-log → MERGED
        with `, no agent-log written` on the transition note."""
        item = self._claim(_NoLogStubRunner)
        approve_and_commit(self.cfg, self.store, item, "stub note")

        final = self.store.get(item.id)
        self.assertEqual(final.status, ItemStatus.MERGED)
        note = self._last_note(item.id)
        self.assertIn(", no agent-log written", note)

    def test_present_log_no_suffix(self):
        """Default StubRunner writes a log → MERGED note has no
        `no agent-log written` marker."""
        item = self._claim()
        approve_and_commit(self.cfg, self.store, item, "stub note")

        final = self.store.get(item.id)
        self.assertEqual(final.status, ItemStatus.MERGED)
        self.assertNotIn("no agent-log written", self._last_note(item.id))

    def test_require_agent_log_blocks_when_missing(self):
        """`require_agent_log=True` + no log → CONFLICTED,
        `last_error="agent-log missing"`, feature branch + worktree
        preserved, base branch untouched."""
        self.cfg.agent.require_agent_log = True
        item = self._claim(_NoLogStubRunner)
        branch = item.branch
        wt = Path(item.worktree_path)
        base_before = _main_sha(self.root)

        approve_and_commit(self.cfg, self.store, item, "stub note")

        final = self.store.get(item.id)
        self.assertEqual(final.status, ItemStatus.CONFLICTED)
        self.assertEqual(final.last_error, "agent-log missing")
        self.assertTrue(wt.exists(),
                        "worktree must be kept for the agent to add the log")
        self.assertTrue(_branch_exists(self.root, branch),
                        "feature branch must be preserved when the gate blocks")
        self.assertEqual(_main_sha(self.root), base_before,
                         "base must not advance when the gate blocks")
        self.assertIn("agent-log missing", self._last_note(item.id))

    def test_require_agent_log_allows_when_present(self):
        """`require_agent_log=True` with a log in the feature branch →
        MERGED as usual."""
        self.cfg.agent.require_agent_log = True
        item = self._claim()
        approve_and_commit(self.cfg, self.store, item, "stub note")

        final = self.store.get(item.id)
        self.assertEqual(final.status, ItemStatus.MERGED)
        self.assertNotIn("no agent-log written", self._last_note(item.id))


if __name__ == "__main__":
    unittest.main()
