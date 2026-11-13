import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import journal  # noqa: E402


def fill(exec_id, side, qty, price, commission=0.0):
    return {"exec_id": exec_id, "side": side, "qty": qty, "price": price,
            "time": "2026-11-12T14:31:00+00:00", "commission": commission}


class MergeFills(unittest.TestCase):
    def test_adds_new_and_late_commission(self):
        known = [fill("a", "BOT", 8, 100.0)]
        self.assertTrue(journal.merge_fills(known, [fill("a", "BOT", 8, 100.0, 1.0), fill("b", "SLD", 2, 102.0)]))
        self.assertEqual(len(known), 2)
        self.assertEqual(known[0]["commission"], 1.0)
        self.assertFalse(journal.merge_fills(known, [fill("b", "SLD", 2, 102.0)]))

    def test_sold_quantity(self):
        self.assertEqual(journal.sold_quantity([fill("a", "BOT", 8, 100), fill("b", "SLD", 2, 102),
                                                fill("c", "SLD", 2, 105)]), 4)


class TradeRow(unittest.TestCase):
    def row(self, fills, **kw):
        args = dict(symbol="NVDA", tier="green", trade_id="1", opened_utc="x", alert_entry=100.0,
                    alert_stop=99.0, fill_price=100.0, initial_stop=99.0, quantity=8,
                    planned_risk_usd=8.0, high=110.0, low=99.5, reached_trail=True,
                    exit_reason="trail", fills=fills)
        args.update(kw)
        return journal.trade_row(**args)

    def test_quarters_and_trail(self):
        # 2 shares each at 2 R, 5 R, 10 R, last 2 trailed out at 7 R
        fills = [fill("e", "BOT", 8, 100.0, 1.0), fill("1", "SLD", 2, 102.0, 1.0),
                 fill("2", "SLD", 2, 105.0, 1.0), fill("3", "SLD", 2, 110.0, 1.0),
                 fill("4", "SLD", 2, 107.0, 1.0)]
        r = self.row(fills)
        self.assertEqual(r["sold_quantity"], 8)
        self.assertEqual(r["pnl_usd"], 48.0)          # 2*(2+5+10+7)
        self.assertEqual(r["commission_usd"], 5.0)
        self.assertEqual(r["net_pnl_usd"], 43.0)
        self.assertEqual(r["r_multiple"], 5.38)       # 43 / (1 * 8)
        self.assertEqual(r["max_r"], 10.0)
        self.assertEqual(r["min_r"], -0.5)

    def test_stopped_out(self):
        r = self.row([fill("e", "BOT", 8, 100.0), fill("s", "SLD", 8, 99.0)], high=100.4, reached_trail=False)
        self.assertEqual(r["pnl_usd"], -8.0)
        self.assertEqual(r["r_multiple"], -1.0)

    def test_no_fills_known(self):
        r = self.row([])
        self.assertEqual(r["pnl_usd"], 0.0)
        self.assertEqual(r["sold_quantity"], 0)


class CsvAndStats(unittest.TestCase):
    def test_append_read_stats(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "t.csv")
            for tier, r, net in [("green", 5.4, 540), ("orange", -1.0, -25), ("orange", 0.0, 0),
                                 ("yellow", 2.0, 100)]:
                journal.append_row(path, journal.TRADE_FIELDS,
                                   {"tier": tier, "r_multiple": r, "net_pnl_usd": net})
            rows = journal.read_rows(path)
            self.assertEqual(len(rows), 4)
            s = journal.stats(rows)
            self.assertEqual(s["total"]["trades"], 4)
            self.assertEqual(s["total"]["won"], 2)
            self.assertEqual(s["total"]["lost"], 1)
            self.assertEqual(s["total"]["breakeven"], 1)
            self.assertEqual(s["total"]["win_rate_pct"], 66.7)
            self.assertEqual(s["total"]["net_pnl_usd"], 615.0)
            self.assertEqual(s["orange"]["trades"], 2)
            self.assertEqual(s["green"]["avg_r"], 5.4)

    def test_missing_file(self):
        self.assertEqual(journal.read_rows("/nonexistent/x.csv"), [])


if __name__ == "__main__":
    unittest.main()
