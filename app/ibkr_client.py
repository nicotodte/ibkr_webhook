import asyncio
import logging
import time
from typing import Callable, Optional

from ib_async import IB, Forex, MarketOrder, Order, Position, Stock, StopOrder, Trade

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


def _has_quote(ticker) -> bool:
    return bool(ticker.bid and ticker.ask and ticker.bid > 0 and ticker.ask > 0)


class IBKRClient:
    def __init__(self):
        self.ib = IB()
        # pair -> (rate, time.monotonic() when it was fetched)
        self._fx_cache: dict[str, tuple[float, float]] = {}

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
        # reqAccountSummary reports every value already converted into the
        # account's base currency, and the row's currency field names that
        # base currency (e.g. "EUR"). It is never the literal "BASE" --
        # that pseudo-currency only exists in reqAccountUpdates/
        # updateAccountValue output, so filtering for it here could never
        # match. Polling doesn't help either: accountSummaryAsync awaits
        # accountSummaryEnd on its first call and caches the result, so a
        # retry loop would just re-read the same dict.
        await self.connect()
        cash_by_currency = {
            v.currency: float(v.value)
            for v in await self.ib.accountSummaryAsync()
            if v.tag == "TotalCashValue"
        }
        if not cash_by_currency:
            raise RuntimeError("No TotalCashValue row in account summary")

        for currency in ("BASE", settings.account_currency):
            if currency in cash_by_currency:
                return cash_by_currency[currency]

        # Refuse to reinterpret a foreign-currency figure as base currency:
        # that would silently mis-size every position.
        raise RuntimeError(
            f"Account summary reports TotalCashValue only in "
            f"{sorted(cash_by_currency)}, not in the configured "
            f"ACCOUNT_CURRENCY={settings.account_currency} -- set "
            f"ACCOUNT_CURRENCY to the account's actual base currency"
        )

    async def get_position(self, symbol: str) -> Optional[Position]:
        await self.connect()
        for pos in self.ib.positions():
            if pos.contract.symbol == symbol:
                return pos
        return None

    async def place_market_order(self, contract, action: str, quantity: float) -> Trade:
        order = MarketOrder(action.upper(), quantity)
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
        self, contract, action: str, quantity: float, stop_price: float
    ) -> Trade:
        order = StopOrder(action.upper(), quantity, stop_price)
        # Without an explicit TIF the account's order preset fills in DAY
        # (IBKR says so via error 10349), which would quietly drop the
        # protective stop at the close while the position stays open.
        order.tif = "GTC"
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


ibkr_client = IBKRClient()
