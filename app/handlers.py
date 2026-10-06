import logging

from config import settings
from ibkr_client import DelayedMarketDataError, ibkr_client
from models import BotAlert
from sizing import SizingError, calculate_quantity
from spread import assess_entry_quotes
from state import TradeState, position_store
from trading_hours import is_within_trading_hours

logger = logging.getLogger("ibkr_webhook.handlers")


async def handle_entry(alert: BotAlert) -> dict:
    existing = await position_store.get(alert.symbol)
    if existing and existing.status == "open":
        live = await ibkr_client.get_position(alert.symbol)
        if live and live.position > 0:
            logger.warning(
                "Ignoring entry for %s: already have an open bot position (trade_id=%s)",
                alert.symbol, existing.trade_id,
            )
            return {"status": "ignored", "reason": "position_already_open"}
        # Nothing holds the stored trade open any more -- most likely its stop
        # filled, which the bot is never told about. Without this the symbol
        # would stay blocked for every future entry.
        logger.info(
            "Clearing stale state for %s (trade_id=%s): IBKR reports no position",
            alert.symbol, existing.trade_id,
        )
        await ibkr_client.cancel_order(existing.stop_order_id)
        await position_store.delete(alert.symbol)

    if not is_within_trading_hours():
        logger.info("Ignoring entry for %s: outside trading hours", alert.symbol)
        return {"status": "ignored", "reason": "outside_trading_hours"}

    if alert.entry is None or alert.stop is None:
        return {"status": "rejected", "reason": "entry alert requires 'entry' and 'stop'"}

    contract = await ibkr_client.qualify_stock(
        alert.symbol, settings.default_exchange, settings.default_currency
    )

    def assess(samples):
        return assess_entry_quotes(
            samples,
            alert.entry,
            alert.stop,
            settings.max_spread_pct,
            settings.max_spread_pct_of_risk,
            settings.max_entry_slippage_pct_of_risk,
        )

    try:
        # Returns the moment the quote passes, so a good spread buys at once.
        samples = await ibkr_client.get_quote_samples(
            contract, settings.quote_max_wait_seconds, lambda s: assess(s).ok
        )
    except DelayedMarketDataError as exc:
        logger.warning("Rejecting entry for %s: delayed/frozen market data (%s)", alert.symbol, exc)
        return {"status": "rejected", "reason": "delayed_market_data"}
    except Exception as exc:
        logger.warning("Rejecting entry for %s: no live quote (%s)", alert.symbol, exc)
        return {"status": "rejected", "reason": "no_market_data"}

    quote = assess(samples)
    logger.info(
        "Quote check %s: bid=%.4f ask=%.4f | spread %.4f USD (%.3f%%) judged on the worse of "
        "median %.4f and latest %.4f, max %.4f over %d samples | risk/share %.4f: spread is "
        "%.0f%% of risk, ask is %.0f%% of risk above entry %s",
        alert.symbol, quote.bid, quote.ask, quote.decision_spread, quote.spread_pct,
        quote.median_spread, quote.latest_spread, quote.max_spread, quote.samples,
        quote.risk_per_share, quote.spread_pct_of_risk, quote.slippage_pct_of_risk, alert.entry,
    )
    if not quote.ok:
        logger.info("Rejecting entry for %s: %s", alert.symbol, quote.reason)
        return {
            "status": "rejected",
            "reason": quote.reason,
            "bid": quote.bid,
            "ask": quote.ask,
            "spread": quote.decision_spread,
            "spread_pct": quote.spread_pct,
            "spread_pct_of_risk": quote.spread_pct_of_risk,
            "slippage_pct_of_risk": quote.slippage_pct_of_risk,
        }
    # The ask at the end of the window is where a market buy would fill.
    ask = quote.ask
    spread_pct = quote.spread_pct

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
            entry_price=buy_trade.orderStatus.avgFillPrice,
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
    # Deliberately not pos.avgCost: IBKR folds the commission into it, so at
    # one share a $1 commission puts "breakeven" a full dollar above the real
    # entry -- i.e. a SELL stop above the market, which fires immediately.
    await ibkr_client.modify_stop_order(
        contract, state.stop_order_id, "SELL", pos.position, state.entry_price
    )
    state.stop_price = state.entry_price
    await position_store.set(alert.symbol, state)
    return {"status": "stop_updated", "symbol": alert.symbol, "new_stop": state.entry_price}


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


async def flatten_all_before_close() -> dict:
    """Force-closes every bot-managed open position, independent of any
    TradingView alert. Called on a timer shortly before market close so a
    stop never sits through the close and reopens the position on the next
    session's gap.
    """
    closed = []
    for state in await position_store.all_open():
        try:
            pos = await ibkr_client.get_position(state.symbol)
            qty = int(pos.position) if pos else 0

            await ibkr_client.cancel_order(state.stop_order_id)
            if qty > 0:
                contract = await ibkr_client.qualify_stock(
                    state.symbol, settings.default_exchange, settings.default_currency
                )
                await ibkr_client.place_market_order(contract, "SELL", qty)

            await position_store.delete(state.symbol)
            logger.info("Flattened %s before close: sold %s", state.symbol, qty)
            closed.append({"symbol": state.symbol, "sold": qty})
        except Exception:
            logger.exception("Failed to flatten %s before close", state.symbol)

    return {"status": "flattened", "positions": closed}
