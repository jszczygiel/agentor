"""Unit tests for `delete_idea` — the unified inspect-view delete.

The dashboard-level dispatch tests in
`test_dashboard_inspect_dispatch.py` cover the key routing and
end-to-end outcomes; this file drills into the branch that
`_inspect_dispatch` intentionally skips with `cfg=None`: git worktree
and branch teardown when the item still holds a worktree_path, and the
WORKING-wait semantics around the runner-race window."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agentor import git_ops
from agentor.committer import delete_idea
from agentor.models import Item, ItemStatus
from agentor.runner import ProcRegistry
from agentor.store import Store


def _mk(id: str) -> Item:
    return Item(
        id=id, title="t", body="b",
        source_file="backlog.md", source_line=1, tags={},
    )


class _FakeDaemon:
    def __init__(self, registry: ProcRegistry) -> None:
        self.proc_registry = registry


def _cfg(root: Path) -> SimpleNamespace:
    """Minimal `Config` stub — `delete_idea` only accesses
    `config.project_root`."""
    return SimpleNamespace(project_root=root)


class TestDeleteIdea(unittest.TestCase):
    def setUp(self) -> None:
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        self.store = Store(self.root / "state.db")
        self.registry = ProcRegistry()
        self.daemon = _FakeDaemon(self.registry)

    def tearDown(self) -> None:
        self.store.close()
        self.td.cleanup()

    def _seed(self, id: str) -> None:
        self.store.upsert_discovered(_mk(id))

    def _fresh(self, id: str):
        got = self.store.get(id)
        assert got is not None
        return got

    def test_deferred_delete_hard_deletes_and_tombstones(self):
        """Baseline: DEFERRED → hard-delete. Row + transitions gone,
        tombstone recorded with the prior status captured in the note so
        the scanner can't resurrect the id."""
        self._seed("d1")
        self.store.transition("d1", ItemStatus.WORKING, note="t")
        self.store.transition("d1", ItemStatus.DEFERRED, note="t")
        deleted = delete_idea(None, self.store, None, self._fresh("d1"))
        self.assertTrue(deleted)
        self.assertIsNone(self.store.get("d1"))
        self.assertTrue(self.store.is_deleted("d1"))
        row = self.store.conn.execute(
            "SELECT note, last_status FROM deletions WHERE item_id = ?",
            ("d1",),
        ).fetchone()
        self.assertEqual(row["last_status"], ItemStatus.DEFERRED.value)
        self.assertEqual(row["note"], "deleted from deferred")

    def test_already_tombstoned_short_circuits(self):
        """Pre-tombstoned id: `delete_idea` returns False and does not
        retry `store.delete_item` (which would raise KeyError)."""
        self._seed("c1")
        stale = self._fresh("c1")
        self.store.delete_item("c1", note="pre")
        deleted = delete_idea(None, self.store, None, stale)
        self.assertFalse(deleted)
        self.assertIsNone(self.store.get("c1"))

    def test_worktree_cleanup_fires_when_path_present(self):
        """ERRORED items keep worktree_path/branch for forensics. Delete
        must force-remove the worktree, prune, and force-delete the
        branch — all against `config.project_root` — before the hard
        delete writes the tombstone."""
        self._seed("e1")
        self.store.transition(
            "e1", ItemStatus.WORKING,
            worktree_path="/tmp/does-not-exist",
            branch="agent/e1-abc",
            note="t",
        )
        self.store.transition(
            "e1", ItemStatus.ERRORED, last_error="boom", note="t",
        )
        with patch.object(git_ops, "worktree_remove") as rm, \
                patch.object(git_ops, "worktree_prune") as prune, \
                patch.object(git_ops, "branch_delete") as bd:
            deleted = delete_idea(
                _cfg(self.root), self.store, self.daemon,
                self._fresh("e1"),
            )
        self.assertTrue(deleted)
        rm.assert_called_once()
        self.assertTrue(rm.call_args.kwargs.get("force"))
        prune.assert_called_once_with(self.root)
        bd.assert_called_once()
        self.assertTrue(bd.call_args.kwargs.get("force"))
        self.assertIsNone(self.store.get("e1"))
        self.assertTrue(self.store.is_deleted("e1"))

    def test_worktree_cleanup_skipped_when_no_path(self):
        """Vanilla QUEUED items have no worktree yet — no git calls
        should fire, but the hard delete still lands."""
        self._seed("q1")
        with patch.object(git_ops, "worktree_remove") as rm, \
                patch.object(git_ops, "branch_delete") as bd:
            deleted = delete_idea(
                _cfg(self.root), self.store, self.daemon,
                self._fresh("q1"),
            )
        self.assertTrue(deleted)
        rm.assert_not_called()
        bd.assert_not_called()
        self.assertIsNone(self.store.get("q1"))
        self.assertTrue(self.store.is_deleted("q1"))

    def test_git_errors_are_swallowed(self):
        """A broken worktree registration must not block the hard
        delete — operators chose to delete, git failures are best-effort
        cleanup."""
        self._seed("g1")
        self.store.transition(
            "g1", ItemStatus.WORKING,
            worktree_path="/tmp/broken",
            branch="agent/g1",
            note="t",
        )
        self.store.transition(
            "g1", ItemStatus.AWAITING_REVIEW, note="t",
        )
        with patch.object(
            git_ops, "worktree_remove",
            side_effect=git_ops.GitError("broken"),
        ), patch.object(git_ops, "worktree_prune"), patch.object(
            git_ops, "branch_delete",
            side_effect=git_ops.GitError("gone"),
        ):
            deleted = delete_idea(
                _cfg(self.root), self.store, self.daemon,
                self._fresh("g1"),
            )
        self.assertTrue(deleted)
        self.assertIsNone(self.store.get("g1"))
        self.assertTrue(self.store.is_deleted("g1"))

    def test_working_kill_one_called_with_item_id(self):
        """WORKING teardown must signal `proc_registry.kill_one(item.id)`
        so the in-flight subprocess stops burning tokens before the
        hard-delete runs."""
        self._seed("w1")
        self.store.transition(
            "w1", ItemStatus.WORKING,
            worktree_path=None, branch="agent/w1",
            note="t",
        )
        kill_one = MagicMock(return_value=False)
        self.registry.kill_one = kill_one  # type: ignore[assignment]
        with patch("agentor.committer._DELETE_WAIT_SECONDS", 0.1):
            deleted = delete_idea(
                None, self.store, self.daemon, self._fresh("w1"),
            )
        self.assertTrue(deleted)
        kill_one.assert_called_once_with("w1")
        self.assertIsNone(self.store.get("w1"))

    def test_working_wait_exits_early_once_runner_transitions(self):
        """If the runner's error path transitions the item out of
        WORKING before the poll budget elapses, the wait loop exits
        promptly. The hard-delete then lands (clearing even the
        runner-written ERRORED row via the tombstone path)."""
        self._seed("w2")
        self.store.transition(
            "w2", ItemStatus.WORKING, note="t",
        )

        # Simulate the runner's error path writing ERRORED between our
        # kill and the first poll tick.
        def _flip_status(_key: str) -> bool:
            self.store.transition(
                "w2", ItemStatus.ERRORED,
                last_error="killed", note="runner",
            )
            return True

        self.registry.kill_one = _flip_status  # type: ignore[assignment]
        with patch("agentor.committer._DELETE_WAIT_SECONDS", 5.0), \
                patch("agentor.committer._DELETE_POLL_INTERVAL", 0.01):
            deleted = delete_idea(
                None, self.store, self.daemon, self._fresh("w2"),
            )
        self.assertTrue(deleted)
        self.assertIsNone(self.store.get("w2"))
        self.assertTrue(self.store.is_deleted("w2"))

    def test_working_wait_returns_when_row_vanishes(self):
        """Edge: a concurrent `store.delete_item` removes the row during
        the wait loop. `delete_idea` should notice the row is gone and
        report the no-op (False) without raising."""
        self._seed("w3")
        self.store.transition("w3", ItemStatus.WORKING, note="t")

        def _concurrent_delete(_key: str) -> bool:
            self.store.delete_item("w3", note="concurrent")
            return True

        self.registry.kill_one = _concurrent_delete  # type: ignore[assignment]
        with patch("agentor.committer._DELETE_WAIT_SECONDS", 1.0), \
                patch("agentor.committer._DELETE_POLL_INTERVAL", 0.01):
            deleted = delete_idea(
                None, self.store, self.daemon, self._fresh("w3"),
            )
        # Row is already gone — result is False, not True (we didn't
        # delete it, the concurrent writer did).
        self.assertFalse(deleted)
        self.assertIsNone(self.store.get("w3"))


class TestProcRegistryKillOne(unittest.TestCase):
    """`kill_one` is the per-item teardown hook. These tests pin its
    behavior on registered/unregistered/already-exited keys so operator
    delete doesn't hang on phantom entries."""

    def test_kill_one_missing_key_returns_false(self):
        reg = ProcRegistry()
        self.assertFalse(reg.kill_one("never-registered"))

    def test_kill_one_already_exited_returns_false(self):
        """A subprocess that has already completed should be popped and
        reported False without re-signalling."""
        reg = ProcRegistry()
        fake = MagicMock()
        fake.poll.return_value = 0  # already exited
        reg._procs["k"] = fake  # type: ignore[assignment]
        self.assertFalse(reg.kill_one("k"))
        self.assertNotIn("k", reg._procs)
        fake.wait.assert_not_called()

    def test_kill_one_signals_and_removes_live_proc(self):
        reg = ProcRegistry()
        fake = MagicMock()
        fake.poll.side_effect = [None]  # live on entry
        fake.pid = 99999  # unused by the patched signal path
        reg._procs["k"] = fake  # type: ignore[assignment]
        with patch("agentor.runner._signal_group") as sg:
            result = reg.kill_one("k")
        self.assertTrue(result)
        sg.assert_called_once()
        self.assertNotIn("k", reg._procs)


if __name__ == "__main__":
    unittest.main()
