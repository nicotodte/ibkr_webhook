import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from config import settings
from handlers import (
    flatten_all_before_close,
    handle_doji,
    handle_entry,
    handle_exit_all,
    handle_level,
    handle_stop_to_breakeven,
    log_tf_signal,
    manage_doji_positions,
    manage_tf_positions,
)
import journal
import stand
import tf_report
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
TF_MONITOR_INTERVAL_SECONDS = 5


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


async def _tf_monitor_loop():
    # Writes the journal row of finished TF trades (sold by an alert, or
    # stopped out at IBKR). Skips passes while the gateway is down.
    while True:
        try:
            if ibkr_client.ib.isConnected():
                await manage_tf_positions()
        except Exception:
            logger.exception("Error in the TF journal monitor")
        await asyncio.sleep(TF_MONITOR_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        journal.set_stand(stand.register(
            settings.versions_file, os.path.dirname(os.path.abspath(__file__)), settings.model_dump()
        ))
        logger.info("Bot Stand %s (listed in %s)", journal.STAND, settings.versions_file)
    except Exception:
        logger.exception("Could not record the Stand of the bot")
    try:
        await ibkr_client.connect()
    except Exception:
        logger.exception("Could not connect to IB Gateway at startup, will retry on first request")
    flatten_task = asyncio.create_task(_flatten_before_close_loop())
    fx_task = asyncio.create_task(_fx_refresh_loop())
    doji_task = asyncio.create_task(_doji_monitor_loop())
    tf_task = asyncio.create_task(_tf_monitor_loop())
    yield
    tf_task.cancel()
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
    return {"status": "ok", "ib_connected": ibkr_client.ib.isConnected(), "stand": journal.STAND}


def journal_token() -> str:
    # JOURNAL_TOKEN if set. Otherwise one derived from WEBHOOK_SECRET, so the
    # journal pages work without another setting. It is not the secret itself
    # (it only reads results and ends up in browser history), and nobody can
    # get the secret back from it.
    if settings.journal_token:
        return settings.journal_token
    return hmac.new(settings.webhook_secret.encode(), b"journal", "sha256").hexdigest()[:24]


def _check_journal_token(token: str):
    if not hmac.compare_digest(token or "", journal_token()):
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
        "by_stand": journal.stats_by_stand(journal.read_all(settings.doji_trades_file)),
        "signals": {"total": len(signals), "by_outcome": reasons},
        "open_trades": open_trades,
    }


@app.get("/doji/tv.txt")
async def doji_tv(token: str = ""):
    """Text for the "Doji Bot Trades" indicator in TradingView: copy it into
    the indicator's settings and every bot trade shows up on its chart."""
    _check_journal_token(token)
    open_trades = [
        {"symbol": t.symbol, "tier": t.tier, "trade_id": t.trade_id, "entry": t.entry_price, "stop": t.stop_price}
        for t in await position_store.all_open() if t.strategy == "doji"
    ]
    text = journal.tv_export(
        journal.read_all(settings.doji_trades_file),
        journal.read_all(settings.doji_signals_file),
        open_trades,
    )
    return PlainTextResponse(text or "noch keine Doji-Signale")


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


@app.get("/versions.csv")
async def versions_csv(token: str = ""):
    """Every Stand of the bot (code + settings) with what changed."""
    _check_journal_token(token)
    if not os.path.exists(settings.versions_file):
        raise HTTPException(status_code=404, detail="No versions recorded yet")
    return FileResponse(settings.versions_file, media_type="text/csv", filename="versions.csv")


@app.get("/tf/stats")
async def tf_stats(token: str = ""):
    """Win rate, P&L and R of the closed TF trades, per entry level and total."""
    _check_journal_token(token)
    trades = journal.read_all(settings.tf_trades_file)
    signals = journal.read_all(settings.tf_signals_file)
    reasons: dict[str, int] = {}
    for row in signals:
        if row.get("event") != "entry":
            continue
        key = row.get("status", "") + (": " + row["reason"] if row.get("reason") else "")
        reasons[key] = reasons.get(key, 0) + 1
    return {
        "stand": journal.STAND,
        "closed_trades": journal.tf_stats(trades),
        "by_stand": journal.stats_by_stand(trades),
        "entry_signals": reasons,
    }


@app.get("/tf/report", response_class=HTMLResponse)
async def tf_report_page(token: str = ""):
    """The TF trade journal as a readable page."""
    _check_journal_token(token)
    open_trades = [
        {"symbol": t.symbol, "opened_at": t.opened_at, "entry_level": t.entry_level,
         "quantity": t.quantity, "entry": t.entry_price, "stop": t.stop_price, "levels_hit": t.levels_hit}
        for t in await position_store.all_open() if t.strategy == "tf"
    ]
    return tf_report.render(
        journal.read_all(settings.tf_trades_file),
        journal.read_all(settings.tf_signals_file),
        open_trades,
        journal.read_rows(settings.versions_file),
        journal.STAND,
    )


@app.get("/tf/trades.csv")
async def tf_trades_csv(token: str = ""):
    _check_journal_token(token)
    if not os.path.exists(settings.tf_trades_file):
        raise HTTPException(status_code=404, detail="No TF trades recorded yet")
    return FileResponse(settings.tf_trades_file, media_type="text/csv", filename="tf_trades.csv")


@app.get("/tf/signals.csv")
async def tf_signals_csv(token: str = ""):
    _check_journal_token(token)
    if not os.path.exists(settings.tf_signals_file):
        raise HTTPException(status_code=404, detail="No TF signals recorded yet")
    return FileResponse(settings.tf_signals_file, media_type="text/csv", filename="tf_signals.csv")


_background: set = set()


async def _run_doji(alert: BotAlert):
    try:
        result = await handle_doji(alert)
        logger.info("Doji %s finished: %s", alert.symbol, result)
    except Exception:
        logger.exception("Handling Doji alert failed: symbol=%s", alert.symbol)


@app.post("/webhook")
async def webhook(request: Request):
    raw_body = await request.body()
    if not raw_body.lstrip().startswith(b"{"):
        # The Longterm script's list alert sends its Achtung/HOLD/K=0 texts to
        # the same webhook as the Doji JSON. They are meant for the phone only.
        text = raw_body.decode("utf-8", "replace")
        logger.info("Ignoring non-JSON alert text: %.80s", text)
        try:
            journal.append_row(settings.other_alerts_file, journal.OTHER_ALERT_FIELDS, {
                "time_utc": journal.now_utc(), "text": text.strip()[:400].replace("\n", " | "),
            })
        except Exception:
            logger.exception("Could not write the ignored-alerts journal")
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

    if alert.event == "doji":
        # TradingView gives up on a webhook after about 3 seconds and logs a
        # delivery error; a Doji entry (quote check, buy, waiting for the
        # fill, placing seven orders) takes longer. So answer at once and run
        # it in the background. The outcome is in the log and doji_signals.csv.
        task = asyncio.create_task(_run_doji(alert))
        _background.add(task)
        task.add_done_callback(_background.discard)
        return {"status": "accepted", "symbol": alert.symbol, "event": "doji"}

    try:
        result = await EVENT_HANDLERS[alert.event](alert)
        log_tf_signal(alert, result)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Handling alert failed: event=%s symbol=%s", alert.event, alert.symbol)
        log_tf_signal(alert, {"status": "error", "detail": str(exc)[:300]})
        raise HTTPException(status_code=502, detail=f"Order handling failed: {exc}") from exc
