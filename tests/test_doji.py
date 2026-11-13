import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import doji  # noqa: E402


class SplitQuarters(unittest.TestCase):
    def test_even_and_remainder_goes_to_trail(self):
        self.assertEqual(doji.split_quarters(8), [2, 2, 2, 2])
        self.assertEqual(doji.split_quarters(13), [3, 3, 3, 4])
        self.assertEqual(doji.split_quarters(555), [138, 138, 138, 141])

    def test_small_positions_keep_a_trailing_share(self):
        self.assertEqual(doji.split_quarters(1), [0, 0, 0, 1])
        self.assertEqual(doji.split_quarters(2), [1, 0, 0, 1])
        self.assertEqual(doji.split_quarters(3), [1, 1, 0, 1])
        self.assertEqual(doji.split_quarters(0), [0, 0, 0, 0])

    def test_parts_always_add_up(self):
        for q in range(1, 300):
            self.assertEqual(sum(doji.split_quarters(q)), q)


class StopQuantities(unittest.TestCase):
    def test_stops_never_exceed_the_position(self):
        for q in range(1, 400):
            self.assertEqual(sum(doji.stop_quantities(q)), q)

    def test_one_stop_per_target_plus_rest(self):
        self.assertEqual(doji.stop_quantities(13), [3, 3, 3, 4])
        self.assertEqual(doji.stop_quantities(3), [1, 1, 1])
        self.assertEqual(doji.stop_quantities(1), [1])


class Targets(unittest.TestCase):
    def test_targets_at_2_5_10_r_rounded_up(self):
        # NVDA 10.11. 16:55: fill 167.79, stop 167.61 -> R 0.18
        self.assertEqual(doji.target_prices(167.79, 0.18), [168.15, 168.69, 169.59])

    def test_rounding_never_sells_below_target(self):
        for p in doji.target_prices(100.0, 0.333):
            self.assertEqual(round(p, 2), p)
        self.assertGreaterEqual(doji.target_prices(100.0, 0.333)[0], 100.666)


class Ladder(unittest.TestCase):
    E, R, S = 100.0, 1.0, 99.0  # entry, R, initial stop

    def stop(self, high, current=None, trail=5.0):
        return doji.ladder_stop(self.E, self.R, high, self.S if current is None else current, trail)

    def test_below_breakeven_keeps_initial_stop(self):
        self.assertEqual(self.stop(101.4), (99.0, False))

    def test_breakeven_at_1_5_r(self):
        self.assertEqual(self.stop(101.5), (100.0, False))

    def test_stop_to_2_r_at_5_r(self):
        self.assertEqual(self.stop(105.0), (102.0, False))
        self.assertEqual(self.stop(109.9), (102.0, False))

    def test_stop_to_5_r_at_10_r_then_trail(self):
        # 10 R with a 5 R trail: both put the stop at 5 R
        self.assertEqual(self.stop(110.0), (105.0, True))
        # trail takes over above that
        self.assertEqual(self.stop(120.0), (115.0, True))
        # orange: 3 R trail
        self.assertEqual(self.stop(110.0, trail=3.0), (107.0, True))

    def test_never_moves_down(self):
        self.assertEqual(self.stop(101.0, current=104.0), (104.0, False))

    def test_zero_risk_does_nothing(self):
        self.assertEqual(doji.ladder_stop(100.0, 0.0, 120.0, 99.0, 5.0), (99.0, False))

    def test_path_simulation(self):
        # Price runs to 13 R and falls back: stop ends at the trail, 8 R.
        stop, trailing = self.S, False
        for high in [100.5, 101.6, 103, 105.2, 108, 110.5, 113.0]:
            stop, trailing = doji.ladder_stop(self.E, self.R, high, stop, 5.0)
        self.assertEqual((stop, trailing), (108.0, True))


class ExtendedExit(unittest.TestCase):
    def test_limit_below_bid(self):
        self.assertEqual(doji.extended_hours_exit_limit(200.0, 0.5), 199.0)

    def test_no_bid(self):
        self.assertIsNone(doji.extended_hours_exit_limit(0.0, 0.5))


if __name__ == "__main__":
    unittest.main()
