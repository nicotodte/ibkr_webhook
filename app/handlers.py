import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import settings
from ibkr_client import DelayedMarketDataError, ibkr_client
from models import BotAlert
from sizing import SizingError, calculate_quantity
from spread import assess_entry_quotes
from state import TradeState, doji_key, position_store, store_key
import doji
import journal
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
    use_margin: bool = False,
    skip_spread_checks: bool = False,
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
            0.0 if skip_spread_checks else settings.max_spread_pct,
            0.0 if skip_spread_checks else settings.max_spread_pct_of_risk,
            0.0 if skip_spread_checks
            else settings.max_entry_slippage_pct_of_risk
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
        return {"status": "rejected", "reason": "delayed_market_data", "detail": str(exc)[:300]}
    except Exception as exc:
        logger.warning("Rejecting entry for %s: no live quote (%s)", alert.symbol, exc)
        return {"status": "rejected", "reason": "no_market_data", "detail": str(exc)[:300]}

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

    # Without spread checks the buy can fill well above the alert price;
    # sizing off the ask keeps the dollar risk at the budget.
    # Sized off the ask either way: above the alert price it keeps the dollar
    # risk at the budget, below it the position isn't too small (SNDK 13.11.:
    # 9 shares for 9.70 USD of risk instead of 23 for 25 USD).
    size_entry = ask if skip_spread_checks else alert.entry
    try:
        quantity = calculate_quantity(size_entry, alert.stop, fx_rate, risk_usd)
    except SizingError as exc:
        logger.warning("Sizing rejected entry for %s: %s", alert.symbol, exc)
        return {"status": "rejected", "reason": str(exc)}

    notional_eur = (quantity * ask) / fx_rate
    funds_label = "available funds incl. margin" if use_margin else "cash"
    try:
        if use_margin:
            available_cash_eur = await ibkr_client.get_available_funds_base_currency()
        else:
            available_cash_eur = await ibkr_client.get_available_cash_base_currency()
    except Exception as exc:
        logger.warning("Rejecting entry for %s: no %s balance (%s)", alert.symbol, funds_label, exc)
        return {"status": "rejected", "reason": "no_cash_balance"}

    if notional_eur > available_cash_eur:
        logger.info(
            "Rejecting entry for %s: needs ~EUR %.2f, only EUR %.2f %s",
            alert.symbol, notional_eur, available_cash_eur, funds_label,
        )
        return {"status": "rejected", "reason": "insufficient_cash"}

    return contract, quantity, quote, fx_rate, available_cash_eur


async def handle_entry(alert: BotAlert) -> dict:
    existing = await position_store.get(alert.symbol)
    if existing and existing.status == "journal_pending":
        await _tf_finalize(existing)
    elif existing and existing.status == "open":
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
        await _tf_finalize(existing)

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
            "bid": quote.bid,
            "ask": quote.ask,
            "spread_pct": spread_pct,
            "fx_rate": fx_rate,
        }

    opened_at = datetime.now(timezone.utc) - timedelta(seconds=30)
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
            opened_at=opened_at.isoformat(),
            alert_entry=alert.entry,
            alert_stop=alert.stop,
            initial_quantity=filled_qty,
            entry_level=journal.entry_level_from(alert.level, alert.info),
            entry_info=alert.info or "",
            stand=journal.STAND,
        ),
    )

    return {
        "status": "submitted",
        "symbol": alert.symbol,
        "quantity": filled_qty,
        "requested_quantity": quantity,
        "fill_price": buy_trade.orderStatus.avgFillPrice,
        "bid": quote.bid,
        "ask": quote.ask,
        "spread_pct": spread_pct,
        "spread_pct_of_risk": quote.spread_pct_of_risk,
        "slippage_pct_of_risk": quote.slippage_pct_of_risk,
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
        await _tf_finalize(state)
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
    state.levels_hit.append("0.843=Stop auf Einstand")
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
        await _tf_finalize(state)
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
        state.levels_hit.append(alert.level or "?")
        await position_store.set(alert.symbol, state)
    else:
        await ibkr_client.cancel_order(state.stop_order_id)
        state.levels_hit.append(alert.level or "?")
        await _tf_mark_closed(state, f"Level {alert.level}: Rest verkauft")

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

    await _tf_mark_closed(state, "exit_all (-0.236)")
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
        if (state.strategy == "doji" and state.trailing and settings.doji_keep_trailing_overnight
                and state.sec_type != "CFD"):
            logger.info("Keeping trailing Doji position %s open through the close", state.symbol)
            continue
        try:
            pos = await ibkr_client.get_position(state.symbol)
            qty = int(pos.position) if pos else 0

            for order_id in _order_ids(state):
                await ibkr_client.cancel_order(order_id)
            if state.strategy == "doji" and _order_ids(state):
                # Let the cancels land, then sell only what is still held: a
                # target or stop filling in between must not be sold twice.
                await asyncio.sleep(1.5)
                pos = await ibkr_client.get_position(state.symbol)
                qty = int(pos.position) if pos else 0
            contract = None
            if qty > 0:
                if state.strategy == "doji":
                    contract, _ = await _doji_contracts(state.symbol, state.sec_type)
                else:
                    contract = await ibkr_client.qualify_stock(
                        state.symbol, settings.default_exchange, settings.default_currency
                    )
                sell = await ibkr_client.place_market_order(contract, "SELL", qty)
                if state.strategy == "doji":
                    try:
                        await ibkr_client.wait_for_fill(sell)
                    except Exception:
                        logger.warning("Flatten sell for %s not confirmed for the journal", state.symbol)
            if state.strategy == "doji":
                await _stop_doji_stream(state.symbol)
                _sync_fills(state)
                _journal_close(state, "flatten_before_close")

            if state.strategy == "tf":
                await _tf_mark_closed(state, "flatten_before_close")
            else:
                await position_store.delete(store_key(state))
            logger.info("Flattened %s before close: sold %s", state.symbol, qty)
            closed.append({"symbol": state.symbol, "sold": qty})
        except Exception:
            logger.exception("Failed to flatten %s before close", state.symbol)

    return {"status": "flattened", "positions": closed}


def _order_ids(state: TradeState) -> list[int]:
    """Every order the bot may still have resting for this trade."""
    ids = [state.stop_order_id] + [t["order_id"] for t in state.targets]
    ids += [t.get("stop_order_id", 0) for t in state.targets]
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


_breadth = doji.Breadth(settings.doji_breadth_window_minutes * 60)


async def handle_doji(alert: BotAlert) -> dict:
    # Counted before any check: every Doji alert that arrives says something
    # about the market, also the ones that are not bought.
    breadth = _breadth.add(time.time(), alert.symbol)
    result = await _handle_doji(alert, breadth)
    try:
        journal.append_row(settings.doji_signals_file, journal.SIGNAL_FIELDS, {
            "time_utc": journal.now_utc(),
            "symbol": alert.symbol,
            "tier": alert.tier or "",
            "trade_id": alert.trade_id,
            "alert_entry": alert.entry,
            "alert_stop": alert.stop,
            "status": result.get("status", ""),
            "reason": result.get("reason", ""),
            "quantity": result.get("quantity", ""),
            "bid": result.get("bid", ""),
            "ask": result.get("ask", ""),
            "spread_pct": _r(result.get("spread_pct")),
            "spread_pct_of_risk": _r(result.get("spread_pct_of_risk")),
            "slippage_pct_of_risk": _r(result.get("slippage_pct_of_risk")),
            "detail": result.get("detail", ""),
            "breadth": breadth,
            "risk_usd": result.get("risk_usd", ""),
            "level": alert.level or "",
            "info": alert.info or "",
        })
    except Exception:
        logger.exception("Could not write the Doji signal journal")
    return result


def _order_detail(trade) -> str:
    """Order status plus IBKR's own messages (rejections, precaution
    warnings) -- the reason a buy did not fill."""
    msgs = [e.message for e in getattr(trade, "log", []) if getattr(e, "message", "")]
    return (f"status={trade.orderStatus.status}; " + " | ".join(msgs))[:300]


ETF_STOCK_TYPES = {"ETF", "ETN", "ETC", "ETP"}


async def _doji_contracts(symbol: str, sec_type: str):
    """(contract to trade, contract for prices). A CFD is traded, but its
    prices come from the ETF itself: same price, and the stock quote is the
    one the account has market data for."""
    stock = await ibkr_client.qualify_stock(symbol, settings.default_exchange, settings.default_currency)
    if sec_type == "CFD":
        return await ibkr_client.qualify_cfd(symbol, settings.default_currency), stock
    return stock, stock


def _r(x):
    return round(x, 3) if isinstance(x, (int, float)) else ""


async def _handle_doji(alert: BotAlert, breadth: int = 0) -> dict:
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
        _sync_fills(existing)
        _journal_close(existing, "closed_while_bot_was_down")
        await position_store.delete(key)

    tf = await position_store.get(alert.symbol)
    if tf and tf.status == "open":
        logger.info("Ignoring Doji for %s: a TF trade is open on it", alert.symbol)
        return {"status": "ignored", "reason": "tf_position_open"}

    if not is_within_doji_entry_hours():
        logger.info("Ignoring Doji for %s: outside the Doji entry window", alert.symbol)
        return {"status": "ignored", "reason": "outside_trading_hours"}

    # More risk when many Dojis come at once (broad sell-off), less for a
    # lone one -- every signal is still taken.
    mult = settings.doji_breadth_high_mult if breadth >= settings.doji_breadth_min_signals else settings.doji_breadth_low_mult
    risk_usd = _doji_risk(alert.tier) * mult
    logger.info("Doji %s: %s other Dojis in the last %.0f min -> risk x%.1f = %.0f USD",
                alert.symbol, breadth, settings.doji_breadth_window_minutes, mult, risk_usd)
    checked = await _pretrade_checks(
        alert,
        risk_usd,
        settings.doji_max_entry_slippage_pct_of_risk,
        settings.doji_use_margin,
        settings.doji_skip_spread_checks,
    )
    if isinstance(checked, dict):
        return checked
    contract, quantity, quote, fx_rate, available_cash_eur = checked

    if settings.doji_reject_bid_at_stop and quote.bid <= alert.stop:
        logger.info("Rejecting Doji for %s: bid %.2f already at or below the stop %.2f",
                    alert.symbol, quote.bid, alert.stop)
        return {"status": "rejected", "reason": "bid_at_stop", "bid": quote.bid, "ask": quote.ask,
                "detail": f"stop {alert.stop}", "risk_usd": risk_usd}

    # US ETFs: an EU retail account gets IBKR error 201 (no KID) on the ETF
    # itself, so the CFD on it is traded. Prices still come from the ETF.
    sec_type = "STK"
    data_contract = contract
    if settings.doji_etf_as_cfd:
        try:
            is_etf = await ibkr_client.stock_type(contract) in ETF_STOCK_TYPES
        except Exception:
            logger.warning("Could not read the stock type of %s", alert.symbol, exc_info=True)
            is_etf = False
        if is_etf:
            try:
                contract = await ibkr_client.qualify_cfd(alert.symbol, settings.default_currency)
                sec_type = "CFD"
            except Exception as exc:
                logger.warning("Rejecting Doji for %s: ETF without CFD (%s)", alert.symbol, exc)
                return {"status": "rejected", "reason": "no_cfd", "detail": str(exc)[:300]}

    # Pay at most the alert price plus a share of its risk. The size comes from
    # that worst case, so the dollar risk stays within the colour's budget
    # however the fill comes (AMAT 17.11.: market buy 0.49 above the ask, 111
    # instead of 25 USD at risk).
    limit = None
    if settings.doji_entry_cap_pct_of_risk > 0:
        cap = doji.entry_limit(alert.entry, alert.stop, settings.doji_entry_cap_pct_of_risk)
        if quote.ask > cap:
            logger.info("Rejecting Doji for %s: ask %.2f already above the limit %.2f", alert.symbol, quote.ask, cap)
            return {"status": "rejected", "reason": "price_ran_away", "bid": quote.bid, "ask": quote.ask,
                    "detail": f"limit {cap}", "risk_usd": risk_usd}
        # A little above the ask, sized from that limit: the risk stays within
        # the budget and a normal fill uses ~90 % of it (sizing from the cap
        # itself left FCX 18.11. at 26 of 50 USD).
        limit, size_price = doji.buy_limit(alert.entry, alert.stop, quote.ask,
                                           settings.doji_entry_cap_pct_of_risk,
                                           settings.doji_entry_above_ask_pct_of_risk)
        try:
            quantity = calculate_quantity(size_price, alert.stop, fx_rate, risk_usd)
        except SizingError as exc:
            return {"status": "rejected", "reason": str(exc), "risk_usd": risk_usd}
    max_qty = int(settings.doji_max_position_eur * fx_rate / (limit or quote.ask))
    if quantity > max_qty:
        logger.info("Doji %s: %s shares capped to %s (max position %.0f EUR)", alert.symbol, quantity, max_qty, settings.doji_max_position_eur)
        quantity = max_qty
    if quantity < 1:
        return {"status": "rejected", "reason": "quantity_below_one", "risk_usd": risk_usd}

    logger.info(
        "Doji accepted: %s %s qty=%s entry=%s stop=%s risk=%.0f USD spread=%.3f%%",
        alert.symbol, alert.tier, quantity, alert.entry, alert.stop, risk_usd, quote.spread_pct,
    )
    if settings.dry_run:
        return {"status": "dry_run", "symbol": alert.symbol, "tier": alert.tier, "quantity": quantity}

    # A little before the order, so the buy execution falls inside the
    # journal window even with a few seconds of clock difference to IBKR.
    opened_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    if limit is not None:
        buy_trade = await ibkr_client.place_limit_order(contract, "BUY", quantity, limit, outside_rth=False, tif="DAY")
    else:
        buy_trade = await ibkr_client.place_market_order(contract, "BUY", quantity)
    try:
        await ibkr_client.wait_for_fill(buy_trade, timeout=settings.doji_entry_fill_timeout_seconds)
    except Exception as exc:
        logger.warning("Doji buy for %s not filled in time (%s)", alert.symbol, exc)
    if buy_trade.orderStatus.status != "Filled":
        # Never leave a buy working: filling later it would be a position
        # without any stop that the bot doesn't know about. Cancel the rest,
        # give IBKR a moment, then protect whatever did fill.
        await ibkr_client.cancel_order(buy_trade.order.orderId)
        await asyncio.sleep(2)
    filled_qty = int(buy_trade.orderStatus.filled or 0)
    if filled_qty <= 0:
        detail = _order_detail(buy_trade)
        logger.warning("Rejecting Doji for %s: buy order did not fill (%s)", alert.symbol, detail)
        return {"status": "rejected", "reason": "order_not_filled", "detail": detail}
    if filled_qty < quantity:
        logger.warning("Doji %s: only %s of %s filled, protecting those", alert.symbol, filled_qty, quantity)

    fill = buy_trade.orderStatus.avgFillPrice or quote.ask
    stop = doji.floor_cent(alert.stop)
    risk = fill - stop
    # Every quarter with a target gets its own stop, and the two are an OCA
    # pair: when the target fills, IBKR itself cancels (or, on a partial fill,
    # shrinks) that quarter's stop -- and the other way round. So the stops
    # never cover more shares than are still held, with no gap in between
    # that the bot would have to close. The last quarter's stop stands alone
    # and is the one that trails.
    targets = []
    if risk > 0:
        sizes = doji.split_for_tier(filled_qty, alert.tier or "", settings.doji_orange_first_target_share)
        prices = doji.target_prices(fill, risk)
    else:
        # Filled at or below the stop: the stop order takes it straight out.
        logger.warning("Doji %s filled at %.2f, not above its stop %.2f", alert.symbol, fill, stop)
        sizes, prices = [0, 0, 0, filled_qty], []
    for i, (size, price) in enumerate(zip(sizes[:3], prices), start=1):
        if size <= 0:
            continue
        group = f"doji-{alert.symbol}-{alert.trade_id}-{i}"
        st = await ibkr_client.place_stop_order(contract, "SELL", size, stop, oca_group=group)
        # CFDs trade in regular hours only
        tp = await ibkr_client.place_limit_order(contract, "SELL", size, price, outside_rth=sec_type != "CFD", oca_group=group)
        targets.append({
            "order_id": tp.order.orderId,
            "stop_order_id": st.order.orderId,
            "quantity": size,
            "price": price,
        })
    stop_trade = await ibkr_client.place_stop_order(contract, "SELL", sizes[3], stop)

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
        sec_type=sec_type,
        planned_risk_usd=risk_usd,
        risk_per_share=max(risk, 0.0),
        high_water=fill,
        targets=targets,
        opened_at=opened_at.isoformat(),
        alert_entry=alert.entry,
        alert_stop=alert.stop,
        initial_quantity=filled_qty,
        low_water=fill,
        stand=journal.STAND,
    )
    _sync_fills(state)
    await position_store.set(key, state)
    await ibkr_client.stream_ticker(data_contract)
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
        "bid": quote.bid,
        "ask": quote.ask,
        "spread_pct": quote.spread_pct,
        "spread_pct_of_risk": quote.spread_pct_of_risk,
        "slippage_pct_of_risk": quote.slippage_pct_of_risk,
        "requested_quantity": quantity,
        "risk_per_share": round(risk, 4),
        "targets": targets,
        "risk_usd": risk_usd,
        "detail": ("CFD " if sec_type == "CFD" else "") + (f"limit {limit} fill {fill}" if limit is not None else f"fill {fill}"),
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
        _sync_fills(state)
        _journal_close(
            state,
            "extended_hours_exit" if state.status == "closing"
            else "trail" if state.trailing else "stop_or_targets",
        )
        await position_store.delete(key)
        logger.info("Doji %s closed (trade_id=%s)", state.symbol, state.trade_id)
        return

    _flat_passes.pop(key, None)
    contract, data_contract = await _doji_contracts(state.symbol, state.sec_type)
    changed = False

    # A quarter is done once neither its target nor its stop is working any
    # more (target filled -> IBKR cancelled the stop, or the other way round).
    # While its stop still works it stays tracked, so the ladder moves it.
    open_ids = await ibkr_client.open_order_ids()
    still_open = [
        t for t in state.targets
        if t["order_id"] in open_ids or t.get("stop_order_id") in open_ids
    ]
    if len(still_open) != len(state.targets):
        state.targets = still_open
        changed = True

    ticker = await ibkr_client.stream_ticker(data_contract)
    last = ticker.last if ticker.last and ticker.last > 0 else 0.0
    bid = ticker.bid if ticker.bid and ticker.bid > 0 else 0.0
    price = last or bid
    if price > state.high_water:
        state.high_water = price
        changed = True
    if price and (not state.low_water or price < state.low_water):
        state.low_water = price
        changed = True
    if _sync_fills(state):
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

    if new_stop > state.stop_price:
        # Only the price moves. Each stop keeps the size IBKR manages for it
        # through its OCA pair, so the bot never sets a stop size itself.
        stop_ids = [state.stop_order_id] + [t["stop_order_id"] for t in state.targets if t.get("stop_order_id")]
        for order_id in stop_ids:
            await ibkr_client.change_order_price(order_id, new_stop)
        logger.info(
            "Doji %s stop %.2f -> %.2f (high %.2f, %.1f R)",
            state.symbol, state.stop_price, new_stop, state.high_water,
            (state.high_water - state.entry_price) / state.risk_per_share
            if state.risk_per_share else 0.0,
        )
        state.stop_price = new_stop
        changed = True
    if qty != state.quantity:
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
        # The sell goes out on the next pass, once the cancels are through
        # and the position and fills are read again -- a target that fills in
        # the meantime must not be sold a second time.
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
    if state.initial_quantity:
        # IBKR's position can lag a fill by a moment; the executions don't.
        qty = min(qty, int(state.initial_quantity - journal.sold_quantity(state.fills)))
        if qty <= 0:
            return
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


def _sync_fills(state: TradeState) -> bool:
    """Pulls this trade's executions into state.fills (journal and exit
    sizing). Never raises: the journal must not get in the way of trading."""
    if not state.opened_at:
        return False
    try:
        since = datetime.fromisoformat(state.opened_at)
        return journal.merge_fills(state.fills, ibkr_client.fills_for(state.symbol, since))
    except Exception:
        logger.warning("Could not read fills for %s", state.symbol, exc_info=True)
        return False


def _journal_close(state: TradeState, reason: str):
    if not state.opened_at:
        return
    try:
        row = journal.trade_row(
            symbol=state.symbol,
            tier=state.tier,
            trade_id=state.trade_id,
            opened_utc=state.opened_at,
            alert_entry=state.alert_entry,
            alert_stop=state.alert_stop,
            fill_price=state.entry_price,
            initial_stop=state.entry_price - state.risk_per_share,
            quantity=state.initial_quantity,
            planned_risk_usd=state.planned_risk_usd or (_doji_risk(state.tier) if state.tier else 0.0),
            high=state.high_water,
            low=state.low_water,
            reached_trail=state.trailing,
            exit_reason=reason,
            fills=state.fills,
        )
        row["stand"] = state.stand
        journal.append_row(settings.doji_trades_file, journal.TRADE_FIELDS, row)
        logger.info(
            "Doji journal %s %s: net %.2f USD, %s R (%s)",
            state.symbol, state.tier, row["net_pnl_usd"], row["r_multiple"], reason,
        )
    except Exception:
        logger.exception("Could not write the Doji trade journal for %s", state.symbol)


# ------------------------------------------------------------ TF journal

TF_JOURNAL_SETTLE_SECONDS = 10
TF_MIN_AGE_BEFORE_CLOSE_CHECK_SECONDS = 90


def log_tf_signal(alert: BotAlert, result: dict):
    """One row in tf_signals.csv per TF alert. Ladder alerts for a symbol
    without an open trade are left out there (Pine fires them whether or not
    the bot bought) and go to tf_ladder.csv instead. Never raises."""
    if alert.event != "entry":
        try:
            journal.append_row(settings.tf_ladder_file, journal.TF_LADDER_FIELDS, {
                "time_utc": journal.now_utc(),
                "symbol": alert.symbol,
                "event": alert.event,
                "trade_id": alert.trade_id,
                "level": alert.level or "",
                "new_stop": alert.new_stop if alert.new_stop is not None else "",
                "taken": "ja" if result.get("status") in ("stop_updated", "partial_exit", "closed") else "nein",
            })
        except Exception:
            logger.exception("Could not write the TF ladder journal")
    try:
        if alert.event != "entry" and result.get("reason") == "no_open_position":
            return
        detail = result.get("detail", "")
        if alert.event != "entry":
            detail = ", ".join(
                f"{k}={v}" for k, v in result.items() if k not in ("status", "reason", "symbol", "detail")
            )
        journal.append_row(settings.tf_signals_file, journal.TF_SIGNAL_FIELDS, {
            "time_utc": journal.now_utc(),
            "symbol": alert.symbol,
            "event": alert.event,
            "trade_id": alert.trade_id,
            "level": journal.entry_level_from(alert.level, alert.info) if alert.event == "entry" else (alert.level or ""),
            "alert_entry": alert.entry if alert.entry is not None else "",
            "alert_stop": alert.stop if alert.stop is not None else alert.new_stop if alert.new_stop is not None else "",
            "status": result.get("status", ""),
            "reason": result.get("reason", ""),
            "quantity": result.get("quantity", result.get("sold", "")),
            "fill_price": _r(result.get("fill_price")),
            "bid": result.get("bid", ""),
            "ask": result.get("ask", ""),
            "spread_pct": _r(result.get("spread_pct")),
            "spread_pct_of_risk": _r(result.get("spread_pct_of_risk")),
            "slippage_pct_of_risk": _r(result.get("slippage_pct_of_risk")),
            "detail": detail,
            "info": (alert.info or "") if alert.event == "entry" else "",
        })
    except Exception:
        logger.exception("Could not write the TF signal journal")


async def _tf_mark_closed(state: TradeState, reason: str):
    """The position is sold (or being sold). The state stays for a few seconds
    so the fills can arrive, then manage_tf_positions writes the journal row."""
    state.status = "journal_pending"
    state.closed_at = journal.now_utc()
    state.journal_reason = reason
    await position_store.set(state.symbol, state)


async def _tf_price_range(state: TradeState):
    try:
        contract = await ibkr_client.qualify_stock(
            state.symbol, settings.default_exchange, settings.default_currency
        )
        return await ibkr_client.price_range(contract, datetime.fromisoformat(state.opened_at))
    except Exception:
        logger.warning("Could not read the price range for %s", state.symbol, exc_info=True)
        return None, None


async def _tf_finalize(state: TradeState, reason: str = ""):
    """Writes the journal row of a finished TF trade, then drops its state.
    Never raises: the journal must not get in the way of trading."""
    try:
        if state.opened_at:
            _sync_fills(state)
            reason = reason or state.journal_reason
            if not reason:
                sells = [f for f in state.fills if f.get("side") == "SLD"]
                if not sells:
                    reason = "keine Verkäufe gefunden"
                elif state.stop_price and sells[-1]["price"] <= state.stop_price * 1.003:
                    reason = f"stop ({state.stop_price})"
                else:
                    reason = "manuell oder unbekannt"
            high, low = await _tf_price_range(state)
            row = journal.tf_trade_row(
                symbol=state.symbol,
                trade_id=state.trade_id,
                opened_utc=state.opened_at,
                entry_level=state.entry_level,
                alert_entry=state.alert_entry,
                alert_stop=state.alert_stop,
                fill_price=state.entry_price,
                quantity=state.initial_quantity or state.quantity,
                high=high,
                low=low,
                levels_hit=state.levels_hit,
                exit_reason=reason,
                fills=state.fills,
                info=state.entry_info,
                closed_utc=state.closed_at or None,
                stand=state.stand,
            )
            journal.append_row(settings.tf_trades_file, journal.TF_TRADE_FIELDS, row)
            logger.info(
                "TF journal %s level %s: net %s USD, %s R (%s)",
                state.symbol, state.entry_level or "?", row["net_pnl_usd"], row["r_multiple"], reason,
            )
    except Exception:
        logger.exception("Could not write the TF trade journal for %s", state.symbol)
    finally:
        cur = await position_store.get(state.symbol)
        if cur and cur.trade_id == state.trade_id and cur.opened_at == state.opened_at:
            await position_store.delete(state.symbol)


async def manage_tf_positions():
    """Called every few seconds: writes the journal row of TF trades that were
    sold by an alert, and notices trades whose stop filled (IBKR never tells
    the bot) by their position being gone."""
    now = datetime.now(timezone.utc)
    for state in await position_store.all_tf():
        try:
            if state.status == "journal_pending":
                closed = datetime.fromisoformat(state.closed_at) if state.closed_at else now
                if (now - closed).total_seconds() >= TF_JOURNAL_SETTLE_SECONDS:
                    await _tf_finalize(state)
            elif state.opened_at:
                age = (now - datetime.fromisoformat(state.opened_at)).total_seconds()
                if age < TF_MIN_AGE_BEFORE_CLOSE_CHECK_SECONDS:
                    continue
                pos = await ibkr_client.get_position(state.symbol)
                if not pos or pos.position <= 0:
                    logger.info("TF trade %s is no longer held (stop filled?)", state.symbol)
                    await _tf_finalize(state)
        except Exception:
            logger.exception("TF journal check failed for %s", state.symbol)
