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

    def test_changed_columns_keep_the_history_in_one_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.csv")
            journal.append_row(path, ["a", "b"], {"a": 1, "b": 2})
            journal.append_row(path, ["a", "b"], {"a": 3, "b": 4})
            journal.append_row(path, ["a", "b", "c"], {"a": 5, "b": 6, "c": 7})
            expected = [
                {"a": "1", "b": "2", "c": ""},
                {"a": "3", "b": "4", "c": ""},
                {"a": "5", "b": "6", "c": "7"},
            ]
            self.assertEqual(journal.read_rows(path), expected)
            # read_all must not count the backup a second time.
            self.assertEqual(journal.read_all(path), expected)
            baks = [f for f in os.listdir(d) if f.endswith(".bak")]
            self.assertEqual(len(baks), 1)
            self.assertEqual(len(journal.read_rows(os.path.join(d, baks[0]))), 2)
            self.assertFalse(any(f.endswith((".old", ".tmp")) for f in os.listdir(d)))

    def test_old_rows_get_no_stand_when_the_stand_column_arrives(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.csv")
            journal.append_row(path, ["a"], {"a": 1})
            journal.set_stand("abcd1234")
            try:
                journal.append_row(path, ["a", "stand"], {"a": 2})
            finally:
                journal.set_stand("")
            self.assertEqual(journal.read_rows(path), [
                {"a": "1", "stand": ""}, {"a": "2", "stand": "abcd1234"},
            ])

    def test_missing_file(self):
        self.assertEqual(journal.read_rows("/nonexistent/x.csv"), [])


class TvExport(unittest.TestCase):
    trades = [{"closed_utc": "2026-11-13T15:13:24+00:00", "symbol": "CRWD", "tier": "yellow",
               "trade_id": "1794582721841", "fill_price": "222.54", "initial_stop": "221.9",
               "avg_exit_price": "221.5342", "r_multiple": "-1.68", "net_pnl_usd": "-83.81", "max_r": "0.58"}]
    signals = [
        {"symbol": "CRWD", "tier": "yellow", "trade_id": "1794582721841", "status": "submitted", "reason": ""},
        {"symbol": "XLK", "tier": "yellow", "trade_id": "1794582848103", "status": "rejected", "reason": "order_not_filled"},
    ]
    opened = [{"symbol": "GLW", "tier": "green", "trade_id": "1794585960382", "entry": 110.81, "stop": 110.48}]

    def test_records(self):
        text = journal.tv_export(self.trades, self.signals, self.opened)
        recs = text.split(";")
        self.assertEqual(len(recs), 3)
        # Doji candle 15:11 UTC: alert fired 15:12:01.8
        self.assertEqual(recs[0], "T,CRWD,y,1794582660,222.54,221.9,1794582804,221.5342,-1.68,-83.81,0.58")
        self.assertTrue(recs[1].startswith("S,XLK,y,"))
        self.assertTrue(recs[1].endswith("rejected:order_not_filled"))
        self.assertEqual(recs[2], "O,GLW,g,1794585900,110.8100,110.4800")

    def test_cut_from_the_oldest_end(self):
        text = journal.tv_export(self.trades, self.signals, self.opened, max_chars=40)
        self.assertTrue(text.startswith("O,GLW"))


if __name__ == "__main__":
    unittest.main()
