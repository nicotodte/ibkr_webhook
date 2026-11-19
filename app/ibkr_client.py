import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from ib_async import CFD, IB, Forex, LimitOrder, MarketOrder, Order, Position, Stock, StopOrder, Trade

from config import settings
from spread import QuoteSample

logger = logging.getLogger("ibkr_webhook.ibkr_client")

# ib_async/IBKR ticker.marketDataType values: 1=Live, 2=Frozen, 3=Delayed,
# 4=Delayed Frozen. Only live data is trustworthy enough to trade on.
LIVE_MARKET_DATA_TYPE = 1
DELAYED_MARKET_DATA_TYPE = 3

# ib_async initialises Ticker.marketDataType to 1 and only overwrites it once
# IBKR sends a marketDataType callback for that request, so an untouched
# ticker claims "live" whether or not IBKR ever confirmed it. Overwriting it
# with this sentinel right after reqMktData makes the two cases
# distinguishable: anything left over is what IBKR actually reported.
UNREPORTED_MARKET_DATA_TYPE = 0

# How often the quote is looked at while waiting for it. Short, because the
# first look that passes is what sends the order.
QUOTE_POLL_SECONDS = 0.05

# main.py refreshes EUR/USD every few minutes; a cached rate older than this
# means that refresh is failing, so fetch a live one instead.
FX_CACHE_MAX_AGE_SECONDS = 20 * 60


class DelayedMarketDataError(RuntimeError):
    """Raised when IBKR is only offering delayed/frozen data for a contract."""


def _forget_quote(ticker) -> None:
    """ib_async keeps a Ticker per contract after its data line is cancelled,
    with the last prices still in it. A new request on that symbol would read
    those at once: MSTR 19.11. 18:07 saw the 88.73 of its first trade at 16:35
    instead of ~86.1 and was rejected as 'price ran away' -- the day's best
    Doji (+9 R). So the old prices are cleared and only fresh ticks count."""
    nan = float("nan")
    for f in ("bid", "ask", "last", "bidSize", "askSize", "lastSize"):
        try:
            setattr(ticker, f, nan)
        except Exception:
            pass


def _has_quote(ticker) -> bool:
    return bool(ticker.bid and ticker.ask and ticker.bid > 0 and ticker.ask > 0)


def _set_oca(order, group: str):
    """Puts the order in an OCA group with type 2: when one order of the
    group fills (also partly), IBKR reduces the others by the same number of
    shares, with overfill protection. A Doji quarter's stop and target form
    such a pair, so the stops never cover more shares than are still held."""
    if group:
        order.ocaGroup = group
        order.ocaType = 2


class IBKRClient:
    def __init__(self):
        self._stock_types: dict[str, str] = {}
        self.ib = IB()
        # pair -> (rate, time.monotonic() when it was fetched)
        self._fx_cache: dict[str, tuple[float, float]] = {}
        # conId -> streaming ticker, for open Doji positions
        self._streams: dict = {}

    async def connect(self):
        if self.ib.isConnected():
            return
        await self.ib.connectAsync(
            settings.ib_host, settings.ib_port, clientId=settings.ib_client_id
        )
        # Type 1 is live-only: where the account holds no entitlement IBKR
        # answers with error 354 and sends no ticks at all rather than
        # quietly downgrading. Type 3 asks for delayed quotes and still
        # yields live ones wherever the entitlement does apply.
        self.ib.reqMarketDataType(
            DELAYED_MARKET_DATA_TYPE
            if settings.allow_delayed_market_data
            else LIVE_MARKET_DATA_TYPE
        )
        accounts = self.ib.managedAccounts()
        if not accounts:
            raise RuntimeError("No managed accounts returned by IB Gateway")
        logger.info("Connected to IB Gateway, account=%s", accounts[0])

    async def disconnect(self):
        if self.ib.isConnected():
            self.ib.disconnect()

    async def qualify_stock(self, symbol: str, exchange: str, currency: str) -> Stock:
        await self.connect()
        contract = Stock(symbol, exchange, currency)
        qualified = await self.ib.qualifyContractsAsync(contract)
        if not qualified:
            raise RuntimeError(f"Could not qualify contract for symbol {symbol}")
        return qualified[0]

    async def qualify_cfd(self, symbol: str, currency: str) -> CFD:
        await self.connect()
        qualified = await self.ib.qualifyContractsAsync(CFD(symbol, "SMART", currency))
        if not qualified:
            raise RuntimeError(f"IBKR offers no CFD for {symbol}")
        return qualified[0]

    async def stock_type(self, contract) -> str:
        """IBKR's stockType for a stock contract: COMMON, ETF, ADR, REIT, ...
        Cached per symbol, it does not change."""
        if contract.symbol in self._stock_types:
            return self._stock_types[contract.symbol]
        await self.connect()
        details = await self.ib.reqContractDetailsAsync(contract)
        st = (details[0].stockType or "").upper() if details else ""
        self._stock_types[contract.symbol] = st
        return st

    async def get_quote_samples(
        self,
        contract,
        max_wait: float,
        accept: Callable[[list[QuoteSample]], bool],
        timeout: float = 10.0,
    ) -> list[QuoteSample]:
        """Returns the bid/ask quotes seen, oldest first, as soon as `accept` is satisfied.

        `accept` is run on the samples so far after every new one, starting
        with the very first valid quote: if that already passes, the caller
        gets it back immediately and nothing waits. Only while it doesn't
        pass does this keep sampling, up to `max_wait` seconds, to see
        whether the quote settles. The market data type is checked on the
        first valid quote.
        """
        await self.connect()
        ticker = self.ib.reqMktData(contract, "", False, False)
        _forget_quote(ticker)
        ticker.marketDataType = UNREPORTED_MARKET_DATA_TYPE
        try:
            waited = 0.0
            while not _has_quote(ticker):
                if waited >= timeout:
                    raise RuntimeError(
                        f"No live bid/ask received for {contract.symbol} within {timeout}s"
                    )
                await asyncio.sleep(QUOTE_POLL_SECONDS)
                waited += QUOTE_POLL_SECONDS

            if ticker.marketDataType == UNREPORTED_MARKET_DATA_TYPE:
                logger.warning(
                    "%s: IBKR never reported a market data type, so this "
                    "quote cannot be confirmed as live",
                    contract.symbol,
                )
            elif ticker.marketDataType != LIVE_MARKET_DATA_TYPE:
                if not settings.allow_delayed_market_data:
                    raise DelayedMarketDataError(
                        f"{contract.symbol} is only offering market data type "
                        f"{ticker.marketDataType} (1=live, 2=frozen, 3=delayed, "
                        "4=delayed-frozen) -- no live subscription for this symbol"
                    )
                logger.warning(
                    "%s: trading on market data type %s (1=live, 2=frozen, "
                    "3=delayed, 4=delayed-frozen) -- prices may be up to 15 "
                    "minutes old, ALLOW_DELAYED_MARKET_DATA is on",
                    contract.symbol,
                    ticker.marketDataType,
                )
            else:
                logger.info("%s: live market data confirmed", contract.symbol)

            samples = [QuoteSample(ticker.bid, ticker.ask)]
            collected = 0.0
            while not accept(samples) and collected < max_wait:
                await asyncio.sleep(QUOTE_POLL_SECONDS)
                collected += QUOTE_POLL_SECONDS
                if _has_quote(ticker):
                    samples.append(QuoteSample(ticker.bid, ticker.ask))
            return samples
        finally:
            self.ib.cancelMktData(contract)

    async def get_fx_rate(self, pair: str = "EURUSD", timeout: float = 10.0) -> float:
        """Returns the cached rate while it is fresh, otherwise fetches a live one.

        EUR/USD only feeds the cash check and the position cap, where a few
        minutes of drift is irrelevant, so a live request -- which can cost
        the entry seconds -- is only made when refresh_fx_rate has not kept
        the cache warm.
        """
        cached = self._fx_cache.get(pair)
        if cached and time.monotonic() - cached[1] < FX_CACHE_MAX_AGE_SECONDS:
            return cached[0]
        return await self.refresh_fx_rate(pair, timeout)

    async def refresh_fx_rate(self, pair: str = "EURUSD", timeout: float = 10.0) -> float:
        await self.connect()
        contract = Forex(pair)
        await self.ib.qualifyContractsAsync(contract)
        ticker = self.ib.reqMktData(contract, "", False, False)
        try:
            elapsed = 0.0
            step = 0.25
            while elapsed < timeout:
                await asyncio.sleep(step)
                elapsed += step
                rate = None
                if ticker.bid and ticker.ask and ticker.bid > 0 and ticker.ask > 0:
                    rate = (ticker.bid + ticker.ask) / 2
                elif ticker.last and ticker.last > 0:
                    rate = ticker.last
                if rate:
                    self._fx_cache[pair] = (rate, time.monotonic())
                    return rate
            raise RuntimeError(f"No live FX rate for {pair} within {timeout}s")
        finally:
            self.ib.cancelMktData(contract)

    async def get_available_cash_base_currency(self) -> float:
        return await self._account_value_base_currency("TotalCashValue")

    async def get_available_funds_base_currency(self) -> float:
        """Buying power including margin (IBKR's AvailableFunds), in the
        account's base currency. Used for Doji entries so several can be
        bought at once when the cash alone wouldn't cover them; IBKR still
        rejects an order the margin can't carry."""
        return await self._account_value_base_currency("AvailableFunds")

    async def _account_value_base_currency(self, tag: str) -> float:
        # reqAccountSummary reports every value already converted into the
        # account's base currency, and the row's currency field names that
        # base currency (e.g. "EUR"). It is never the literal "BASE" --
        # that pseudo-currency only exists in reqAccountUpdates/
        # updateAccountValue output, so filtering for it here could never
        # match. Polling doesn't help either: accountSummaryAsync awaits
        # accountSummaryEnd on its first call and caches the result, so a
        # retry loop would just re-read the same dict.
        await self.connect()
        by_currency = {
            v.currency: float(v.value)
            for v in await self.ib.accountSummaryAsync()
            if v.tag == tag
        }
        if not by_currency:
            raise RuntimeError(f"No {tag} row in account summary")

        for currency in ("BASE", settings.account_currency):
            if currency in by_currency:
                return by_currency[currency]

        # Refuse to reinterpret a foreign-currency figure as base currency:
        # that would silently mis-size every position.
        raise RuntimeError(
            f"Account summary reports {tag} only in "
            f"{sorted(by_currency)}, not in the configured "
            f"ACCOUNT_CURRENCY={settings.account_currency} -- set "
            f"ACCOUNT_CURRENCY to the account's actual base currency"
        )

    def fills_for(self, symbol: str, since) -> list[dict]:
        """Executions of this symbol since `since` (aware datetime), from the
        fills ib_async has collected this session (it loads the day's
        executions on connect). Feeds the Doji trade journal."""
        out = []
        for f in self.ib.fills():
            if f.contract.symbol != symbol or f.execution.time < since:
                continue
            commission = 0.0
            report = f.commissionReport
            if report and report.commission and report.commission < 1e9:
                commission = float(report.commission)
            out.append({
                "exec_id": f.execution.execId,
                "side": f.execution.side,
                "qty": float(f.execution.shares),
                "price": float(f.execution.price),
                "time": f.execution.time.isoformat(),
                "commission": commission,
            })
        return out

    async def get_position(self, symbol: str) -> Optional[Position]:
        await self.connect()
        for pos in self.ib.positions():
            if pos.contract.symbol == symbol:
                return pos
        return None

    async def place_market_order(self, contract, action: str, quantity: float) -> Trade:
        order = MarketOrder(action.upper(), quantity)
        # Explicit DAY, as the account preset would set anyway: without it
        # IBKR answers with warning 10349, which ib_async briefly reports as
        # a cancellation of an order that is in fact working.
        order.tif = "DAY"
        trade = self.ib.placeOrder(contract, order)
        logger.info("Placed market order: %s %s x%s", action, contract.symbol, quantity)
        return trade

    async def wait_for_fill(self, trade: Trade, timeout: float = 15.0) -> float:
        """Waits for a (market) order to fill and returns the actually filled quantity.

        IBKR doesn't always fill the full requested quantity (thin liquidity,
        partial fills, etc.), so callers must size follow-up orders (stop,
        take-profit) off the returned value, not the originally requested one.
        """
        await self.connect()
        elapsed = 0.0
        step = 0.25
        while elapsed < timeout:
            if trade.orderStatus.status == "Filled":
                return trade.orderStatus.filled
            if trade.orderStatus.status in ("Inactive", "ApiCancelled") and not trade.orderStatus.filled:
                # Rejected by IBKR (e.g. no trading permission): no point waiting.
                break
            await asyncio.sleep(step)
            elapsed += step

        filled = trade.orderStatus.filled
        if filled and filled > 0:
            logger.warning(
                "Order %s not fully filled within %ss (requested=%s, filled=%s); "
                "proceeding with the partial fill",
                trade.order.orderId,
                timeout,
                trade.order.totalQuantity,
                filled,
            )
            return filled
        raise RuntimeError(
            f"Order {trade.order.orderId} did not fill within {timeout}s"
        )

    async def place_stop_order(
        self, contract, action: str, quantity: float, stop_price: float, oca_group: str = ""
    ) -> Trade:
        order = StopOrder(action.upper(), quantity, stop_price)
        # Without an explicit TIF the account's order preset fills in DAY
        # (IBKR says so via error 10349), which would quietly drop the
        # protective stop at the close while the position stays open.
        order.tif = "GTC"
        _set_oca(order, oca_group)
        trade = self.ib.placeOrder(contract, order)
        logger.info(
            "Placed stop order: %s %s x%s @ %s (orderId=%s)",
            action,
            contract.symbol,
            quantity,
            stop_price,
            trade.order.orderId,
        )
        return trade

    async def modify_stop_order(
        self, contract, order_id: int, action: str, quantity: float, stop_price: float
    ) -> Trade:
        order = StopOrder(action.upper(), quantity, stop_price)
        order.orderId = order_id
        order.tif = "GTC"
        trade = self.ib.placeOrder(contract, order)
        logger.info(
            "Modified stop order %s: %s %s x%s @ %s",
            order_id,
            action,
            contract.symbol,
            quantity,
            stop_price,
        )
        return trade

    async def cancel_order(self, order_id: int):
        await self.connect()
        self.ib.cancelOrder(Order(orderId=order_id))
        logger.info("Cancelled order %s", order_id)

    async def place_limit_order(
        self,
        contract,
        action: str,
        quantity: float,
        limit_price: float,
        outside_rth: bool = True,
        tif: str = "GTC",
        oca_group: str = "",
    ) -> Trade:
        """Limit order that may also fill in the pre-market and after-hours
        session (outside_rth), where IBKR executes no market or stop orders."""
        await self.connect()
        order = LimitOrder(action.upper(), quantity, limit_price)
        order.tif = tif
        order.outsideRth = outside_rth
        _set_oca(order, oca_group)
        trade = self.ib.placeOrder(contract, order)
        logger.info(
            "Placed limit order: %s %s x%s @ %s outsideRth=%s (orderId=%s)",
            action, contract.symbol, quantity, limit_price, outside_rth, trade.order.orderId,
        )
        return trade

    async def modify_limit_order(
        self,
        contract,
        order_id: int,
        action: str,
        quantity: float,
        limit_price: float,
        outside_rth: bool = True,
        tif: str = "GTC",
    ) -> Trade:
        # Same orderId: IBKR changes the working order instead of adding one.
        await self.connect()
        order = LimitOrder(action.upper(), quantity, limit_price)
        order.orderId = order_id
        order.tif = tif
        order.outsideRth = outside_rth
        trade = self.ib.placeOrder(contract, order)
        logger.info("Modified limit order %s: %s %s x%s @ %s", order_id, action, contract.symbol, quantity, limit_price)
        return trade

    async def change_order_price(self, order_id: int, price: float, field: str = "auxPrice") -> bool:
        """Moves the price of a working order (auxPrice = stop trigger,
        lmtPrice = limit) and leaves everything else as it is -- quantity,
        OCA group, TIF. Works on IBKR's own copy of the order, so an OCA
        stop keeps its link to its target. Returns False if the order is no
        longer working (filled or cancelled)."""
        await self.connect()
        for trade in self.ib.openTrades():
            if trade.order.orderId == order_id:
                setattr(trade.order, field, price)
                self.ib.placeOrder(trade.contract, trade.order)
                logger.info("Moved order %s %s to %s", order_id, field, price)
                return True
        return False

    async def price_range(self, contract, since) -> tuple:
        """Highest and lowest price of the 1-minute bars from `since` (aware
        datetime) until now, for the TF journal. (None, None) without bars."""
        await self.connect()
        secs = max(120, int((datetime.now(timezone.utc) - since).total_seconds()) + 60)
        duration = f"{secs} S" if secs <= 86400 else f"{-(-secs // 86400)} D"
        bars = await asyncio.wait_for(
            self.ib.reqHistoricalDataAsync(
                contract, "", duration, "1 min", "TRADES", False, 2
            ),
            timeout=15,
        )
        if not bars:
            return None, None
        return max(b.high for b in bars), min(b.low for b in bars)

    async def open_order_ids(self) -> set[int]:
        await self.connect()
        return {t.order.orderId for t in self.ib.openTrades()}

    async def stream_ticker(self, contract):
        """Keeps one streaming market data line per contract open, so the
        Doji monitor can read the latest price every couple of seconds
        without a new request each time."""
        await self.connect()
        key = contract.conId
        ticker = self._streams.get(key)
        if ticker is None:
            ticker = self.ib.reqMktData(contract, "", False, False)
            _forget_quote(ticker)
            self._streams[key] = ticker
        return ticker

    async def stop_stream(self, contract):
        if self._streams.pop(contract.conId, None) is not None:
            self.ib.cancelMktData(contract)


ibkr_client = IBKRClient()
