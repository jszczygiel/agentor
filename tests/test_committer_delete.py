"""Unit tests for `delete_idea` — the unified inspect-view delete.

The dashboard-level dispatch tests in
`test_dashboard_inspect_dispatch.py` cover the key routing and
transition outcomes; this file drills into the branch that
`_inspect_dispatch` intentionally skips with `cfg=None`: git worktree
and branch teardown when the item still holds a worktree_path."""

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

    def test_deferred_delete_transitions_to_cancelled(self):
        """Baseline: the legacy DEFERRED delete call site still lands the
        item in CANCELLED with the prior status captured in the note."""
        self._seed("d1")
        self.store.transition("d1", ItemStatus.WORKING, note="t")
        self.store.transition("d1", ItemStatus.DEFERRED, note="t")
        changed = delete_idea(None, self.store, None, self._fresh("d1"))
        self.assertTrue(changed)
        got = self.store.get("d1")
        self.assertEqual(got.status, ItemStatus.CANCELLED)
        note = self.store.transitions_for("d1")[-1].note or ""
        self.assertIn("deferred", note)

    def test_already_cancelled_short_circuits(self):
        """Repeat deletes are idempotent no-ops. `delete_idea` returns
        False and does not write another transition."""
        self._seed("c1")
        self.store.transition("c1", ItemStatus.CANCELLED, note="t")
        before = len(self.store.transitions_for("c1"))
        changed = delete_idea(None, self.store, None, self._fresh("c1"))
        self.assertFalse(changed)
        self.assertEqual(len(self.store.transitions_for("c1")), before)

    def test_worktree_cleanup_fires_when_path_present(self):
        """ERRORED items keep worktree_path/branch for forensics. Delete
        must force-remove the worktree, prune, and force-delete the
        branch — all against `config.project_root`."""
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
            changed = delete_idea(
                _cfg(self.root), self.store, self.daemon,
                self._fresh("e1"),
            )
        self.assertTrue(changed)
        rm.assert_called_once()
        _, kwargs = rm.call_args[:2], rm.call_args[1]
        self.assertTrue(rm.call_args.kwargs.get("force"))
        prune.assert_called_once_with(self.root)
        bd.assert_called_once()
        self.assertTrue(bd.call_args.kwargs.get("force"))
        got = self.store.get("e1")
        self.assertEqual(got.status, ItemStatus.CANCELLED)
        self.assertIsNone(got.worktree_path)
        self.assertIsNone(got.branch)

    def test_worktree_cleanup_skipped_when_no_path(self):
        """Vanilla QUEUED items have no worktree yet — no git calls
        should fire."""
        self._seed("q1")
        with patch.object(git_ops, "worktree_remove") as rm, \
                patch.object(git_ops, "branch_delete") as bd:
            changed = delete_idea(
                _cfg(self.root), self.store, self.daemon,
                self._fresh("q1"),
            )
        self.assertTrue(changed)
        rm.assert_not_called()
        bd.assert_not_called()
        self.assertEqual(
            self.store.get("q1").status, ItemStatus.CANCELLED,
        )

    def test_git_errors_are_swallowed(self):
        """A broken worktree registration must not block the CANCELLED
        transition — operators chose to delete, git failures are
        best-effort cleanup."""
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
            changed = delete_idea(
                _cfg(self.root), self.store, self.daemon,
                self._fresh("g1"),
            )
        self.assertTrue(changed)
        self.assertEqual(
            self.store.get("g1").status, ItemStatus.CANCELLED,
        )

    def test_working_kill_one_called_with_item_id(self):
        """WORKING teardown must signal `proc_registry.kill_one(item.id)`
        so the in-flight subprocess stops burning tokens before we write
        CANCELLED."""
        self._seed("w1")
        self.store.transition(
            "w1", ItemStatus.WORKING,
            worktree_path=None, branch="agent/w1",
            note="t",
        )
        kill_one = MagicMock(return_value=False)
        self.registry.kill_one = kill_one  # type: ignore[assignment]
        with patch("agentor.committer._DELETE_WAIT_SECONDS", 0.1):
            changed = delete_idea(
                None, self.store, self.daemon, self._fresh("w1"),
            )
        self.assertTrue(changed)
        kill_one.assert_called_once_with("w1")
        self.assertEqual(
            self.store.get("w1").status, ItemStatus.CANCELLED,
        )

    def test_working_wait_exits_early_once_runner_transitions(self):
        """If the runner's error path transitions the item out of WORKING
        before the poll budget elapses, the wait loop exits and the final
        CANCELLED write still lands (shadowing whatever the runner
        wrote)."""
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
            changed = delete_idea(
                None, self.store, self.daemon, self._fresh("w2"),
            )
        self.assertTrue(changed)
        got = self.store.get("w2")
        self.assertEqual(got.status, ItemStatus.CANCELLED)
        # Transition history records BOTH the runner's ERRORED write and
        # our CANCELLED shadow — the final status wins.
        tos = [t.to_status for t in self.store.transitions_for("w2")]
        self.assertIn(ItemStatus.ERRORED, tos)
        self.assertEqual(tos[-1], ItemStatus.CANCELLED)


class TestProcRegistryKillOne(unittest.TestCase):
    """`kill_one` is the new per-item teardown hook. These tests pin its
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
