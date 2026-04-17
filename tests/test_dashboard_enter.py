import unittest

from agentor.dashboard.modes import _enter_route
from agentor.models import ItemStatus


class TestEnterRoute(unittest.TestCase):
    def test_backlog_goes_to_pickup(self):
        self.assertEqual(_enter_route(ItemStatus.BACKLOG), "pickup")

    def test_deferred_goes_to_pickup(self):
        self.assertEqual(_enter_route(ItemStatus.DEFERRED), "pickup")

    def test_awaiting_plan_review_goes_to_plan_review(self):
        self.assertEqual(
            _enter_route(ItemStatus.AWAITING_PLAN_REVIEW), "plan_review"
        )

    def test_awaiting_review_goes_to_code_review(self):
        self.assertEqual(
            _enter_route(ItemStatus.AWAITING_REVIEW), "code_review"
        )

    def test_queued_falls_through_to_inspect(self):
        # QUEUED is past pickup (already claimed by scheduler) — inspect is
        # the least-surprising fallback since there's no queued-specific action.
        self.assertEqual(_enter_route(ItemStatus.QUEUED), "inspect")

    def test_working_falls_through_to_inspect(self):
        self.assertEqual(_enter_route(ItemStatus.WORKING), "inspect")

    def test_terminal_and_error_states_inspect(self):
        for st in (
            ItemStatus.MERGED,
            ItemStatus.REJECTED,
            ItemStatus.CANCELLED,
            ItemStatus.ERRORED,
            ItemStatus.CONFLICTED,
        ):
            with self.subTest(status=st):
                self.assertEqual(_enter_route(st), "inspect")

    def test_every_status_has_a_route(self):
        # Sanity: no status should return an unexpected value.
        valid = {"pickup", "plan_review", "code_review", "inspect"}
        for st in ItemStatus:
            with self.subTest(status=st):
                self.assertIn(_enter_route(st), valid)


if __name__ == "__main__":
    unittest.main()
