"""The red-candle tactic (tier "red", sent by the TF script) on the Doji engine:
own risk, no breadth, own journal files, stays out of the Doji numbers."""
import csv
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

from test_tradeflow_doji import FakeIBKR, alert, handlers, position_store, run, settings  # noqa: E402
from state import doji_key  # noqa: E402

_tmp = tempfile.mkdtemp()
RED_SIGNALS = os.path.join(_tmp, "red_signals.csv")
RED_TRADES = os.path.join(_tmp, "red_trades.csv")
DOJI_SIGNALS = os.path.join(_tmp, "doji_signals.csv")
DOJI_TRADES = os.path.join(_tmp, "doji_trades.csv")


def rows(path):
    with open(path) as fh:
        return list(csv.DictReader(fh))


class RedCandle(unittest.TestCase):
    def setUp(self):
        handlers._breadth = handlers.doji.Breadth(600)
        for k in list(run(position_store.all_open())):
            run(position_store.delete(handlers.store_key(k)))
        for p in (RED_SIGNALS, RED_TRADES, DOJI_SIGNALS, DOJI_TRADES):
            if os.path.exists(p):
                os.remove(p)
        self.p = [mock.patch.object(handlers, "is_within_doji_entry_hours", return_value=True),
                  mock.patch.object(settings, "dry_run", False),
                  mock.patch.object(settings, "red_signals_file", RED_SIGNALS),
                  mock.patch.object(settings, "red_trades_file", RED_TRADES),
                  mock.patch.object(settings, "doji_signals_file", DOJI_SIGNALS),
                  mock.patch.object(settings, "doji_trades_file", DOJI_TRADES)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def enter(self, fake, a):
        with mock.patch.object(handlers, "ibkr_client", fake):
            return run(handlers.handle_doji(a))

    def test_own_risk_even_in_a_sell_off_and_quarters_like_green(self):
        for i, s in enumerate(["A1", "A2", "A3", "A4", "A5", "A6"]):
            handlers._breadth.add(1e12 + i, s)           # a broad sell-off of Dojis
        fake = FakeIBKR(99.98, 100.02, 100.02)
        with mock.patch.object(handlers.time, "time", return_value=1e12 + 10):
            res = self.enter(fake, alert(symbol="CHWY", tier="red"))
        self.assertEqual(res["status"], "submitted")
        self.assertEqual(res["risk_usd"], settings.doji_risk_red_usd)     # no breadth multiplier
        sells = [o for o in fake.orders if o[0] == "LMT" and o[2] == "SELL"]
        self.assertEqual(len(sells), 3)                                   # 2 R, 5 R, 10 R
        state = run(position_store.get(doji_key("CHWY")))
        self.assertEqual(state.tier, "red")

    def test_red_signal_does_not_count_for_the_doji_breadth(self):
        self.enter(FakeIBKR(99.98, 100.02, 100.02), alert(symbol="CHWY", tier="red"))
        self.assertEqual(handlers._breadth.seen, [])

    def test_journals_go_to_the_red_files_not_the_doji_files(self):
        fake = FakeIBKR(99.98, 100.02, 100.02)
        self.enter(fake, alert(symbol="CHWY", tier="red"))
        (sig,) = rows(RED_SIGNALS)
        self.assertEqual((sig["symbol"], sig["tier"], sig["status"]), ("CHWY", "red", "submitted"))
        self.assertFalse(os.path.exists(DOJI_SIGNALS))
        state = run(position_store.get(doji_key("CHWY")))
        with mock.patch.object(handlers, "ibkr_client", fake):
            handlers._journal_close(state, "test")
        (trade,) = rows(RED_TRADES)
        self.assertEqual(trade["tier"], "red")
        self.assertFalse(os.path.exists(DOJI_TRADES))

    def test_doji_alerts_still_use_the_doji_files(self):
        self.enter(FakeIBKR(99.98, 100.02, 100.02), alert(symbol="ABC", tier="orange"))
        self.assertTrue(os.path.exists(DOJI_SIGNALS))
        self.assertFalse(os.path.exists(RED_SIGNALS))

    def test_a_red_trade_blocks_a_second_one_on_the_same_symbol(self):
        fake = FakeIBKR(99.98, 100.02, 100.02)
        fake.get_position = None
        first = self.enter(FakeIBKR(99.98, 100.02, 100.02), alert(symbol="CHWY", tier="red", tid="1"))
        self.assertEqual(first["status"], "submitted")

        class Held(FakeIBKR):
            async def get_position(self, symbol):
                from types import SimpleNamespace
                return SimpleNamespace(position=10)

        second = self.enter(Held(99.98, 100.02, 100.02), alert(symbol="CHWY", tier="red", tid="2"))
        self.assertEqual(second["reason"], "position_already_open")


if __name__ == "__main__":
    unittest.main()
