import asyncio
import logging
from typing import Optional

from ib_async import IB, Forex, MarketOrder, Order, Position, Stock, StopOrder, Trade

from config import settings

logger = logging.getLogger("ibkr_webhook.ibkr_client")

# ib_async/IBKR ticker.marketDataType values: 1=Live, 2=Frozen, 3=Delayed,
# 4=Delayed Frozen. Only live data is trustworthy enough to trade on.
LIVE_MARKET_DATA_TYPE = 1

# ib_async initialises Ticker.marketDataType to 1 and only overwrites it once
# IBKR sends a marketDataType callback for that request, so an untouched
# ticker claims "live" whether or not IBKR ever confirmed it. Overwriting it
# with this sentinel right after reqMktData makes the two cases
# distinguishable: anything left over is what IBKR actually reported.
UNREPORTED_MARKET_DATA_TYPE = 0


class DelayedMarketDataError(RuntimeError):
    """Raised when IBKR is only offering delayed/frozen data for a contract."""


class IBKRClient:
    def __init__(self):
        self.ib = IB()

    async def connect(self):
        if self.ib.isConnected():
            return
        await self.ib.connectAsync(
            settings.ib_host, settings.ib_port, clientId=settings.ib_client_id
        )
        # Real-time market data; falls back to whatever the account is
        # actually entitled to (IBKR itself decides live vs delayed).
        self.ib.reqMarketDataType(1)
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

    async def get_bid_ask(self, contract, timeout: float = 10.0) -> tuple[float, float]:
        await self.connect()
        ticker = self.ib.reqMktData(contract, "", False, False)
        ticker.marketDataType = UNREPORTED_MARKET_DATA_TYPE
        try:
            elapsed = 0.0
            step = 0.25
            while elapsed < timeout:
                await asyncio.sleep(step)
                elapsed += step
                if ticker.bid and ticker.ask and ticker.bid > 0 and ticker.ask > 0:
                    if ticker.marketDataType == UNREPORTED_MARKET_DATA_TYPE:
                        logger.warning(
                            "%s: IBKR never reported a market data type, so this "
                            "quote cannot be confirmed as live",
                            contract.symbol,
                        )
                    elif ticker.marketDataType != LIVE_MARKET_DATA_TYPE:
                        raise DelayedMarketDataError(
                            f"{contract.symbol} is only offering market data type "
                            f"{ticker.marketDataType} (1=live, 2=frozen, 3=delayed, "
                            "4=delayed-frozen) -- no live subscription for this symbol"
                        )
                    else:
                        logger.info("%s: live market data confirmed", contract.symbol)
                    return ticker.bid, ticker.ask
            raise RuntimeError(
                f"No live bid/ask received for {contract.symbol} within {timeout}s"
            )
        finally:
            self.ib.cancelMktData(contract)

    async def get_fx_rate(self, pair: str = "EURUSD", timeout: float = 10.0) -> float:
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
                if ticker.bid and ticker.ask and ticker.bid > 0 and ticker.ask > 0:
                    return (ticker.bid + ticker.ask) / 2
                if ticker.last and ticker.last > 0:
                    return ticker.last
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
