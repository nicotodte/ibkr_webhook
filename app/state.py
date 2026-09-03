import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from config import settings

logger = logging.getLogger("ibkr_webhook.state")


@dataclass
class TradeState:
    symbol: str
    trade_id: str
    quantity: int
    stop_order_id: int
    stop_price: float
    status: str = "open"  # open | closed


class PositionStore:
    """Tracks bot-managed open positions, keyed by symbol.

    Persisted to a JSON file so an open trade's ladder state (which levels
    already fired, the current stop order) survives a webhook container
    restart -- important since a live trade can stay open for hours.
    """

    def __init__(self, path: str):
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._trades: dict[str, TradeState] = {}
        self._load()

    def _load(self):
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
            self._trades = {k: TradeState(**v) for k, v in raw.items()}
        except Exception:
            logger.exception("Failed to load position store from %s", self._path)

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: asdict(v) for k, v in self._trades.items()}, indent=2))
        tmp.replace(self._path)

    async def get(self, symbol: str) -> Optional[TradeState]:
        async with self._lock:
            return self._trades.get(symbol)

    async def set(self, symbol: str, state: TradeState):
        async with self._lock:
            self._trades[symbol] = state
            self._save()

    async def delete(self, symbol: str):
        async with self._lock:
            self._trades.pop(symbol, None)
            self._save()


position_store = PositionStore(settings.state_file)
