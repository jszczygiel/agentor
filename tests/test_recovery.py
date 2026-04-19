import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agentor.config import (AgentConfig, Config, GitConfig, ParsingConfig,
                            ReviewConfig, SourcesConfig)
from agentor.models import Item, ItemStatus
from agentor.recovery import (RecoveryResult, _STALE_SESSION_MARKER,
                              _is_auto_recoverable_error, recover_on_startup)
from agentor.store import Store


def _mk_item(id: str, title: str = "T", body: str = "B") -> Item:
    return Item(
        id=id, title=title, body=body,
        source_file="backlog.md", source_line=1,
        tags={},
    )


def _mk_config(root: Path) -> Config:
    return Config(
        project_name="t",
        project_root=root,
        sources=SourcesConfig(),
        parsing=ParsingConfig(),
        agent=AgentConfig(),
        git=GitConfig(),
        review=ReviewConfig(),
    )


class TestAutoRecoverablePatterns(unittest.TestCase):
    def test_none_and_empty_return_false(self):
        self.assertFalse(_is_auto_recoverable_error(None))
        self.assertFalse(_is_auto_recoverable_error(""))

    def test_shutdown_matches(self):
        self.assertTrue(_is_auto_recoverable_error("agentor shutdown before dispatch"))

    def test_case_insensitive(self):
        self.assertTrue(_is_auto_recoverable_error("Not A Git Repository"))

    def test_known_infra_classes_match(self):
        for msg in [
            "max_cost_usd: 5.0 exceeded",
            "no conversation found with session id abc",
            "no agent result yet",
            "no token data",
            "fatal: invalid reference: main",
            "fatal: bad object deadbeef",
            "fatal: bad revision",
            "branch already exists",
            "is already checked out at /tmp/foo",
            "path already used by worktree /bar",
            "not a working tree",
        ]:
            self.assertTrue(_is_auto_recoverable_error(msg), msg)

    def test_unknown_error_returns_false(self):
        self.assertFalse(_is_auto_recoverable_error("permission denied"))
        self.assertFalse(_is_auto_recoverable_error("disk full"))


class TestRecoveryWorkingItems(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        self.store = Store(self.root / ".agentor" / "state.db")
        self.config = _mk_config(self.root)
        # Patch worktree_remove: no real git repo, no need to shell out.
        self.patcher = patch("agentor.recovery.git_ops.worktree_remove")
        self.mock_wt_remove = self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.store.close()
        self.td.cleanup()

    def _seed_working(
        self, id: str, session_id: str | None = None,
        worktree_path: str | None = None, prior_status: ItemStatus | None = None,
    ) -> None:
        """Seed an item in WORKING state with optional session_id / worktree."""
        self.store.upsert_discovered(_mk_item(id))
        self.store.transition(id, ItemStatus.QUEUED, note="promote")
        if prior_status and prior_status != ItemStatus.QUEUED:
            self.store.transition(id, ItemStatus.WORKING, note="claim")
            self.store.transition(id, prior_status, note="reach settled")
        self.store.transition(
            id, ItemStatus.WORKING,
            worktree_path=worktree_path, branch="br",
            session_id=session_id, note="claim",
        )

    def test_live_session_demoted_to_queued_preserves_fields(self):
        wt = self.root / "wt-live"
        wt.mkdir()
        self._seed_working("a", session_id="sess-1", worktree_path=str(wt))
        result = recover_on_startup(self.config, self.store)
        item = self.store.get("a")
        self.assertEqual(item.status, ItemStatus.QUEUED)
        self.assertEqual(item.session_id, "sess-1")
        self.assertEqual(item.worktree_path, str(wt))
        self.assertEqual(item.branch, "br")
        self.assertEqual(item.attempts, 0)  # reset for resume
        self.assertEqual(len(result.resumable), 1)
        self.assertEqual(result.resumable[0].id, "a")
        self.assertEqual(result.requeued, [])
        self.mock_wt_remove.assert_not_called()

    def test_dead_session_worktree_gone_reverts_to_queued(self):
        self._seed_working("a", session_id="sess-1",
                           worktree_path=str(self.root / "gone"))
        result = recover_on_startup(self.config, self.store)
        item = self.store.get("a")
        self.assertEqual(item.status, ItemStatus.QUEUED)
        self.assertIsNone(item.session_id)
        self.assertIsNone(item.worktree_path)
        self.assertIsNone(item.branch)
        self.assertEqual(result.requeued, ["a"])
        self.assertEqual(result.resumable, [])
        # worktree_remove called to force cleanup, even though dir missing.
        self.mock_wt_remove.assert_called_once()

    def test_no_session_id_reverts_even_if_worktree_exists(self):
        wt = self.root / "orphan-wt"
        wt.mkdir()
        self._seed_working("a", session_id=None, worktree_path=str(wt))
        recover_on_startup(self.config, self.store)
        item = self.store.get("a")
        self.assertEqual(item.status, ItemStatus.QUEUED)
        self.assertIsNone(item.worktree_path)
        self.mock_wt_remove.assert_called_once()

    def test_revert_to_awaiting_plan_review_when_prior_settled(self):
        """Item reached AWAITING_PLAN_REVIEW, user approved it, crashed mid-execute.
        Recovery restores user-visible progress rather than forcing re-approval."""
        wt = self.root / "gone"
        self._seed_working(
            "a", session_id=None, worktree_path=str(wt),
            prior_status=ItemStatus.AWAITING_PLAN_REVIEW,
        )
        recover_on_startup(self.config, self.store)
        item = self.store.get("a")
        self.assertEqual(item.status, ItemStatus.AWAITING_PLAN_REVIEW)

    def test_no_worktree_path_skips_remove(self):
        self._seed_working("a", session_id=None, worktree_path=None)
        recover_on_startup(self.config, self.store)
        self.assertEqual(self.store.get("a").status, ItemStatus.QUEUED)
        self.mock_wt_remove.assert_not_called()


class TestRecoveryAutoRecoveredErrors(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        self.store = Store(self.root / ".agentor" / "state.db")
        self.config = _mk_config(self.root)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _seed(self, id: str, status: ItemStatus, last_error: str | None) -> None:
        self.store.upsert_discovered(_mk_item(id))
        if status != ItemStatus.QUEUED:
            self.store.transition(id, status, last_error=last_error)
        else:
            # Self-loop transition so the seed item can carry last_error
            # without jumping off QUEUED.
            self.store.transition(id, ItemStatus.QUEUED, last_error=last_error)

    def test_benign_error_cleared_status_unchanged(self):
        self._seed("a", ItemStatus.QUEUED, "agentor shutdown")
        result = recover_on_startup(self.config, self.store)
        item = self.store.get("a")
        self.assertIsNone(item.last_error)
        self.assertEqual(item.status, ItemStatus.QUEUED)
        self.assertEqual(item.attempts, 0)
        self.assertIn("a", result.auto_recovered)

    def test_non_benign_error_kept(self):
        self._seed("a", ItemStatus.QUEUED, "permission denied on /etc/shadow")
        result = recover_on_startup(self.config, self.store)
        item = self.store.get("a")
        self.assertEqual(item.last_error, "permission denied on /etc/shadow")
        self.assertEqual(result.auto_recovered, [])

    def test_terminal_merged_with_stale_error_cleared(self):
        """MERGED item with any last_error — noise regardless of class."""
        self._seed("a", ItemStatus.QUEUED, "weird unrelated error")
        self.store.transition(
            "a", ItemStatus.WORKING, last_error="weird unrelated error")
        self.store.transition(
            "a", ItemStatus.AWAITING_REVIEW, last_error="weird unrelated error")
        self.store.transition(
            "a", ItemStatus.MERGED, last_error="weird unrelated error")
        result = recover_on_startup(self.config, self.store)
        item = self.store.get("a")
        self.assertIsNone(item.last_error)
        self.assertIn("a", result.auto_recovered)

    def test_rejected_with_benign_error_cleared(self):
        self._seed("a", ItemStatus.QUEUED, "max_cost_usd: 5 exceeded")
        self.store.transition(
            "a", ItemStatus.REJECTED, last_error="max_cost_usd: 5 exceeded")
        result = recover_on_startup(self.config, self.store)
        self.assertIsNone(self.store.get("a").last_error)
        self.assertIn("a", result.auto_recovered)

    def test_deferred_with_benign_error_cleared(self):
        self._seed("a", ItemStatus.QUEUED, "not a git repository")
        self.store.transition(
            "a", ItemStatus.DEFERRED, last_error="not a git repository")
        recover_on_startup(self.config, self.store)
        self.assertIsNone(self.store.get("a").last_error)

    def test_item_with_no_error_untouched(self):
        self._seed("a", ItemStatus.QUEUED, None)
        result = recover_on_startup(self.config, self.store)
        self.assertEqual(result.auto_recovered, [])

    def test_returns_empty_result_when_store_empty(self):
        result = recover_on_startup(self.config, self.store)
        self.assertIsInstance(result, RecoveryResult)
        self.assertEqual(result.requeued, [])
        self.assertEqual(result.resumable, [])
        self.assertEqual(result.auto_recovered, [])


class TestRecoveryStaleSession(unittest.TestCase):
    """Recovery sweep should fast-fail WORKING items whose persisted Claude
    session is presumed dead — either because the WORKING claim is older
    than the configured age threshold, or because the most recent failure
    row matches the dead-session signature. Demoting up front avoids the
    ~$0.50 round-trip of `claude --resume <expired-id>`."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        self.store = Store(self.root / ".agentor" / "state.db")
        self.config = _mk_config(self.root)
        self.patcher = patch("agentor.recovery.git_ops.worktree_remove")
        self.mock_wt_remove = self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.store.close()
        self.td.cleanup()

    def _seed_working(
        self, id: str, session_id: str | None = None,
        worktree_path: str | None = None,
    ) -> None:
        self.store.upsert_discovered(_mk_item(id))
        self.store.transition(id, ItemStatus.QUEUED, note="promote")
        self.store.transition(
            id, ItemStatus.WORKING,
            worktree_path=worktree_path, branch="br",
            session_id=session_id, note="claim",
        )

    def _backdate_working_transition(self, id: str, seconds_ago: float) -> None:
        """Rewrite the latest WORKING transition row's `at` timestamp so the
        recovery sweep's age check sees a stale claim. Touches the test DB
        directly because production code never backdates transitions."""
        target = time.time() - seconds_ago
        self.store.conn.execute(
            """UPDATE transitions SET at = ? WHERE id = (
                   SELECT id FROM transitions
                   WHERE item_id = ? AND to_status = ?
                   ORDER BY id DESC LIMIT 1
               )""",
            (target, id, ItemStatus.WORKING.value),
        )

    def test_old_session_demoted_to_fresh_plan(self):
        wt = self.root / "wt-old"
        wt.mkdir()
        self._seed_working("a", session_id="sess-1", worktree_path=str(wt))
        self._backdate_working_transition("a", seconds_ago=5 * 3600)

        result = recover_on_startup(self.config, self.store)

        item = self.store.get("a")
        self.assertEqual(item.status, ItemStatus.QUEUED)
        self.assertIsNone(item.session_id)
        self.assertIsNone(item.worktree_path)
        self.assertIsNone(item.branch)
        self.assertEqual(item.attempts, 0)
        self.assertEqual(item.last_error, _STALE_SESSION_MARKER)
        self.assertEqual(result.stale_sessions, ["a"])
        self.assertEqual(result.resumable, [])
        self.assertEqual(result.requeued, [])

    def test_recent_session_with_dead_session_failure_demoted(self):
        wt = self.root / "wt-recent"
        wt.mkdir()
        self._seed_working("a", session_id="sess-1", worktree_path=str(wt))
        # Fresh claim (no backdating) — the failure row alone must trigger demotion.
        self.store.record_failure(
            item_id="a", attempt=1, phase="execute",
            error="claude exited 1: No conversation found with session ID sess-1",
            error_sig="claudeexited:noconversationfoundwithsessionidsess-",
        )

        result = recover_on_startup(self.config, self.store)

        item = self.store.get("a")
        self.assertEqual(item.status, ItemStatus.QUEUED)
        self.assertIsNone(item.session_id)
        self.assertEqual(item.last_error, _STALE_SESSION_MARKER)
        self.assertEqual(result.stale_sessions, ["a"])
        self.assertEqual(result.resumable, [])

    def test_recent_session_no_failure_remains_resumable(self):
        """Regression guard: the existing resumable path still applies when
        nothing flags the session as dead."""
        wt = self.root / "wt-fresh"
        wt.mkdir()
        self._seed_working("a", session_id="sess-1", worktree_path=str(wt))

        result = recover_on_startup(self.config, self.store)

        item = self.store.get("a")
        self.assertEqual(item.status, ItemStatus.QUEUED)
        self.assertEqual(item.session_id, "sess-1")
        self.assertEqual(item.worktree_path, str(wt))
        self.assertEqual(result.stale_sessions, [])
        self.assertEqual(len(result.resumable), 1)

    def test_threshold_configurable_via_config(self):
        wt = self.root / "wt-low-threshold"
        wt.mkdir()
        self._seed_working("a", session_id="sess-1", worktree_path=str(wt))
        # Push the threshold below the fresh claim's age (a few ms is enough).
        self.config.agent.session_max_age_hours = 0.0001
        # Make sure the WORKING transition is at least a few seconds old so
        # the age comparison clears the tiny threshold.
        self._backdate_working_transition("a", seconds_ago=10)

        result = recover_on_startup(self.config, self.store)

        self.assertEqual(result.stale_sessions, ["a"])
        self.assertEqual(self.store.get("a").session_id, None)

    def test_unrelated_failure_row_does_not_demote(self):
        """A non-dead-session failure row must not trigger the stale path."""
        wt = self.root / "wt-other-fail"
        wt.mkdir()
        self._seed_working("a", session_id="sess-1", worktree_path=str(wt))
        self.store.record_failure(
            item_id="a", attempt=1, phase="execute",
            error="claude exited 1: rate limited",
            error_sig="claudeexited:ratelimited",
        )

        result = recover_on_startup(self.config, self.store)

        self.assertEqual(result.stale_sessions, [])
        self.assertEqual(len(result.resumable), 1)
        self.assertEqual(self.store.get("a").session_id, "sess-1")

    def test_marker_clears_on_next_sweep(self):
        """The single-tick marker must be in `_AUTO_RECOVERABLE_PATTERNS`
        so the next startup wipes it without operator intervention."""
        self.assertTrue(_is_auto_recoverable_error(_STALE_SESSION_MARKER))


if __name__ == "__main__":
    unittest.main()
