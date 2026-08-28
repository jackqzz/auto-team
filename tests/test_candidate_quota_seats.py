import unittest

from webui import app


class CandidateQuotaSeatTests(unittest.TestCase):
    def test_prolite_is_queryable_but_codex_is_excluded(self):
        self.assertFalse(app._is_non_default_seat("prolite"))
        self.assertFalse(app._is_non_default_seat("pro_lite"))
        self.assertTrue(app._is_non_default_seat("usage_based"))
        self.assertTrue(app._is_non_default_seat("Codex席位"))


if __name__ == "__main__":
    unittest.main()
