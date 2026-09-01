import asyncio
import logging
from typing import Optional

from ib_async import IB, Forex, MarketOrder, Order, Position, Stock, StopOrder, Trade

from .config import settings

logger = logging.getLogger("ibkr_webhook.ibkr_client")


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
        try:
            elapsed = 0.0
            step = 0.25
            while elapsed < timeout:
                await asyncio.sleep(step)
                elapsed += step
                if ticker.bid and ticker.ask and ticker.bid > 0 and ticker.ask > 0:
                    return ticker.bid, ticker.ask
            raise RuntimeError(f"No live bid/ask received for {contract.symbol} within {timeout}s")
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
        await self.connect()
        for v in self.ib.accountValues():
            if v.tag == "TotalCashValue" and v.currency == "BASE":
                return float(v.value)
        raise RuntimeError("TotalCashValue (BASE) not found in account values")

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

    async def place_stop_order(self, contract, action: str, quantity: float, stop_price: float) -> Trade:
        order = StopOrder(action.upper(), quantity, stop_price)
        trade = self.ib.placeOrder(contract, order)
        logger.info(
            "Placed stop order: %s %s x%s @ %s (orderId=%s)",
            action, contract.symbol, quantity, stop_price, trade.order.orderId,
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
            order_id, action, contract.symbol, quantity, stop_price,
        )
        return trade

    async def cancel_order(self, order_id: int):
        await self.connect()
        self.ib.cancelOrder(Order(orderId=order_id))
        logger.info("Cancelled order %s", order_id)


ibkr_client = IBKRClient()
