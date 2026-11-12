import logging
from typing import Optional

from config import settings
from ibkr_client import DelayedMarketDataError, ibkr_client
from models import BotAlert
from sizing import SizingError, calculate_quantity
from spread import assess_entry_quotes
from state import TradeState, doji_key, position_store, store_key
import doji
from trading_hours import (
    is_extended_session,
    is_regular_session,
    is_within_doji_entry_hours,
    is_within_trading_hours,
)

logger = logging.getLogger("ibkr_webhook.handlers")


async def _pretrade_checks(
    alert: BotAlert,
    risk_usd: Optional[float] = None,
    max_slippage_pct_of_risk: Optional[float] = None,
):
    """Quote, FX, sizing and cash checks shared by TF and Doji entries.

    Returns (contract, quantity, quote, fx_rate, available_cash_eur) when the
    entry may go ahead, otherwise the rejection response as a dict.
    """
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
            settings.max_entry_slippage_pct_of_risk
            if max_slippage_pct_of_risk is None
            else max_slippage_pct_of_risk,
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

    try:
        fx_rate = await ibkr_client.get_fx_rate(settings.fx_pair)
    except Exception as exc:
        logger.warning("Rejecting entry for %s: no FX rate (%s)", alert.symbol, exc)
        return {"status": "rejected", "reason": "no_fx_rate"}

    try:
        quantity = calculate_quantity(alert.entry, alert.stop, fx_rate, risk_usd)
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

    return contract, quantity, quote, fx_rate, available_cash_eur


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

    doji_trade = await position_store.get(doji_key(alert.symbol))
    if doji_trade and doji_trade.status in ("open", "closing"):
        # Both strategies size their sells off the live IBKR position, which
        # would then mix the two trades.
        logger.info("Ignoring entry for %s: a Doji trade is open on it", alert.symbol)
        return {"status": "ignored", "reason": "doji_position_open"}

    if not is_within_trading_hours():
        logger.info("Ignoring entry for %s: outside trading hours", alert.symbol)
        return {"status": "ignored", "reason": "outside_trading_hours"}

    if alert.entry is None or alert.stop is None:
        return {"status": "rejected", "reason": "entry alert requires 'entry' and 'stop'"}

    checked = await _pretrade_checks(alert)
    if isinstance(checked, dict):
        return checked
    contract, quantity, quote, fx_rate, available_cash_eur = checked
    spread_pct = quote.spread_pct

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

    Doji trades that already trail (10 R reached) are left running when
    DOJI_KEEP_TRAILING_OVERNIGHT is on; the monitor keeps managing them in
    the after-hours and pre-market session.
    """
    closed = []
    for state in await position_store.all_open():
        if state.strategy == "doji" and state.trailing and settings.doji_keep_trailing_overnight:
            logger.info("Keeping trailing Doji position %s open through the close", state.symbol)
            continue
        try:
            pos = await ibkr_client.get_position(state.symbol)
            qty = int(pos.position) if pos else 0

            for order_id in _order_ids(state):
                await ibkr_client.cancel_order(order_id)
            contract = None
            if qty > 0:
                contract = await ibkr_client.qualify_stock(
                    state.symbol, settings.default_exchange, settings.default_currency
                )
                await ibkr_client.place_market_order(contract, "SELL", qty)
            if state.strategy == "doji":
                await _stop_doji_stream(state.symbol)

            await position_store.delete(store_key(state))
            logger.info("Flattened %s before close: sold %s", state.symbol, qty)
            closed.append({"symbol": state.symbol, "sold": qty})
        except Exception:
            logger.exception("Failed to flatten %s before close", state.symbol)

    return {"status": "flattened", "positions": closed}


def _order_ids(state: TradeState) -> list[int]:
    """Every order the bot may still have resting for this trade."""
    ids = [state.stop_order_id] + [t["order_id"] for t in state.targets]
    if state.exit_order_id:
        ids.append(state.exit_order_id)
    return [i for i in ids if i]


# ══════════════════════════════════════════════════════════════════════════
#  Doji trades
#
#  One alert opens the trade; everything after that is the bot's job:
#  resting limit sells for a quarter each at 2 R, 5 R and 10 R, an IBKR stop
#  for the rest that follows the ladder in doji.ladder_stop, and a trail for
#  the last quarter. R comes from the actual fill.
#
#  Regular hours: IBKR's stop order protects the position even if the bot
#  is down. Outside them IBKR runs no stop orders, so the monitor watches
#  the bid itself and sells with a limit just below it once the stop is
#  crossed. The take-profit limits rest outside regular hours too.
# ══════════════════════════════════════════════════════════════════════════

def _doji_risk(tier: str) -> float:
    return {
        "green": settings.doji_risk_green_usd,
        "yellow": settings.doji_risk_yellow_usd,
        "orange": settings.doji_risk_orange_usd,
    }[tier]


def _doji_trail_r(tier: str) -> float:
    return {
        "green": settings.doji_trail_r_green,
        "yellow": settings.doji_trail_r_yellow,
        "orange": settings.doji_trail_r_orange,
    }.get(tier, settings.doji_trail_r_orange)


async def _stop_doji_stream(symbol: str):
    try:
        contract = await ibkr_client.qualify_stock(
            symbol, settings.default_exchange, settings.default_currency
        )
        await ibkr_client.stop_stream(contract)
    except Exception:
        logger.warning("Could not stop the price stream for %s", symbol, exc_info=True)


async def handle_doji(alert: BotAlert) -> dict:
    if not settings.doji_enabled:
        return {"status": "ignored", "reason": "doji_disabled"}
    if alert.entry is None or alert.stop is None or alert.tier is None:
        return {"status": "rejected", "reason": "doji alert requires 'entry', 'stop' and 'tier'"}

    key = doji_key(alert.symbol)
    existing = await position_store.get(key)
    if existing and existing.status in ("open", "closing"):
        live = await ibkr_client.get_position(alert.symbol)
        if live and live.position > 0:
            logger.info(
                "Ignoring Doji for %s: a Doji trade is already open (trade_id=%s)",
                alert.symbol, existing.trade_id,
            )
            return {"status": "ignored", "reason": "position_already_open"}
        logger.info("Clearing stale Doji state for %s: IBKR reports no position", alert.symbol)
        for order_id in _order_ids(existing):
            await ibkr_client.cancel_order(order_id)
        await position_store.delete(key)

    tf = await position_store.get(alert.symbol)
    if tf and tf.status == "open":
        logger.info("Ignoring Doji for %s: a TF trade is open on it", alert.symbol)
        return {"status": "ignored", "reason": "tf_position_open"}

    if not is_within_doji_entry_hours():
        logger.info("Ignoring Doji for %s: outside the Doji entry window", alert.symbol)
        return {"status": "ignored", "reason": "outside_trading_hours"}

    risk_usd = _doji_risk(alert.tier)
    checked = await _pretrade_checks(alert, risk_usd, settings.doji_max_entry_slippage_pct_of_risk)
    if isinstance(checked, dict):
        return checked
    contract, quantity, quote, fx_rate, available_cash_eur = checked

    logger.info(
        "Doji accepted: %s %s qty=%s entry=%s stop=%s risk=%.0f USD spread=%.3f%%",
        alert.symbol, alert.tier, quantity, alert.entry, alert.stop, risk_usd, quote.spread_pct,
    )
    if settings.dry_run:
        return {"status": "dry_run", "symbol": alert.symbol, "tier": alert.tier, "quantity": quantity}

    buy_trade = await ibkr_client.place_market_order(contract, "BUY", quantity)
    try:
        filled_qty = int(await ibkr_client.wait_for_fill(buy_trade))
    except Exception as exc:
        logger.warning("Rejecting Doji for %s: buy order did not fill (%s)", alert.symbol, exc)
        return {"status": "rejected", "reason": "order_not_filled"}

    fill = buy_trade.orderStatus.avgFillPrice or quote.ask
    stop = doji.floor_cent(alert.stop)
    risk = fill - stop
    stop_trade = await ibkr_client.place_stop_order(contract, "SELL", filled_qty, stop)

    targets = []
    if risk > 0:
        sizes = doji.split_quarters(filled_qty)
        for size, price in zip(sizes[:3], doji.target_prices(fill, risk)):
            if size <= 0:
                continue
            tp = await ibkr_client.place_limit_order(contract, "SELL", size, price)
            targets.append({"order_id": tp.order.orderId, "quantity": size, "price": price})
    else:
        # Filled at or below the stop: the stop order takes it straight out.
        logger.warning("Doji %s filled at %.2f, not above its stop %.2f", alert.symbol, fill, stop)

    state = TradeState(
        symbol=alert.symbol,
        trade_id=alert.trade_id,
        quantity=filled_qty,
        stop_order_id=stop_trade.order.orderId,
        stop_price=stop,
        entry_price=fill,
        status="open",
        strategy="doji",
        tier=alert.tier,
        risk_per_share=max(risk, 0.0),
        high_water=fill,
        targets=targets,
    )
    await position_store.set(key, state)
    await ibkr_client.stream_ticker(contract)
    logger.info(
        "Doji opened: %s %s qty=%s fill=%.2f stop=%.2f R=%.2f targets=%s",
        alert.symbol, alert.tier, filled_qty, fill, stop, risk,
        [(t["quantity"], t["price"]) for t in targets],
    )
    return {
        "status": "submitted",
        "symbol": alert.symbol,
        "tier": alert.tier,
        "quantity": filled_qty,
        "requested_quantity": quantity,
        "risk_per_share": round(risk, 4),
        "targets": targets,
    }


FLAT_PASSES_TO_CLOSE = 3
_flat_passes: dict[str, int] = {}


async def manage_doji_positions():
    """One pass over every open Doji trade; main.py calls this every
    DOJI_MONITOR_INTERVAL_SECONDS."""
    for state in await position_store.all_open():
        if state.strategy != "doji":
            continue
        try:
            await _manage_doji(state)
        except Exception:
            logger.exception("Managing Doji trade %s failed", state.symbol)


async def _manage_doji(state: TradeState):
    key = store_key(state)
    pos = await ibkr_client.get_position(state.symbol)
    qty = int(pos.position) if pos else 0

    if qty <= 0:
        # Stopped out, all targets filled, or sold by the extended-hours exit.
        # IBKR's position list can lag a fresh fill, so only a flat reading
        # several passes in a row counts.
        _flat_passes[key] = _flat_passes.get(key, 0) + 1
        if _flat_passes[key] < FLAT_PASSES_TO_CLOSE:
            return
        _flat_passes.pop(key, None)
        for order_id in _order_ids(state):
            await ibkr_client.cancel_order(order_id)
        await _stop_doji_stream(state.symbol)
        await position_store.delete(key)
        logger.info("Doji %s closed (trade_id=%s)", state.symbol, state.trade_id)
        return

    _flat_passes.pop(key, None)
    contract = await ibkr_client.qualify_stock(
        state.symbol, settings.default_exchange, settings.default_currency
    )
    changed = False

    # Targets that are no longer open have filled (or were cancelled).
    open_ids = await ibkr_client.open_order_ids()
    still_open = [t for t in state.targets if t["order_id"] in open_ids]
    if len(still_open) != len(state.targets):
        state.targets = still_open
        changed = True

    ticker = await ibkr_client.stream_ticker(contract)
    last = ticker.last if ticker.last and ticker.last > 0 else 0.0
    bid = ticker.bid if ticker.bid and ticker.bid > 0 else 0.0
    price = last or bid
    if price > state.high_water:
        state.high_water = price
        changed = True

    if state.status == "closing":
        await _work_doji_exit(state, contract, qty, bid, open_ids)
        await position_store.set(key, state)
        return

    new_stop, trailing = doji.ladder_stop(
        state.entry_price,
        state.risk_per_share,
        state.high_water,
        state.stop_price,
        _doji_trail_r(state.tier),
        settings.doji_breakeven_r,
    )
    if trailing and not state.trailing:
        state.trailing = True
        changed = True
        logger.info("Doji %s reached 10 R: trailing the rest", state.symbol)

    if new_stop > state.stop_price or qty != state.quantity:
        await ibkr_client.modify_stop_order(
            contract, state.stop_order_id, "SELL", qty, max(new_stop, state.stop_price)
        )
        if new_stop > state.stop_price:
            logger.info(
                "Doji %s stop %.2f -> %.2f (high %.2f, %.1f R)",
                state.symbol, state.stop_price, new_stop, state.high_water,
                (state.high_water - state.entry_price) / state.risk_per_share
                if state.risk_per_share else 0.0,
            )
            state.stop_price = new_stop
        state.quantity = qty
        changed = True

    if is_extended_session() and bid and bid <= state.stop_price:
        # IBKR's stop order does not run outside regular hours.
        logger.info(
            "Doji %s: bid %.2f at/below stop %.2f outside regular hours, selling %s",
            state.symbol, bid, state.stop_price, qty,
        )
        for order_id in _order_ids(state):
            await ibkr_client.cancel_order(order_id)
        state.targets = []
        state.stop_order_id = 0
        state.status = "closing"
        await _work_doji_exit(state, contract, qty, bid, set())
        changed = True

    if changed:
        await position_store.set(key, state)


async def _work_doji_exit(state: TradeState, contract, qty: int, bid: float, open_ids: set[int]):
    """Drives the exit of a Doji trade whose stop was crossed outside
    regular hours until IBKR reports the position flat.

    Only one exit order is ever working: it is re-priced in place (same
    order id) as the bid falls, never cancelled and replaced, so two sells
    can't both fill. If it is gone while shares remain (a DAY limit that
    expired), the next session sells again: at market in regular hours, with
    a limit outside them; overnight nothing trades.
    """
    below = settings.doji_extended_exit_below_bid_pct
    if state.exit_order_id and state.exit_order_id in open_ids:
        if is_extended_session() and bid:
            limit = doji.extended_hours_exit_limit(bid, below)
            if limit and limit < state.exit_price:
                await ibkr_client.modify_limit_order(
                    contract, state.exit_order_id, "SELL", qty, limit, tif="DAY"
                )
                state.exit_price = limit
        return

    if is_regular_session():
        trade = await ibkr_client.place_market_order(contract, "SELL", qty)
        state.exit_order_id = trade.order.orderId
        state.exit_price = 0.0
        logger.info("Doji %s: exit at market in regular hours, %s shares", state.symbol, qty)
    elif is_extended_session() and bid:
        limit = doji.extended_hours_exit_limit(bid, below)
        trade = await ibkr_client.place_limit_order(contract, "SELL", qty, limit, tif="DAY")
        state.exit_order_id = trade.order.orderId
        state.exit_price = limit
