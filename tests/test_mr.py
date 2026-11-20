"""Index mean reversion, signal logging only: the rule, the bookkeeping, and a
full day's check with a fake IBKR client -- which must never place an order."""
import asyncio
import csv
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
if "ib_async" not in sys.modules:
    try:
        import ib_async  # noqa: F401
    except Exception:
        stub = types.ModuleType("ib_async")
        for name in ("CFD", "IB", "Forex", "LimitOrder", "MarketOrder", "Order", "Position", "Stock",
                     "StopOrder", "Trade"):
            setattr(stub, name, mock.MagicMock())
        sys.modules["ib_async"] = stub

import mr_logic  # noqa: E402


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class TestRule(unittest.TestCase):
    def test_rsi_extremes(self):
        self.assertEqual(mr_logic.rsi([1, 2, 3, 4]), 100.0)
        self.assertLess(mr_logic.rsi([10, 9, 8, 7.9]), 5)
        self.assertIsNone(mr_logic.rsi([1, 2]))

    def test_rsi_matches_wilder_by_hand(self):
        # alpha 1/2: up/down averages start with the first move
        closes = [10, 11, 10.5, 10.0]
        au, ad = 1.0, 0.0
        au, ad = au * 0.5 + 0 * 0.5, ad * 0.5 + 0.5 * 0.5
        au, ad = au * 0.5, ad * 0.5 + 0.5 * 0.5
        self.assertAlmostEqual(mr_logic.rsi(closes), 100 - 100 / (1 + au / ad))

    def test_entry_needs_uptrend_and_low_rsi(self):
        rising = [100 + i * 0.1 for i in range(250)]
        dip = rising + [rising[-1] - 1.0, rising[-1] - 2.0]
        ind = mr_logic.indicators(dip)
        self.assertTrue(ind.entry_signal)
        falling = [200 - i * 0.2 for i in range(250)]
        self.assertFalse(mr_logic.indicators(falling).entry_signal)   # below SMA200

    def test_exit_above_sma5(self):
        self.assertTrue(mr_logic.indicators([10] * 210 + [11]).exit_signal)
        self.assertFalse(mr_logic.indicators([10] * 210 + [9]).exit_signal)

    def test_with_today_replaces_unfinished_bar(self):
        d0, d1 = date(2026, 11, 18), date(2026, 11, 19)
        self.assertEqual(mr_logic.with_today([(d0, 1.0), (d1, 2.0)], d1, 3.0), [1.0, 3.0])
        self.assertEqual(mr_logic.with_today([(d0, 1.0)], d1, 3.0), [1.0, 3.0])

    def test_session_close(self):
        h = "20261126:CLOSED;20261127:0930-20261127:1300;20261130:0930-20261130:1600"
        self.assertIsNone(mr_logic.session_close(h, date(2026, 11, 26)))
        self.assertEqual(mr_logic.session_close(h, date(2026, 11, 27)), time(13, 0))
        self.assertEqual(mr_logic.session_close(h, date(2026, 11, 30)), time(16, 0))
        self.assertIsNone(mr_logic.session_close(h, date(2026, 12, 1)))

    def test_fallback_commission(self):
        self.assertEqual(mr_logic.fallback_commission(40, price=600), 1.0)
        self.assertEqual(mr_logic.fallback_commission(1000, price=50), 5.0)

    def test_close_trade_books_ask_in_bid_out_and_costs(self):
        pos = mr_logic.ShadowPosition(symbol="QQQ", opened_utc="x", entry_day="2026-11-19", quantity=40,
                                      entry_ask=600.10, entry_bid=600.00, entry_last=600.05,
                                      entry_commission=1.0, commission_source="ibkr", rsi2=5.0,
                                      sma200=550.0, notional_usd=24004.0)
        t = mr_logic.close_trade(pos, exit_day="2026-11-23", closed_utc="y", exit_bid=606.00, exit_ask=606.10,
                                 exit_last=606.05, exit_commission=1.0, exit_commission_source="ibkr",
                                 financing_pct_year=5.0, sma5=605.0)
        self.assertEqual(t["nights"], 4)
        self.assertAlmostEqual(t["gross_pnl_usd"], (606.00 - 600.10) * 40)
        self.assertAlmostEqual(t["financing_usd"], round(24004.0 * 0.05 / 360 * 4, 2))
        self.assertAlmostEqual(t["net_pnl_usd"], round(236.0 - 2.0 - t["financing_usd"], 2))
        self.assertAlmostEqual(t["close_based_pnl_usd"], 240.0)
        self.assertAlmostEqual(t["spread_cost_usd"], 4.0)


class FakeIB:
    def __init__(self, owner):
        self.o = owner

    async def reqContractDetailsAsync(self, contract):
        return [SimpleNamespace(liquidHours=self.o.hours)]

    async def reqHistoricalDataAsync(self, contract, *a):
        return [SimpleNamespace(date=d, close=c) for d, c in self.o.bars]

    async def whatIfOrderAsync(self, contract, order):
        self.o.whatifs.append((contract.secType, order))
        return SimpleNamespace(commission=1.23, minCommission=1e308, maxCommission=1e308)

    def placeOrder(self, *a, **k):   # must never be called
        raise AssertionError("MR shadow placed an order")


class FakeIBKR:
    def __init__(self, bars, bid, ask, hours):
        self.bars, self.bid, self.ask, self.hours = bars, bid, ask, hours
        self.whatifs = []
        self.ib = FakeIB(self)

    async def qualify_stock(self, symbol, *a):
        return SimpleNamespace(symbol=symbol, secType="STK", conId=1)

    async def qualify_cfd(self, symbol, currency):
        return SimpleNamespace(symbol=symbol, secType="CFD", conId=2)

    async def get_quote_samples(self, contract, max_wait, accept, timeout=10.0):
        return [SimpleNamespace(bid=self.bid, ask=self.ask)]

    async def place_limit_order(self, *a, **k):
        raise AssertionError("MR shadow placed an order")

    async def place_market_order(self, *a, **k):
        raise AssertionError("MR shadow placed an order")


class TestShadowDay(unittest.TestCase):
    def setUp(self):
        import mr_shadow
        from config import settings
        self.mr = mr_shadow
        self.settings = settings
        self.tmp = tempfile.mkdtemp()
        self.patch = mock.patch.multiple(
            settings, mr_symbols="QQQ", mr_notional_usd=25000.0, mr_instrument="CFD", mr_use_whatif=True,
            mr_shadow_enabled=True, mr_minutes_before_close=5, mr_cfd_financing_pct_year=5.0,
            mr_signals_file=os.path.join(self.tmp, "s.csv"), mr_trades_file=os.path.join(self.tmp, "t.csv"),
            mr_state_file=os.path.join(self.tmp, "st.json"),
        )
        self.patch.start()
        mr_shadow._close_cache.clear()

    def tearDown(self):
        self.patch.stop()

    def _bars(self, end_day, closes):
        days = [end_day - timedelta(days=len(closes) - 1 - i) for i in range(len(closes))]
        return list(zip(days, closes))

    def _at(self, day, hh, mm):
        return datetime.combine(day, time(hh, mm), tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)

    def test_buy_then_sell_without_any_order(self):
        d1 = date(2026, 11, 19)            # Thursday
        rising = [500 + i * 0.5 for i in range(250)]
        hist = rising + [rising[-1] - 4, rising[-1] - 8]          # two down days, then today
        hours = "20261119:0930-20261119:1600;20261120:0930-20261120:1600"
        fake = FakeIBKR(self._bars(d1 - timedelta(days=1), hist), bid=600.0, ask=600.1, hours=hours)
        with mock.patch.object(self.mr, "ibkr_client", fake):
            self.assertIsNone(run(self.mr.run_if_due(self._at(d1, 15, 50))))      # too early
            self.assertEqual(run(self.mr.run_if_due(self._at(d1, 15, 56))), "done")
            self.assertIsNone(run(self.mr.run_if_due(self._at(d1, 15, 58))))      # once a day
        state = json.load(open(self.settings.mr_state_file))
        pos = state["positions"]["QQQ"]
        self.assertEqual(pos["quantity"], int(25000 // 600.1))
        self.assertEqual(pos["entry_ask"], 600.1)
        self.assertEqual(pos["entry_commission"], 1.23)
        self.assertEqual(fake.whatifs[0][0], "CFD")

        d2 = date(2026, 11, 20)
        fake.bars = self._bars(d1, hist + [600.05])
        fake.bid, fake.ask = 640.0, 640.1                          # far above the 5-day average
        with mock.patch.object(self.mr, "ibkr_client", fake):
            self.assertEqual(run(self.mr.run_if_due(self._at(d2, 15, 56))), "done")
        rows = list(csv.DictReader(open(self.settings.mr_trades_file)))
        self.assertEqual(len(rows), 1)
        t = rows[0]
        self.assertEqual(t["nights"], "1")
        self.assertAlmostEqual(float(t["gross_pnl_usd"]), round((640.0 - 600.1) * pos["quantity"], 2))
        self.assertAlmostEqual(float(t["commission_usd"]), 2.46)
        actions = [r["action"] for r in csv.DictReader(open(self.settings.mr_signals_file))]
        self.assertEqual(actions, ["buy", "sell"])
        s = self.mr.stats()
        self.assertEqual(s["closed_trades"], 1)
        self.assertEqual(s["mode"], "nur Signale, keine Orders")

    def test_holiday_and_failed_quote_retry(self):
        d = date(2026, 11, 26)
        fake = FakeIBKR([], 1, 1, "20261126:CLOSED")
        with mock.patch.object(self.mr, "ibkr_client", fake):
            self.assertEqual(run(self.mr.run_if_due(self._at(d, 15, 56))), "closed")
        d = date(2026, 11, 30)
        self.mr._close_cache.clear()
        fake = FakeIBKR(self._bars(d - timedelta(days=1), [500.0] * 250), 500, 500.1,
                        "20261130:0930-20261130:1600")

        async def broken(*a, **k):
            raise RuntimeError("No live bid/ask")
        fake.get_quote_samples = broken
        with mock.patch.object(self.mr, "ibkr_client", fake):
            self.assertEqual(run(self.mr.run_if_due(self._at(d, 15, 56))), "retry")
        rows = list(csv.DictReader(open(self.settings.mr_signals_file)))
        self.assertEqual(rows[-1]["action"], "error")


if __name__ == "__main__":
    unittest.main()
