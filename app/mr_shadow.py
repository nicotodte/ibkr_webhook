"""Index mean reversion, signal logging only ("shadow"): once a day, a few
minutes before the close, the bot checks SPY/QQQ/IWM/DIA against the MR rule
(see mr_logic.py) and books what it WOULD have done at the real bid/ask, with
the commission IBKR quotes for that order (a what-if preview, which IBKR
never executes). No order is ever sent from here.

Files: mr_signals.csv (every daily check per symbol), mr_trades.csv (every
finished shadow trade), mr_shadow.json (open shadow positions).
"""
import asyncio
import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from ib_async import LimitOrder

import journal
import mr_logic
from config import settings
from ibkr_client import ibkr_client

logger = logging.getLogger("ibkr_webhook.mr_shadow")

_lock = asyncio.Lock()
_close_cache: dict = {}   # day -> close time (or None = closed)


def _load_state() -> dict:
    try:
        with open(settings.mr_state_file, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {"positions": {}, "last_run_day": "", "last_exit_day": {}}
    except Exception:
        logger.exception("MR state unreadable, starting empty")
        return {"positions": {}, "last_run_day": "", "last_exit_day": {}}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(settings.mr_state_file) or ".", exist_ok=True)
    tmp = settings.mr_state_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, settings.mr_state_file)


def symbols() -> list:
    return [s.strip().upper() for s in settings.mr_symbols.split(",") if s.strip()]


async def _session_close(day) -> Optional[datetime]:
    """Today's close in exchange time from IBKR's trading hours of the first
    symbol (16:00, 13:00 on half days, None on holidays)."""
    if day in _close_cache:
        return _close_cache[day]
    tz = ZoneInfo(settings.trading_timezone)
    stock = await ibkr_client.qualify_stock(symbols()[0], settings.default_exchange, settings.default_currency)
    details = await asyncio.wait_for(ibkr_client.ib.reqContractDetailsAsync(stock), timeout=15)
    hours = details[0].liquidHours if details else ""
    t = mr_logic.session_close(hours, day)
    close = datetime.combine(day, t, tzinfo=tz) if t else None
    _close_cache[day] = close
    return close


async def _daily_closes(stock) -> list:
    bars = await asyncio.wait_for(
        ibkr_client.ib.reqHistoricalDataAsync(stock, "", "1 Y", "1 day", "TRADES", True, 1),
        timeout=30,
    )
    out = []
    for b in bars or []:
        d = b.date.date() if isinstance(b.date, datetime) else b.date
        out.append((d, float(b.close)))
    return out


async def _quote(contract) -> tuple:
    samples = await ibkr_client.get_quote_samples(contract, max_wait=0.0, accept=lambda s: True, timeout=10.0)
    q = samples[-1]
    return float(q.bid), float(q.ask)


async def _commission(contract, action: str, qty: int, price: float) -> tuple:
    """(commission USD, source). IBKR's what-if preview of this exact order
    gives the account's real commission; it is never executed."""
    if settings.mr_use_whatif:
        try:
            order = LimitOrder(action, qty, round(price, 2))
            order.tif = "DAY"
            st = await asyncio.wait_for(ibkr_client.ib.whatIfOrderAsync(contract, order), timeout=15)
            if st is not None:
                if mr_logic.valid_number(st.commission):
                    return float(st.commission), "ibkr"
                if mr_logic.valid_number(st.minCommission) and mr_logic.valid_number(st.maxCommission):
                    return (float(st.minCommission) + float(st.maxCommission)) / 2, "ibkr_range"
        except Exception as exc:
            logger.warning("MR what-if for %s failed: %s", contract.symbol, exc)
    return mr_logic.fallback_commission(qty, price=price), "fixed_table"


async def _contracts(symbol: str):
    """(contract the trade would use, ETF contract for prices and bars)."""
    stock = await ibkr_client.qualify_stock(symbol, settings.default_exchange, settings.default_currency)
    if settings.mr_instrument.upper() == "CFD":
        return await ibkr_client.qualify_cfd(symbol, settings.default_currency), stock
    return stock, stock


def _row(**kw) -> dict:
    return {"time_utc": journal.now_utc(), **kw}


async def check_symbol(symbol: str, state: dict, day, now_iso: str) -> dict:
    trade_c, stock = await _contracts(symbol)
    bars = await _daily_closes(stock)
    bid_e, ask_e = await _quote(stock)
    detail = ""
    try:
        bid, ask = await _quote(trade_c) if trade_c is not stock else (bid_e, ask_e)
    except Exception as exc:
        bid, ask = bid_e, ask_e
        detail = f"CFD-Kurs fehlte, ETF-Kurs genommen ({str(exc)[:80]})"
    live = (bid_e + ask_e) / 2
    closes = mr_logic.with_today(bars, day, live)
    ind = mr_logic.indicators(closes)
    base = dict(day=day.isoformat(), symbol=symbol, live_price=round(live, 4), bid=bid, ask=ask,
                rsi2=round(ind.rsi2, 2) if ind.rsi2 is not None else "",
                sma5=round(ind.sma5, 4) if ind.sma5 else "", sma200=round(ind.sma200, 4) if ind.sma200 else "",
                bars=len(closes), detail=detail)
    if len(closes) < 201:
        return _row(action="none", reason="too_few_bars", **base)

    pos_d = state["positions"].get(symbol)
    if pos_d:
        pos = mr_logic.ShadowPosition.from_dict(pos_d)
        if not ind.exit_signal:
            return _row(action="hold", reason="close_not_above_sma5", quantity=pos.quantity, **base)
        comm, src = await _commission(trade_c, "SELL", pos.quantity, bid)
        trade = mr_logic.close_trade(
            pos, exit_day=day.isoformat(), closed_utc=now_iso, exit_bid=bid, exit_ask=ask, exit_last=live,
            exit_commission=comm, exit_commission_source=src,
            financing_pct_year=settings.mr_cfd_financing_pct_year if settings.mr_instrument.upper() == "CFD" else 0.0,
            sma5=ind.sma5,
        )
        journal.append_row(settings.mr_trades_file, mr_logic.TRADE_FIELDS, trade)
        del state["positions"][symbol]
        state.setdefault("last_exit_day", {})[symbol] = day.isoformat()
        return _row(action="sell", reason="close_above_sma5", quantity=pos.quantity, commission_usd=comm,
                    commission_source=src, **base)

    if not ind.entry_signal:
        return _row(action="none", reason="no_signal", **base)
    if state.get("last_exit_day", {}).get(symbol) == day.isoformat():
        return _row(action="none", reason="sold_today", **base)
    if ask <= 0:
        return _row(action="none", reason="no_ask", **base)
    qty = int(settings.mr_notional_usd // ask)
    if qty < 1:
        return _row(action="none", reason="quantity_below_1", **base)
    comm, src = await _commission(trade_c, "BUY", qty, ask)
    pos = mr_logic.ShadowPosition(
        symbol=symbol, opened_utc=now_iso, entry_day=day.isoformat(), quantity=qty, entry_ask=ask,
        entry_bid=bid, entry_last=live, entry_commission=comm, commission_source=src,
        rsi2=ind.rsi2, sma200=ind.sma200, notional_usd=qty * ask,
    )
    state["positions"][symbol] = pos.to_dict()
    return _row(action="buy", reason="rsi2_below_10_above_sma200", quantity=qty, commission_usd=comm,
                commission_source=src, **base)


async def run_if_due(now: Optional[datetime] = None) -> Optional[str]:
    """Called every few seconds; does the day's check once, in the
    MR_MINUTES_BEFORE_CLOSE minutes before the close."""
    if not settings.mr_shadow_enabled:
        return None
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(settings.trading_timezone)
    local = now.astimezone(tz)
    if local.weekday() >= 5:
        return None
    async with _lock:
        state = _load_state()
        day = local.date()
        if state.get("last_run_day") == day.isoformat():
            return None
        close = await _session_close(day)
        if close is None:
            state["last_run_day"] = day.isoformat()
            _save_state(state)
            return "closed"
        start = close - timedelta(minutes=settings.mr_minutes_before_close)
        if local < start:
            return None
        if local >= close:
            # Missed the window (bot down at the close): say so once.
            missing = [x for x in symbols() if x not in state.get("checked", {}).get("symbols", [])
                       or state.get("checked", {}).get("day") != day.isoformat()]
            for sym in missing:
                journal.append_row(settings.mr_signals_file, mr_logic.SIGNAL_FIELDS, _row(
                    day=day.isoformat(), symbol=sym, action="missed", reason="no_check_before_close"))
            state["last_run_day"] = day.isoformat()
            _save_state(state)
            return "missed"
        now_iso = now.isoformat(timespec="seconds")
        checked = state.get("checked", {})
        if checked.get("day") != day.isoformat():
            checked = {"day": day.isoformat(), "symbols": []}
        for sym in symbols():
            if sym in checked["symbols"]:
                continue
            try:
                row = await check_symbol(sym, state, day, now_iso)
                checked["symbols"].append(sym)
            except Exception as exc:
                # Not marked as checked: tried again on the next pass until
                # the close (e.g. no market data while the user is logged in).
                logger.exception("MR check failed for %s", sym)
                row = _row(day=day.isoformat(), symbol=sym, action="error", reason=type(exc).__name__,
                           detail=str(exc)[:300])
            journal.append_row(settings.mr_signals_file, mr_logic.SIGNAL_FIELDS, row)
            logger.info("MR %s: %s (%s)", sym, row.get("action"), row.get("reason"))
            state["checked"] = checked
            _save_state(state)
        if all(s in checked["symbols"] for s in symbols()):
            state["last_run_day"] = day.isoformat()
            _save_state(state)
            return "done"
        return "retry"


def stats() -> dict:
    trades = journal.read_all(settings.mr_trades_file)
    state = _load_state()
    s = mr_logic.summary(trades, list(state.get("positions", {}).values()))
    s["mode"] = "nur Signale, keine Orders"
    s["instrument"] = settings.mr_instrument
    s["notional_per_position_usd"] = settings.mr_notional_usd
    s["last_check_day"] = state.get("last_run_day", "")
    s["recent_checks"] = journal.read_rows(settings.mr_signals_file)[-8:]
    return s
