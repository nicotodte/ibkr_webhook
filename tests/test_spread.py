import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from spread import QuoteSample, assess_entry_quotes  # noqa: E402


class DojiWithoutSpreadChecks(unittest.TestCase):
    # MSTR 13.11. 15:31: bid 111.71, ask 111.87, entry 111.83, stop 111.62
    def test_wide_spread_passes_when_checks_are_off(self):
        q = assess_entry_quotes([QuoteSample(111.71, 111.87)], 111.83, 111.62, 0.0, 0.0, 0.0)
        self.assertTrue(q.ok, q.reason)

    def test_same_quote_is_rejected_with_tf_limits(self):
        q = assess_entry_quotes([QuoteSample(111.71, 111.87)], 111.83, 111.62, 0.05, 25.0, 25.0)
        self.assertFalse(q.ok)
        self.assertEqual(q.reason, "spread_too_wide")

    def test_ask_far_above_entry_still_buys(self):
        q = assess_entry_quotes([QuoteSample(112.40, 112.50)], 111.83, 111.62, 0.0, 0.0, 0.0)
        self.assertTrue(q.ok, q.reason)

    def test_ask_at_or_below_stop_never_buys(self):
        q = assess_entry_quotes([QuoteSample(111.50, 111.60)], 111.83, 111.62, 0.0, 0.0, 0.0)
        self.assertFalse(q.ok)
        self.assertEqual(q.reason, "price_at_or_below_stop")


if __name__ == "__main__":
    unittest.main()
