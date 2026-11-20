"""End-to-end Doji entry through handlers.handle_doji with a fake IBKR client:
limit and size, risk by breadth, ETF as CFD, the orders placed, the journal row."""
import asyncio
import csv
import os
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

_tmp = tempfile.mkdtemp()
# Named to load after test_tf_journal: the settings are created once, by the
# first module that imports them, and that one sets the TF journal paths.
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("STATE_FILE", os.path.join(_tmp, "positions.json"))
SIGNALS = os.path.join(_tmp, "doji_signals.csv")
TRADES = os.path.join(_tmp, "doji_trades.csv")

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
from config import settings  # noqa: E402
from models import BotAlert  # noqa: E402
from spread import QuoteSample  # noqa: E402
from state import doji_key, position_store  # noqa: E402


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class FakeIBKR:
    def __init__(self, bid, ask, fill, etf=False):
        self.bid, self.ask, self.fill, self.etf = bid, ask, fill, etf
        self.orders = []
        self.next_id = 100

    def _trade(self, filled=0, price=0.0, status="Submitted"):
        self.next_id += 1
        return SimpleNamespace(order=SimpleNamespace(orderId=self.next_id),
                               orderStatus=SimpleNamespace(status=status, filled=filled, avgFillPrice=price), log=[])

    async def qualify_stock(self, symbol, *a):
        return SimpleNamespace(symbol=symbol, secType="STK", conId=1)

    async def qualify_cfd(self, symbol, currency):
        return SimpleNamespace(symbol=symbol, secType="CFD", conId=2)

    async def stock_type(self, contract):
        return "ETF" if self.etf else "COMMON"

    async def get_quote_samples(self, contract, max_wait, accept, timeout=10.0):
        return [QuoteSample(self.bid, self.ask)]

    async def get_fx_rate(self, pair="EURUSD", timeout=10.0):
        return 1.16

    async def get_available_cash_base_currency(self):
        return 1_000_000.0

    async def get_available_funds_base_currency(self):
        return 1_000_000.0

    async def place_limit_order(self, contract, action, qty, price, outside_rth=True, tif="GTC", oca_group=""):
        self.orders.append(("LMT", contract.secType, action, qty, price, outside_rth, tif, oca_group))
        if action == "BUY":
            return self._trade(qty, self.fill, "Filled")
        return self._trade()

    async def place_market_order(self, contract, action, qty):
        self.orders.append(("MKT", contract.secType, action, qty))
        return self._trade(qty, self.fill, "Filled")

    async def place_stop_order(self, contract, action, qty, price, oca_group=""):
        self.orders.append(("STP", contract.secType, action, qty, price, oca_group))
        return self._trade()

    async def wait_for_fill(self, trade, timeout=15.0):
        return trade.orderStatus.filled

    async def cancel_order(self, order_id):
        pass

    async def stream_ticker(self, contract):
        self.streamed = contract
        return SimpleNamespace(last=float("nan"), bid=float("nan"))

    async def get_position(self, symbol):
        return None

    def fills_for(self, symbol, since):
        return []


def alert(symbol="ABC", entry=100.0, stop=99.0, tier="orange", tid="1"):
    return BotAlert(secret="test-secret", event="doji", symbol=symbol, trade_id=tid,
                    entry=entry, stop=stop, tier=tier)


class DojiEntry(unittest.TestCase):
    def setUp(self):
        handlers._breadth = handlers.doji.Breadth(600)
        for k in list(run(position_store.all_open())):
            run(position_store.delete(handlers.store_key(k)))
        if os.path.exists(SIGNALS):
            os.remove(SIGNALS)
        self.p = [mock.patch.object(handlers, "is_within_doji_entry_hours", return_value=True),
                  mock.patch.object(settings, "dry_run", False),
                  mock.patch.object(settings, "doji_signals_file", SIGNALS),
                  mock.patch.object(settings, "doji_trades_file", TRADES)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def enter(self, fake, a):
        with mock.patch.object(handlers, "ibkr_client", fake):
            return run(handlers.handle_doji(a))

    def test_lone_doji_half_risk_limit_just_above_the_ask(self):
        fake = FakeIBKR(bid=99.98, ask=100.02, fill=100.02)
        res = self.enter(fake, alert())
        self.assertEqual(res["status"], "submitted")
        self.assertEqual(res["risk_usd"], settings.doji_risk_orange_usd * settings.doji_breadth_low_mult)
        buy = [o for o in fake.orders if o[2] == "BUY"][0]
        self.assertEqual(buy[0], "LMT")
        self.assertAlmostEqual(buy[4], 100.12)                 # ask + 10 % of R
        self.assertEqual(buy[3], int(res["risk_usd"] / (100.12 - 99.0)))
        self.assertFalse(buy[5])                               # regular hours only
        # three quarters with stop + target, one trailing stop, all adding up
        stops = [o for o in fake.orders if o[0] == "STP"]
        self.assertEqual(sum(o[3] for o in stops), buy[3])
        state = run(position_store.get(doji_key("ABC")))
        self.assertEqual(state.sec_type, "STK")
        self.assertEqual(state.planned_risk_usd, res["risk_usd"])

    def test_cluster_doubles_the_risk(self):
        for i, s in enumerate(["A1", "A2", "A3", "A4", "A5", "A6"]):
            handlers._breadth.add(1e12 + i, s)
        with mock.patch.object(handlers.time, "time", return_value=1e12 + 10):
            res = self.enter(FakeIBKR(99.98, 100.02, 100.02), alert(symbol="ZZZ"))
        self.assertEqual(res["risk_usd"], settings.doji_risk_orange_usd * settings.doji_breadth_high_mult)
        with open(SIGNALS) as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[-1]["breadth"], "6")

    def test_etf_is_traded_as_cfd_and_prices_come_from_the_etf(self):
        fake = FakeIBKR(99.98, 100.02, 100.02, etf=True)
        res = self.enter(fake, alert(symbol="QQQ", tier="yellow"))
        self.assertEqual(res["status"], "submitted")
        self.assertTrue(all(o[1] == "CFD" for o in fake.orders))
        self.assertTrue(all(o[5] is False for o in fake.orders if o[0] == "LMT"))   # no outside-RTH targets
        self.assertEqual(fake.streamed.secType, "STK")
        self.assertEqual(run(position_store.get(doji_key("QQQ"))).sec_type, "CFD")
        self.assertIn("CFD", res["detail"])

    def test_ask_far_above_the_signal_is_skipped(self):
        fake = FakeIBKR(100.55, 100.60, 100.60)              # more than 50 % of R above 100
        res = self.enter(fake, alert())
        self.assertEqual(res["reason"], "price_ran_away")
        self.assertEqual(fake.orders, [])

    def test_bid_already_at_the_stop_is_skipped(self):
        # BLK 20.11.: bid under the stop, ask just above it -> no buy
        fake = FakeIBKR(bid=98.95, ask=99.10, fill=99.0)
        res = self.enter(fake, alert())
        self.assertEqual(res["reason"], "bid_at_stop")
        self.assertEqual(fake.orders, [])
        with open(SIGNALS) as fh:
            row = list(csv.DictReader(fh))[-1]
        self.assertEqual((row["status"], row["reason"]), ("rejected", "bid_at_stop"))

    def test_bid_just_above_the_stop_still_buys(self):
        fake = FakeIBKR(bid=99.05, ask=99.10, fill=99.10)
        res = self.enter(fake, alert())
        self.assertEqual(res["status"], "submitted")


if __name__ == "__main__":
    unittest.main()
