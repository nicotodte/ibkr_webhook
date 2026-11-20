import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import weekly_tf_review as w  # noqa: E402

T0 = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc).timestamp()  # 10:00 New York
FLAT = w.flatten_ts(T0)  # 15:50 New York = 20:50 UTC


def bar(i, o, h, l, c):
    return (T0 + i * 300, o, h, l, c)


def flat_bars(n=5, price=100.0):
    return [bar(i, price, price + 0.2, price - 0.2, price) for i in range(n)]


class Simulate(unittest.TestCase):
    def sim(self, bars, events=(), entry=100.0, stop=99.0, at=T0):
        return w.simulate(at, entry, stop, list(events), bars, FLAT)

    def test_flatten_time_is_1550_new_york(self):
        self.assertEqual(datetime.fromtimestamp(FLAT, timezone.utc).strftime("%H:%M"), "20:50")

    def test_stop_hit_is_minus_one_r(self):
        bars = flat_bars(2) + [bar(2, 99.8, 99.9, 98.5, 98.9)]
        r = self.sim(bars)
        self.assertAlmostEqual(r["r"], -1.0)
        self.assertEqual(r["exit"], "Stop")

    def test_gap_below_the_stop_fills_at_the_open(self):
        bars = flat_bars(2) + [bar(2, 98.0, 98.5, 97.5, 98.0)]
        self.assertAlmostEqual(self.sim(bars)["r"], -2.0)

    def test_entry_candle_cannot_stop_out(self):
        # first candle started before the entry (T0 + 60 s) and dipped under the stop
        bars = [bar(0, 100, 100.2, 98.0, 100.0)] + flat_bars(3)[1:]
        r = self.sim(bars, at=T0 + 60)
        self.assertGreater(r["r"], -1.0)

    def test_open_position_is_sold_at_the_last_close(self):
        bars = flat_bars(3) + [bar(3, 100.5, 101.2, 100.4, 101.0)]
        r = self.sim(bars)
        self.assertAlmostEqual(r["r"], 1.0)
        self.assertEqual(r["exit"], "Glattstellung 15:50")

    def test_nothing_after_the_flatten_time(self):
        late = [(FLAT + 300, 100, 100, 90, 90)]
        self.assertIsNone(self.sim(late))

    def test_breakeven_then_stop_is_zero(self):
        ev = [{"ts": T0 + 400, "event": "stop_to_breakeven", "level": "0.843", "new_stop": None}]
        bars = flat_bars(2) + [bar(2, 100.3, 100.5, 100.2, 100.4), bar(3, 100.2, 100.3, 99.8, 99.9)]
        r = self.sim(bars, ev)
        self.assertAlmostEqual(r["r"], 0.0)
        self.assertEqual(r["exit"], "Stop auf Einstieg")

    def test_level_sells_half_and_trails_the_stop(self):
        ev = [{"ts": T0 + 650, "event": "level", "level": "0.786", "new_stop": 100.0}]
        bars = flat_bars(2) + [bar(2, 101.5, 102.2, 101.4, 102.0), bar(3, 101.0, 101.0, 99.9, 100.0)]
        r = self.sim(bars, ev)
        # half sold at 102 (+2), rest stopped at the entry (0) -> +1 R
        self.assertAlmostEqual(r["r"], 1.0)
        self.assertAlmostEqual(r["max_r"], 2.2)

    def test_exit_all_sells_the_rest(self):
        ev = [{"ts": T0 + 650, "event": "level", "level": "0.5", "new_stop": 99.5},
              {"ts": T0 + 950, "event": "exit_all", "level": "", "new_stop": None}]
        bars = flat_bars(2) + [bar(2, 102, 102.2, 101.8, 102.0), bar(3, 101, 101.2, 100.8, 101.0)]
        r = self.sim(bars, ev)
        # half at 102 (+2) and the other half at 101 (+1) -> +1.5 R
        self.assertAlmostEqual(r["r"], 1.5)
        self.assertEqual(r["exit"], "Ausstieg -0.236")

    def test_events_before_the_entry_are_ignored(self):
        ev = [{"ts": T0 - 100, "event": "exit_all", "level": "", "new_stop": None}]
        self.assertAlmostEqual(self.sim(flat_bars(3), ev)["r"], 0.0)

    def test_stop_not_below_entry_cannot_be_judged(self):
        self.assertIsNone(self.sim(flat_bars(3), stop=100.0))


class Analysis(unittest.TestCase):
    def rows(self):
        def sig(sym, tid, status, reason="", minute_utc="15:30", spread="", entry="100", stop="99"):
            return {"time_utc": f"2026-11-19T{minute_utc}:00+00:00", "symbol": sym, "event": "entry",
                    "trade_id": tid, "level": "0.95", "alert_entry": entry, "alert_stop": stop,
                    "status": status, "reason": reason, "spread_pct": spread}
        return [
            sig("AAA", "1", "submitted", spread="0.02"),                              # 10:30 NY, bought
            sig("BBB", "2", "rejected", "spread_too_wide", "16:00", "0.08"),          # 11:00 NY
            sig("CCC", "3", "ignored", "outside_trading_hours", "14:40"),             # 09:40 NY
            sig("AAA", "1", "submitted"),                                             # duplicate
        ]

    def bars_for(self, symbol):
        # every symbol: flat, then +2 % by the close
        base = datetime(2026, 11, 19, 14, 0, tzinfo=timezone.utc).timestamp()
        return [(base + i * 300, 100, 100.3, 99.9, 100.0 + min(i, 40) * 0.05) for i in range(0, 80)]

    def test_signals_are_built_once_and_replayed(self):
        sigs = w.build_signals(self.rows(), [], [{"symbol": "AAA", "trade_id": "1", "r_multiple": "1.2"}], self.bars_for)
        self.assertEqual([s["symbol"] for s in sigs], ["AAA", "BBB", "CCC"])
        self.assertTrue(sigs[0]["bought"])
        self.assertEqual(sigs[0]["real_r"], 1.2)
        self.assertEqual(sigs[2]["minute"], 9 * 60 + 40)
        self.assertTrue(all(s["r"] is not None for s in sigs))

    def test_windows_and_spread_filters(self):
        sigs = w.build_signals(self.rows(), [], [], self.bars_for)
        now = [s for s in sigs if w.in_window(s, 10 * 60, 15 * 60)]
        wide = [s for s in sigs if w.in_window(s, 9 * 60 + 30, 15 * 60)]
        self.assertEqual((len(now), len(wide)), (2, 3))
        bbb = sigs[1]
        self.assertFalse(w.spread_ok(bbb, 0.05))
        self.assertTrue(w.spread_ok(bbb, 0.10))
        self.assertTrue(w.spread_ok(bbb, None))
        self.assertTrue(w.spread_ok(sigs[2], 0.05))  # never measured counts as fine

    def test_summary_numbers(self):
        s = w.summarize([{"r": 2.0, "entry": 100, "stop": 99}, {"r": -1.0, "entry": 50, "stop": 49.5},
                         {"r": None, "entry": 1, "stop": 0.5}])
        self.assertEqual((s["n"], s["won"], s["lost"], s["win_rate"]), (2, 1, 1, 50))
        self.assertEqual(s["sum_r"], 1.0)
        self.assertEqual(s["pnl"], 100)
        self.assertEqual(s["notional"], 10000 + 10000)
        self.assertEqual(s["roi"], 0.5)

    def test_report_has_all_sections(self):
        sigs = w.build_signals(self.rows(), [], [{"symbol": "AAA", "trade_id": "1", "r_multiple": "1.2",
                                                   "net_pnl_usd": "120"}], self.bars_for)
        text = w.build_report(sigs, [{"r_multiple": "1.2", "net_pnl_usd": "120"}], "13.11.2026", "20.11.2026", [])
        for heading in ("## 1.", "## 2.", "## 3.", "## 4.", "## 5.", "## 6."):
            self.assertIn(heading, text)
        self.assertIn("Spread (Bid/Ask) zu groß", text)
        self.assertIn("außerhalb der Handelszeit", text)


if __name__ == "__main__":
    unittest.main()
