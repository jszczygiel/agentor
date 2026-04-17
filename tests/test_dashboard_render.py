import unittest

from agentor.dashboard.render import ACTIONS


class TestActionsHint(unittest.TestCase):
    def test_unpause_not_advertised(self):
        self.assertNotIn("[u]npause", ACTIONS)
        self.assertNotIn("unpause", ACTIONS)

    def test_core_actions_present(self):
        for key in ("[p]ickup", "[r]eview", "[d]eferred", "[i]nspect",
                    "[tab]filter", "[+/-]pool", "[m]ode", "[q]uit"):
            self.assertIn(key, ACTIONS)

    def test_double_space_separators(self):
        # single-space separators between words would compress the layout
        # and mislead operators about which tokens are grouped.
        self.assertNotIn("] [", ACTIONS)


if __name__ == "__main__":
    unittest.main()
