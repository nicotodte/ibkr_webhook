import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from config import settings
from handlers import (
    flatten_all_before_close,
    handle_doji,
    handle_entry,
    handle_exit_all,
    handle_level,
    handle_stop_to_breakeven,
    manage_doji_positions,
)
import journal
from ibkr_client import ibkr_client
from models import BotAlert
from state import position_store
from trading_hours import is_within_closing_flatten_window

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ibkr_webhook.main")

FLATTEN_POLL_INTERVAL_SECONDS = 30
FX_REFRESH_INTERVAL_SECONDS = 300


async def _flatten_before_close_loop():
    # Guards against re-flattening on every poll tick while the window is
    # open: only fires once per calendar day, then waits for the window to
    # pass before it can arm again the following day.
    last_flattened_date = None
    while True:
        try:
            now = datetime.now(timezone.utc)
            if is_within_closing_flatten_window(now) and last_flattened_date != now.date():
                logger.info("Flatten window reached, closing all open bot positions")
                await flatten_all_before_close()
                last_flattened_date = now.date()
        except Exception:
            logger.exception("Error while checking/executing pre-close flatten")
        await asyncio.sleep(FLATTEN_POLL_INTERVAL_SECONDS)


async def _fx_refresh_loop():
    # Keeps the EUR/USD rate warm so an entry never waits on a live request
    # for it. Only refreshes while connected: a reconnect here could race the
    # one a webhook request or health check is already making.
    while True:
        try:
            if ibkr_client.ib.isConnected():
                await ibkr_client.refresh_fx_rate(settings.fx_pair)
        except Exception as exc:
            logger.warning("Could not refresh %s rate: %s", settings.fx_pair, exc)
        await asyncio.sleep(FX_REFRESH_INTERVAL_SECONDS)


async def _doji_monitor_loop():
    # Drives every open Doji trade: stop ladder, trail, target fills and the
    # exit outside regular hours. Skips passes while the gateway is down
    # rather than reconnecting here, like the FX loop.
    while True:
        try:
            if ibkr_client.ib.isConnected():
                await manage_doji_positions()
        except Exception:
            logger.exception("Error in the Doji monitor")
        await asyncio.sleep(settings.doji_monitor_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await ibkr_client.connect()
    except Exception:
        logger.exception("Could not connect to IB Gateway at startup, will retry on first request")
    flatten_task = asyncio.create_task(_flatten_before_close_loop())
    fx_task = asyncio.create_task(_fx_refresh_loop())
    doji_task = asyncio.create_task(_doji_monitor_loop())
    yield
    flatten_task.cancel()
    fx_task.cancel()
    doji_task.cancel()
    await ibkr_client.disconnect()


app = FastAPI(title="TradingView -> IBKR Webhook", lifespan=lifespan)

EVENT_HANDLERS = {
    "entry": handle_entry,
    "stop_to_breakeven": handle_stop_to_breakeven,
    "level": handle_level,
    "exit_all": handle_exit_all,
    "doji": handle_doji,
}


@app.get("/health")
async def health():
    if not ibkr_client.ib.isConnected():
        try:
            await ibkr_client.connect()
        except Exception:
            logger.warning("Health check: IB Gateway still unreachable", exc_info=True)
    return {"status": "ok", "ib_connected": ibkr_client.ib.isConnected()}


def _check_journal_token(token: str):
    # Off unless JOURNAL_TOKEN is set. A separate token from WEBHOOK_SECRET:
    # it only reads results and ends up in browser history.
    if not settings.journal_token:
        raise HTTPException(status_code=404, detail="Not found")
    if not hmac.compare_digest(token or "", settings.journal_token):
        raise HTTPException(status_code=401, detail="Invalid token")


@app.get("/doji/stats")
async def doji_stats(token: str = ""):
    """Win rate, P&L and R of the closed Doji trades, per colour and total."""
    _check_journal_token(token)
    trades = journal.read_rows(settings.doji_trades_file)
    signals = journal.read_rows(settings.doji_signals_file)
    reasons: dict[str, int] = {}
    for row in signals:
        key = row.get("status", "") + (": " + row["reason"] if row.get("reason") else "")
        reasons[key] = reasons.get(key, 0) + 1
    open_trades = [
        {"symbol": t.symbol, "tier": t.tier, "entry": t.entry_price, "stop": t.stop_price,
         "high": t.high_water, "trailing": t.trailing}
        for t in await position_store.all_open() if t.strategy == "doji"
    ]
    return {
        "closed_trades": journal.stats(trades),
        "signals": {"total": len(signals), "by_outcome": reasons},
        "open_trades": open_trades,
    }


@app.get("/doji/trades.csv")
async def doji_trades_csv(token: str = ""):
    _check_journal_token(token)
    if not os.path.exists(settings.doji_trades_file):
        raise HTTPException(status_code=404, detail="No Doji trades recorded yet")
    return FileResponse(settings.doji_trades_file, media_type="text/csv", filename="doji_trades.csv")


@app.get("/doji/signals.csv")
async def doji_signals_csv(token: str = ""):
    _check_journal_token(token)
    if not os.path.exists(settings.doji_signals_file):
        raise HTTPException(status_code=404, detail="No Doji signals recorded yet")
    return FileResponse(settings.doji_signals_file, media_type="text/csv", filename="doji_signals.csv")


@app.post("/webhook")
async def webhook(request: Request):
    raw_body = await request.body()
    if not raw_body.lstrip().startswith(b"{"):
        # The Longterm script's list alert sends its Achtung/HOLD/K=0 texts to
        # the same webhook as the Doji JSON. They are meant for the phone only.
        logger.info("Ignoring non-JSON alert text: %.80s", raw_body.decode("utf-8", "replace"))
        return {"status": "ignored", "reason": "not_json"}
    try:
        alert = BotAlert.model_validate_json(raw_body)
    except Exception as exc:
        logger.warning("Rejected malformed alert payload: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid alert payload") from exc

    if not hmac.compare_digest(alert.secret, settings.webhook_secret):
        logger.warning("Rejected alert with invalid secret for symbol=%s", alert.symbol)
        raise HTTPException(status_code=401, detail="Invalid secret")

    logger.info("Alert received: event=%s symbol=%s trade_id=%s", alert.event, alert.symbol, alert.trade_id)

    try:
        return await EVENT_HANDLERS[alert.event](alert)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Handling alert failed: event=%s symbol=%s", alert.event, alert.symbol)
        raise HTTPException(status_code=502, detail=f"Order handling failed: {exc}") from exc
