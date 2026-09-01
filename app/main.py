import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from .config import settings
from .handlers import handle_entry, handle_exit_all, handle_level, handle_stop_to_breakeven
from .ibkr_client import ibkr_client
from .models import BotAlert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ibkr_webhook.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await ibkr_client.connect()
    except Exception:
        logger.exception("Could not connect to IB Gateway at startup, will retry on first request")
    yield
    await ibkr_client.disconnect()


app = FastAPI(title="TradingView -> IBKR Webhook", lifespan=lifespan)

EVENT_HANDLERS = {
    "entry": handle_entry,
    "stop_to_breakeven": handle_stop_to_breakeven,
    "level": handle_level,
    "exit_all": handle_exit_all,
}


@app.get("/health")
async def health():
    return {"status": "ok", "ib_connected": ibkr_client.ib.isConnected()}


@app.post("/webhook")
async def webhook(request: Request):
    raw_body = await request.body()
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
