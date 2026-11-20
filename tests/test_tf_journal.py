import asyncio
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

_tmp = tempfile.mkdtemp()
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ["STATE_FILE"] = os.path.join(_tmp, "positions.json")
os.environ["TF_TRADES_FILE"] = os.path.join(_tmp, "tf_trades.csv")
os.environ["TF_SIGNALS_FILE"] = os.path.join(_tmp, "tf_signals.csv")
os.environ["TF_LADDER_FILE"] = os.path.join(_tmp, "tf_ladder.csv")

import journal  # noqa: E402
import tf_report  # noqa: E402


def fill(exec_id, side, qty, price, commission=0.0):
    return {"exec_id": exec_id, "side": side, "qty": qty, "price": price,
            "time": "2026-11-12T14:31:00+00:00", "commission": commission}


class TradeRow(unittest.TestCase):
    def row(self, fills, **kw):
        args = dict(symbol="NVS", trade_id="1", opened_utc="2026-11-12T14:30:00+00:00",
                    entry_level="0.95", alert_entry=100.0, alert_stop=99.0, fill_price=100.0,
                    quantity=100, high=103.0, low=99.6, levels_hit=["0.786"],
                    exit_reason="exit_all", fills=fills)
        args.update(kw)
        return journal.tf_trade_row(**args)

    def test_winner(self):
        r = self.row([fill("e", "BOT", 100, 100.0, 1.0), fill("s", "SLD", 100, 101.0, 1.0)])
        self.assertEqual(r["pnl_usd"], 100.0)
        self.assertEqual(r["net_pnl_usd"], 98.0)
        self.assertEqual(r["risk_usd"], 100.0)
        self.assertEqual(r["r_multiple"], 0.98)
        self.assertEqual(r["result"], "gewonnen")
        self.assertEqual(r["max_r"], 3.0)
        self.assertEqual(r["min_r"], -0.4)
        self.assertEqual(r["levels_hit"], "0.786")

    def test_stopped_out(self):
        r = self.row([fill("e", "BOT", 100, 100.0), fill("s", "SLD", 100, 99.0)])
        self.assertEqual(r["r_multiple"], -1.0)
        self.assertEqual(r["result"], "verloren")

    def test_no_sells_has_no_result(self):
        r = self.row([fill("e", "BOT", 100, 100.0)])
        self.assertEqual(r["result"], "")
        self.assertEqual(r["sold_quantity"], 0)

    def test_missing_range_is_blank(self):
        r = self.row([fill("s", "SLD", 100, 101.0)], high=None, low=None)
        self.assertEqual((r["high"], r["max_r"]), ("", ""))

    def test_fields_match_header(self):
        self.assertEqual(set(self.row([]).keys()), set(journal.TF_TRADE_FIELDS))


class EntryLevel(unittest.TestCase):
    def test_from_level_or_info(self):
        self.assertEqual(journal.entry_level_from("0.9", "TF 0.95 R: 1"), "0.9")
        self.assertEqual(journal.entry_level_from(None, "TF 0.975 R: 1.2 SL: 99 QTY: 83 | Chart 15"), "0.975")
        self.assertEqual(journal.entry_level_from(None, "TF ALT2: 0.975 R: 0.67 SL: 134.99  QTY: 119 | Chart 15"), "0.975 ALT2")
        self.assertEqual(journal.entry_level_from(None, "TF ALT: 0.975 R: 1"), "0.975 ALT")
        self.assertEqual(journal.entry_level_from(None, None), "")
        self.assertEqual(journal.entry_level_from(None, "Doji W 0.95"), "")


class Stats(unittest.TestCase):
    def test_per_level(self):
        rows = [
            {"entry_level": "0.95", "r_multiple": "1.5", "net_pnl_usd": "150"},
            {"entry_level": "0.95", "r_multiple": "-1", "net_pnl_usd": "-100"},
            {"entry_level": "0.975", "r_multiple": "2", "net_pnl_usd": "200"},
            {"entry_level": "", "r_multiple": "0", "net_pnl_usd": "0"},
        ]
        s = journal.tf_stats(rows)
        self.assertEqual(s["total"]["trades"], 4)
        self.assertEqual(s["0.95"]["win_rate_pct"], 50.0)
        self.assertEqual(s["0.975"]["sum_r"], 2.0)
        self.assertEqual(s["?"]["breakeven"], 1)

    def test_doji_stats_unchanged(self):
        s = journal.stats([{"tier": "green", "r_multiple": "2", "net_pnl_usd": "10"}])
        self.assertEqual(s["green"]["won"], 1)
        self.assertEqual(s["yellow"]["trades"], 0)


class Report(unittest.TestCase):
    def test_renders_with_and_without_data(self):
        self.assertIn("Noch nichts", tf_report.render([], [], []))
        versions = [{"first_seen_utc": "2026-11-19T23:29:00+00:00", "stand": "abc12345", "code_hash": "x",
                     "code_changed": "ja", "settings_changes": "max_spread_pct: 0.05 -> 0.15"}]
        html_v = tf_report.render([{"stand": "abc12345", "r_multiple": "1", "net_pnl_usd": "10"}], [], [], versions, "abc12345")
        self.assertIn("Programm geändert; max_spread_pct: 0.05 -&gt; 0.15", html_v)
        trade = {"opened_utc": "2026-11-12T14:30:00+00:00", "symbol": "NVS", "entry_level": "0.95",
                 "quantity": "100", "fill_price": "100", "alert_stop": "99", "entry_slippage_pct": "0.1",
                 "max_r": "3", "min_r": "-0.4", "levels_hit": "0.786", "exit_reason": "exit_all",
                 "net_pnl_usd": "98", "r_multiple": "0.98", "result": "gewonnen"}
        sigs = [
            {"time_utc": "2026-11-12T14:30:00+00:00", "symbol": "NVS", "event": "entry", "level": "0.95",
             "status": "submitted", "quantity": "100"},
            {"time_utc": "2026-11-12T15:30:00+00:00", "symbol": "INTC", "event": "entry", "level": "0.9",
             "status": "rejected", "reason": "spread_too_wide", "spread_pct": "0.4"},
            {"time_utc": "2026-11-12T15:31:00+00:00", "symbol": "<b>X</b>", "event": "entry",
             "status": "ignored", "reason": "outside_trading_hours"},
        ]
        html = tf_report.render([trade], sigs, [])
        self.assertIn("gewonnen", html)
        self.assertIn("Spread (Bid/Ask) zu groß", html)
        self.assertIn("außerhalb der Handelszeit", html)
        self.assertNotIn("<b>X</b>", html)  # escaped


# --------------------------------------------------------------- bot flow
# ib_async needs a newer Python than some dev machines have: stub it, the
# flow below only uses the bot's own code and a fake IBKR client.
if "ib_async" not in sys.modules:
    try:
        import ib_async  # noqa: F401
    except Exception:
        stub = types.ModuleType("ib_async")
        for name in ("CFD", "IB", "Forex", "LimitOrder", "MarketOrder", "Order", "Position", "Stock",
                     "StopOrder", "Trade"):
            setattr(stub, name, mock.MagicMock())
        sys.modules["ib_async"] = stub

import handlers  # noqa: E402
from models import BotAlert  # noqa: E402
from state import position_store  # noqa: E402


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class FakeIBKR:
    """Stands in for ibkr_client: one position, fills the test sets."""

    def __init__(self):
        self.position = 0
        self.fills = []
        self.orders = []

    async def qualify_stock(self, *a):
        return SimpleNamespace(symbol="NVS")

    async def place_market_order(self, contract, action, qty):
        self.orders.append((action, qty))
        if action == "BUY":
            self.position += qty
        else:
            self.position -= qty
        return SimpleNamespace(orderStatus=SimpleNamespace(avgFillPrice=100.0))

    async def wait_for_fill(self, trade):
        return self.orders[-1][1]

    async def place_stop_order(self, contract, action, qty, price):
        return SimpleNamespace(order=SimpleNamespace(orderId=77))

    async def modify_stop_order(self, *a):
        pass

    async def cancel_order(self, order_id):
        pass

    async def get_position(self, symbol):
        return SimpleNamespace(position=self.position) if self.position else None

    def fills_for(self, symbol, since):
        return list(self.fills)

    async def price_range(self, contract, since):
        return 103.0, 99.6


class BotFlow(unittest.TestCase):
    def setUp(self):
        for p in (os.environ["TF_TRADES_FILE"], os.environ["TF_SIGNALS_FILE"], os.environ["TF_LADDER_FILE"]):
            if os.path.exists(p):
                os.remove(p)
        run(position_store.delete("NVS"))
        self.ib = FakeIBKR()
        quote = SimpleNamespace(bid=99.99, ask=100.01, spread_pct=0.02, spread_pct_of_risk=2.0,
                                slippage_pct_of_risk=1.0)

        async def checks(alert, *a, **k):
            return SimpleNamespace(symbol="NVS"), 100, quote, 1.08, 50000.0

        self.patches = [
            mock.patch.object(handlers, "ibkr_client", self.ib),
            mock.patch.object(handlers, "_pretrade_checks", checks),
            mock.patch.object(handlers, "is_within_trading_hours", lambda *a, **k: True),
            mock.patch.object(handlers, "TF_JOURNAL_SETTLE_SECONDS", 0),
            mock.patch.object(handlers, "TF_MIN_AGE_BEFORE_CLOSE_CHECK_SECONDS", 0),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def alert(self, event="entry", **kw):
        base = dict(secret="x", event=event, symbol="NVS", trade_id="42", entry=100.0, stop=99.0,
                    info="TF 0.95 R: 1.01 SL: 99 QTY: 99 | Chart 15")
        base.update(kw)
        return BotAlert(**base)

    def rows(self, name):
        return journal.read_rows(os.environ[name])

    def test_entry_ladder_exit_all_is_journaled(self):
        a = self.alert()
        res = run(handlers.handle_entry(a))
        handlers.log_tf_signal(a, res)
        self.assertEqual(res["status"], "submitted")
        self.assertEqual(res["fill_price"], 100.0)
        st = run(position_store.get("NVS"))
        self.assertEqual(st.entry_level, "0.95")
        self.assertEqual(st.initial_quantity, 100)

        lv = self.alert("level", level="0.786", new_stop=99.5, entry=None, stop=None, info=None)
        res = run(handlers.handle_level(lv))
        handlers.log_tf_signal(lv, res)
        self.assertEqual(res["status"], "partial_exit")
        self.assertEqual(run(position_store.get("NVS")).levels_hit, ["0.786"])

        self.ib.fills = [fill("e", "BOT", 100, 100.0, 1.0), fill("s1", "SLD", 50, 101.0, 0.5),
                         fill("s2", "SLD", 50, 102.0, 0.5)]
        ex = self.alert("exit_all", entry=None, stop=None, info=None)
        res = run(handlers.handle_exit_all(ex))
        handlers.log_tf_signal(ex, res)
        self.assertEqual(run(position_store.get("NVS")).status, "journal_pending")

        run(handlers.manage_tf_positions())
        self.assertIsNone(run(position_store.get("NVS")))
        (row,) = self.rows("TF_TRADES_FILE")
        self.assertEqual(row["entry_level"], "0.95")
        self.assertEqual(row["levels_hit"], "0.786")
        self.assertEqual(row["exit_reason"], "exit_all (-0.236)")
        self.assertEqual(row["net_pnl_usd"], "148.0")
        self.assertEqual(row["max_r"], "3.0")
        self.assertEqual(row["result"], "gewonnen")
        events = [(r["event"], r["status"]) for r in self.rows("TF_SIGNALS_FILE")]
        self.assertEqual(events, [("entry", "submitted"), ("level", "partial_exit"), ("exit_all", "closed")])

    def test_stop_hit_is_noticed_by_the_monitor(self):
        a = self.alert()
        run(handlers.handle_entry(a))
        self.ib.position = 0  # IBKR's stop order filled, nobody told the bot
        self.ib.fills = [fill("e", "BOT", 100, 100.0), fill("s", "SLD", 100, 99.0)]
        run(handlers.manage_tf_positions())
        self.assertIsNone(run(position_store.get("NVS")))
        (row,) = self.rows("TF_TRADES_FILE")
        self.assertTrue(row["exit_reason"].startswith("stop"))
        self.assertEqual(row["r_multiple"], "-1.0")
        self.assertEqual(row["result"], "verloren")

    def test_trade_still_held_is_left_alone(self):
        run(handlers.handle_entry(self.alert()))
        run(handlers.manage_tf_positions())
        self.assertEqual(run(position_store.get("NVS")).status, "open")
        self.assertEqual(self.rows("TF_TRADES_FILE"), [])

    def test_ladder_alert_without_trade_is_not_logged(self):
        lv = self.alert("level", level="0.786", new_stop=99.5, entry=None, stop=None, info=None)
        res = run(handlers.handle_level(lv))
        handlers.log_tf_signal(lv, res)
        self.assertEqual(self.rows("TF_SIGNALS_FILE"), [])
        # ...but the ladder journal keeps it, for the review of signals not taken
        (ladder,) = self.rows("TF_LADDER_FILE")
        self.assertEqual((ladder["symbol"], ladder["level"], ladder["new_stop"], ladder["taken"]),
                         ("NVS", "0.786", "99.5", "nein"))

    def test_rejected_entry_is_logged_with_reason(self):
        async def reject(alert, *a, **k):
            return {"status": "rejected", "reason": "spread_too_wide", "bid": 99.0, "ask": 101.0,
                    "spread_pct": 2.0, "spread_pct_of_risk": 200.0, "slippage_pct_of_risk": 100.0}

        with mock.patch.object(handlers, "_pretrade_checks", reject):
            a = self.alert()
            res = run(handlers.handle_entry(a))
            handlers.log_tf_signal(a, res)
        (row,) = self.rows("TF_SIGNALS_FILE")
        self.assertEqual((row["status"], row["reason"], row["level"]), ("rejected", "spread_too_wide", "0.95"))
        self.assertEqual(row["ask"], "101.0")

    def test_new_entry_finalizes_a_pending_trade_first(self):
        run(handlers.handle_entry(self.alert()))
        self.ib.fills = [fill("e", "BOT", 100, 100.0), fill("s", "SLD", 100, 101.0)]
        run(handlers._tf_mark_closed(run(position_store.get("NVS")), "exit_all (-0.236)"))
        self.ib.position = 0
        run(handlers.handle_entry(self.alert(trade_id="43")))
        self.assertEqual(len(self.rows("TF_TRADES_FILE")), 1)
        self.assertEqual(run(position_store.get("NVS")).trade_id, "43")


class JournalToken(unittest.TestCase):
    def test_derived_token_is_stable_and_not_the_secret(self):
        import main
        t = main.journal_token()
        self.assertEqual(len(t), 24)
        self.assertNotIn("test-secret", t)
        self.assertEqual(t, main.journal_token())


if __name__ == "__main__":
    unittest.main()
