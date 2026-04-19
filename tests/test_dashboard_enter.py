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

    def test_awaiting_plan_review_has_approve_feedback_reject_defer(self):
        keys = {k for k, _ in _ACTION_KEYS_BY_STATUS[
            ItemStatus.AWAITING_PLAN_REVIEW
        ]}
        self.assertEqual(keys, {"a", "f", "r", "s"})

    def test_awaiting_review_has_approve_reject_defer_diff(self):
        keys = {k for k, _ in _ACTION_KEYS_BY_STATUS[
            ItemStatus.AWAITING_REVIEW
        ]}
        self.assertEqual(keys, {"a", "r", "s", "v"})

    def test_conflicted_has_retry_merge_resubmit_defer(self):
        keys = {k for k, _ in _ACTION_KEYS_BY_STATUS[ItemStatus.CONFLICTED]}
        self.assertEqual(keys, {"m", "e", "s"})

    def test_errored_has_retry_and_defer(self):
        keys = {k for k, _ in _ACTION_KEYS_BY_STATUS[ItemStatus.ERRORED]}
        self.assertEqual(keys, {"a", "s"})

    def test_rejected_has_retry(self):
        keys = {k for k, _ in _ACTION_KEYS_BY_STATUS[ItemStatus.REJECTED]}
        self.assertIn("a", keys)

    def test_deferred_has_restore_and_delete(self):
        keys = {k for k, _ in _ACTION_KEYS_BY_STATUS[ItemStatus.DEFERRED]}
        self.assertEqual(keys, {"a", "x"})

    def test_backlog_legacy_has_approve_and_delete(self):
        # BACKLOG is a legacy status — new items no longer land there, but
        # stale rows must stay actionable from the unified view.
        keys = {k for k, _ in _ACTION_KEYS_BY_STATUS[ItemStatus.BACKLOG]}
        self.assertEqual(keys, {"a", "x"})

    def test_terminal_and_mid_flight_statuses_have_no_actions(self):
        for st in (
            ItemStatus.MERGED,
            ItemStatus.CANCELLED,
            ItemStatus.APPROVED,
            ItemStatus.WORKING,
            ItemStatus.QUEUED,
        ):
            with self.subTest(status=st):
                self.assertEqual(_ACTION_KEYS_BY_STATUS.get(st, []), [])

    def test_keys_do_not_collide_with_global_close(self):
        # q closes the inspect view; n advances; enter/esc close. Those
        # must never be bound to an action.
        reserved = {"q", "n"}
        for st, pairs in _ACTION_KEYS_BY_STATUS.items():
            for key, _ in pairs:
                with self.subTest(status=st, key=key):
                    self.assertNotIn(key, reserved)

    def test_approve_key_is_a_everywhere(self):
        # "a" is the primary forward action in every status that has one.
        for st, pairs in _ACTION_KEYS_BY_STATUS.items():
            if not pairs:
                continue
            keys = {k for k, _ in pairs}
            # CONFLICTED uses m/e/s — no single primary approve key.
            if st == ItemStatus.CONFLICTED:
                self.assertNotIn("a", keys)
                continue
            with self.subTest(status=st):
                self.assertIn("a", keys)


class TestInspectActionLabel(unittest.TestCase):
    def test_empty_for_view_only_status(self):
        self.assertEqual(_inspect_action_label(ItemStatus.MERGED), "")
        self.assertEqual(_inspect_action_label(ItemStatus.WORKING), "")

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
        self.assertIn("[r]eject+feedback", footer)

    def test_scroll_hint_always_present(self):
        self.assertIn("[j/k]scroll", _inspect_footer(
            ItemStatus.CONFLICTED, cycle=False,
        ))


if __name__ == "__main__":
    unittest.main()
