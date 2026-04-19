import unittest

from agentor.dashboard.modes import (
    _ACTION_KEYS_BY_STATUS,
    _inspect_action_label,
    _inspect_footer,
)
from agentor.models import ItemStatus


class TestInspectActionMap(unittest.TestCase):
    """The unified inspect view exposes a status-gated action set instead
    of routing Enter through mode-specific screens. These tests pin the
    action surface for each status so a regression that silently drops an
    action is caught."""

    def test_queued_has_feedback_reject_defer_delete(self):
        keys = {k for k, _ in _ACTION_KEYS_BY_STATUS[ItemStatus.QUEUED]}
        self.assertEqual(keys, {"f", "r", "s", "x"})

    def test_awaiting_plan_review_has_approve_feedback_defer_delete(self):
        keys = {k for k, _ in _ACTION_KEYS_BY_STATUS[
            ItemStatus.AWAITING_PLAN_REVIEW
        ]}
        self.assertEqual(keys, {"a", "r", "s", "x"})

    def test_awaiting_review_has_approve_reject_defer_diff(self):
        keys = {k for k, _ in _ACTION_KEYS_BY_STATUS[
            ItemStatus.AWAITING_REVIEW
        ]}
        self.assertEqual(keys, {"a", "r", "s", "v", "x"})

    def test_conflicted_has_retry_merge_resubmit_defer(self):
        keys = {k for k, _ in _ACTION_KEYS_BY_STATUS[ItemStatus.CONFLICTED]}
        self.assertEqual(keys, {"m", "e", "s", "x"})

    def test_errored_has_retry_and_defer(self):
        keys = {k for k, _ in _ACTION_KEYS_BY_STATUS[ItemStatus.ERRORED]}
        self.assertEqual(keys, {"a", "s", "x"})

    def test_rejected_has_retry(self):
        keys = {k for k, _ in _ACTION_KEYS_BY_STATUS[ItemStatus.REJECTED]}
        self.assertIn("a", keys)

    def test_deferred_has_restore_and_delete(self):
        keys = {k for k, _ in _ACTION_KEYS_BY_STATUS[ItemStatus.DEFERRED]}
        self.assertEqual(keys, {"a", "x"})

    def test_delete_is_bound_on_every_status(self):
        """`x` is the unified delete action — every lifecycle state must
        expose it so operators can remove any item from the inspect view."""
        for st in ItemStatus:
            with self.subTest(status=st):
                keys = {k for k, _ in _ACTION_KEYS_BY_STATUS.get(st, [])}
                self.assertIn("x", keys)

    def test_view_only_statuses_have_only_delete(self):
        """WORKING and the terminal success states expose only `x`; other
        actions (approve/defer/merge/etc.) aren't meaningful there.
        QUEUED is excluded — operators can reject/feedback/defer a
        freshly discovered item before the daemon picks it up."""
        for st in (
            ItemStatus.WORKING,
            ItemStatus.APPROVED,
            ItemStatus.MERGED,
            ItemStatus.CANCELLED,
        ):
            with self.subTest(status=st):
                keys = {k for k, _ in _ACTION_KEYS_BY_STATUS.get(st, [])}
                self.assertEqual(keys, {"x"})

    def test_keys_do_not_collide_with_global_close(self):
        # q closes the inspect view; n advances; enter/esc close. Those
        # must never be bound to an action.
        reserved = {"q", "n"}
        for st, pairs in _ACTION_KEYS_BY_STATUS.items():
            for key, _ in pairs:
                with self.subTest(status=st, key=key):
                    self.assertNotIn(key, reserved)

    def test_approve_key_is_a_where_an_approve_action_exists(self):
        # "a" is the primary forward action in every status that has a
        # non-delete action (approve / retry / restore). Statuses whose
        # only action is `[x]delete` (WORKING, APPROVED, MERGED, CANCELLED)
        # are excluded, as are CONFLICTED (uses m/e/s instead) and QUEUED
        # (item is already destined for dispatch — reject/feedback/defer
        # are the meaningful ops, not a redundant approve).
        delete_only = {"x"}
        no_approve = {ItemStatus.CONFLICTED, ItemStatus.QUEUED}
        for st, pairs in _ACTION_KEYS_BY_STATUS.items():
            keys = {k for k, _ in pairs}
            if not keys or keys == delete_only:
                continue
            if st in no_approve:
                self.assertNotIn("a", keys)
                continue
            with self.subTest(status=st):
                self.assertIn("a", keys)


class TestInspectActionLabel(unittest.TestCase):
    def test_delete_only_status_label_contains_delete(self):
        """Statuses whose only action is `[x]delete` render that label
        alone in the footer."""
        for st in (ItemStatus.MERGED, ItemStatus.WORKING):
            with self.subTest(status=st):
                self.assertEqual(
                    _inspect_action_label(st), "[x]delete",
                )

    def test_lists_all_labels_for_awaiting_review(self):
        label = _inspect_action_label(ItemStatus.AWAITING_REVIEW)
        for token in ("[a]approve+merge", "[r]eject+feedback",
                      "[s]defer", "[v]diff"):
            self.assertIn(token, label)


class TestInspectFooter(unittest.TestCase):
    def test_non_cycle_uses_close_hint(self):
        footer = _inspect_footer(ItemStatus.MERGED, cycle=False)
        self.assertIn("[q/enter]close", footer)
        self.assertNotIn("[n]ext", footer)

    def test_cycle_uses_next_quit_hint(self):
        footer = _inspect_footer(ItemStatus.AWAITING_REVIEW, cycle=True)
        self.assertIn("[n]ext", footer)
        self.assertIn("[q]uit", footer)

    def test_footer_includes_action_labels(self):
        footer = _inspect_footer(
            ItemStatus.AWAITING_PLAN_REVIEW, cycle=False
        )
        self.assertIn("[a]approve→execute", footer)
        self.assertIn("[r]feedback", footer)
        self.assertNotIn("[f]approve+feedback", footer)

    def test_scroll_hint_always_present(self):
        self.assertIn("[j/k]scroll", _inspect_footer(
            ItemStatus.CONFLICTED, cycle=False,
        ))


if __name__ == "__main__":
    unittest.main()
