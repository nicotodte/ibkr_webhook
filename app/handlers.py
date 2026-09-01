import logging

from .config import settings
from .ibkr_client import DelayedMarketDataError, ibkr_client
from .models import BotAlert
from .sizing import SizingError, calculate_quantity
from .state import TradeState, position_store
from .trading_hours import is_within_trading_hours

logger = logging.getLogger("ibkr_webhook.handlers")


async def handle_entry(alert: BotAlert) -> dict:
    existing = await position_store.get(alert.symbol)
    if existing and existing.status == "open":
        logger.warning(
            "Ignoring entry for %s: already have an open bot position (trade_id=%s)",
            alert.symbol, existing.trade_id,
        )
        return {"status": "ignored", "reason": "position_already_open"}

    if not is_within_trading_hours():
        logger.info("Ignoring entry for %s: outside trading hours", alert.symbol)
        return {"status": "ignored", "reason": "outside_trading_hours"}

    if alert.entry is None or alert.stop is None:
        return {"status": "rejected", "reason": "entry alert requires 'entry' and 'stop'"}

    contract = await ibkr_client.qualify_stock(
        alert.symbol, settings.default_exchange, settings.default_currency
    )

    try:
        bid, ask = await ibkr_client.get_bid_ask(contract)
    except DelayedMarketDataError as exc:
        logger.warning("Rejecting entry for %s: delayed/frozen market data (%s)", alert.symbol, exc)
        return {"status": "rejected", "reason": "delayed_market_data"}
    except Exception as exc:
        logger.warning("Rejecting entry for %s: no live quote (%s)", alert.symbol, exc)
        return {"status": "rejected", "reason": "no_market_data"}

    mid = (bid + ask) / 2
    spread_pct = (ask - bid) / mid * 100
    if spread_pct > settings.max_spread_pct:
        logger.info(
            "Rejecting entry for %s: spread %.3f%% > max %.3f%%",
            alert.symbol, spread_pct, settings.max_spread_pct,
        )
        return {"status": "rejected", "reason": "spread_too_wide", "spread_pct": spread_pct}

    try:
        fx_rate = await ibkr_client.get_fx_rate(settings.fx_pair)
    except Exception as exc:
        logger.warning("Rejecting entry for %s: no FX rate (%s)", alert.symbol, exc)
        return {"status": "rejected", "reason": "no_fx_rate"}

    try:
        quantity = calculate_quantity(alert.entry, alert.stop, fx_rate)
    except SizingError as exc:
        logger.warning("Sizing rejected entry for %s: %s", alert.symbol, exc)
        return {"status": "rejected", "reason": str(exc)}

    notional_eur = (quantity * ask) / fx_rate
    try:
        available_cash_eur = await ibkr_client.get_available_cash_base_currency()
    except Exception as exc:
        logger.warning("Rejecting entry for %s: no cash balance (%s)", alert.symbol, exc)
        return {"status": "rejected", "reason": "no_cash_balance"}

    if notional_eur > available_cash_eur:
        logger.info(
            "Rejecting entry for %s: needs ~EUR %.2f, only EUR %.2f cash available",
            alert.symbol, notional_eur, available_cash_eur,
        )
        return {"status": "rejected", "reason": "insufficient_cash"}

    logger.info(
        "Entry accepted: %s qty=%s entry=%s stop=%s spread=%.3f%% fx=%s cash_eur=%.2f",
        alert.symbol, quantity, alert.entry, alert.stop, spread_pct, fx_rate, available_cash_eur,
    )

    if settings.dry_run:
        return {
            "status": "dry_run",
            "symbol": alert.symbol,
            "quantity": quantity,
            "spread_pct": spread_pct,
            "fx_rate": fx_rate,
        }

    buy_trade = await ibkr_client.place_market_order(contract, "BUY", quantity)
    try:
        filled_qty = int(await ibkr_client.wait_for_fill(buy_trade))
    except Exception as exc:
        logger.warning("Rejecting entry for %s: buy order did not fill (%s)", alert.symbol, exc)
        return {"status": "rejected", "reason": "order_not_filled"}

    if filled_qty != quantity:
        logger.warning(
            "Partial fill for %s: requested=%s filled=%s -- sizing stop off the actual fill",
            alert.symbol, quantity, filled_qty,
        )

    stop_trade = await ibkr_client.place_stop_order(contract, "SELL", filled_qty, alert.stop)

    await position_store.set(
        alert.symbol,
        TradeState(
            symbol=alert.symbol,
            trade_id=alert.trade_id,
            quantity=filled_qty,
            stop_order_id=stop_trade.order.orderId,
            stop_price=alert.stop,
            status="open",
        ),
    )

    return {
        "status": "submitted",
        "symbol": alert.symbol,
        "quantity": filled_qty,
        "requested_quantity": quantity,
    }


async def handle_stop_to_breakeven(alert: BotAlert) -> dict:
    state = await position_store.get(alert.symbol)
    if not state or state.status != "open":
        return {"status": "ignored", "reason": "no_open_position"}
    if state.trade_id != alert.trade_id:
        logger.warning(
            "stop_to_breakeven trade_id mismatch for %s: have %s, got %s",
            alert.symbol, state.trade_id, alert.trade_id,
        )
        return {"status": "ignored", "reason": "trade_id_mismatch"}

    pos = await ibkr_client.get_position(alert.symbol)
    if not pos or pos.position <= 0:
        await position_store.delete(alert.symbol)
        return {"status": "ignored", "reason": "no_live_position"}

    contract = await ibkr_client.qualify_stock(
        alert.symbol, settings.default_exchange, settings.default_currency
    )
    await ibkr_client.modify_stop_order(
        contract, state.stop_order_id, "SELL", pos.position, pos.avgCost
    )
    state.stop_price = pos.avgCost
    await position_store.set(alert.symbol, state)
    return {"status": "stop_updated", "symbol": alert.symbol, "new_stop": pos.avgCost}


async def handle_level(alert: BotAlert) -> dict:
    state = await position_store.get(alert.symbol)
    if not state or state.status != "open":
        return {"status": "ignored", "reason": "no_open_position"}
    if state.trade_id != alert.trade_id:
        logger.warning(
            "level trade_id mismatch for %s: have %s, got %s",
            alert.symbol, state.trade_id, alert.trade_id,
        )
        return {"status": "ignored", "reason": "trade_id_mismatch"}
    if alert.new_stop is None:
        return {"status": "rejected", "reason": "level alert requires 'new_stop'"}

    pos = await ibkr_client.get_position(alert.symbol)
    qty = int(pos.position) if pos else 0
    if qty <= 0:
        await position_store.delete(alert.symbol)
        return {"status": "ignored", "reason": "no_live_position"}

    contract = await ibkr_client.qualify_stock(
        alert.symbol, settings.default_exchange, settings.default_currency
    )

    sell_qty = qty if qty <= 1 else qty // 2
    if sell_qty > 0:
        await ibkr_client.place_market_order(contract, "SELL", sell_qty)

    remaining = qty - sell_qty
    if remaining > 0:
        await ibkr_client.modify_stop_order(
            contract, state.stop_order_id, "SELL", remaining, alert.new_stop
        )
        state.quantity = remaining
        state.stop_price = alert.new_stop
        await position_store.set(alert.symbol, state)
    else:
        await ibkr_client.cancel_order(state.stop_order_id)
        await position_store.delete(alert.symbol)

    logger.info(
        "Level %s for %s: sold %s, remaining %s, new_stop=%s",
        alert.level, alert.symbol, sell_qty, remaining, alert.new_stop,
    )
    return {"status": "partial_exit", "symbol": alert.symbol, "sold": sell_qty, "remaining": remaining}


async def handle_exit_all(alert: BotAlert) -> dict:
    state = await position_store.get(alert.symbol)
    if not state or state.status != "open":
        return {"status": "ignored", "reason": "no_open_position"}
    if state.trade_id != alert.trade_id:
        logger.warning(
            "exit_all trade_id mismatch for %s: have %s, got %s",
            alert.symbol, state.trade_id, alert.trade_id,
        )
        return {"status": "ignored", "reason": "trade_id_mismatch"}

    pos = await ibkr_client.get_position(alert.symbol)
    qty = int(pos.position) if pos else 0

    await ibkr_client.cancel_order(state.stop_order_id)
    if qty > 0:
        contract = await ibkr_client.qualify_stock(
            alert.symbol, settings.default_exchange, settings.default_currency
        )
        await ibkr_client.place_market_order(contract, "SELL", qty)

    await position_store.delete(alert.symbol)
    logger.info("Exit-all for %s: sold %s, position closed", alert.symbol, qty)
    return {"status": "closed", "symbol": alert.symbol, "sold": qty}
