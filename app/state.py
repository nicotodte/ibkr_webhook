import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
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
    # Average fill price of the entry order. Kept here because IBKR's
    # Position.avgCost folds the commission into the per-share figure, which
    # at small share counts sits far above what was actually paid.
    entry_price: float
    status: str = "open"  # open | closing | closed
    # Everything below is only used by Doji trades (strategy="doji"); the
    # defaults keep older state files loading.
    strategy: str = "tf"  # tf | doji
    tier: str = ""
    risk_per_share: float = 0.0
    high_water: float = 0.0
    trailing: bool = False
    # Resting take-profit limit orders: [{"order_id", "quantity", "price"}]
    targets: list = field(default_factory=list)
    exit_order_id: int = 0
    exit_price: float = 0.0
    # Journal data (Doji trades)
    opened_at: str = ""
    alert_entry: float = 0.0
    alert_stop: float = 0.0
    initial_quantity: int = 0
    low_water: float = 0.0
    fills: list = field(default_factory=list)


DOJI_KEY_PREFIX = "DOJI:"


def doji_key(symbol: str) -> str:
    """Doji trades are stored under their own key, so a Doji and a TF trade
    on the same symbol never overwrite each other's state."""
    return DOJI_KEY_PREFIX + symbol


def store_key(state: TradeState) -> str:
    return doji_key(state.symbol) if state.strategy == "doji" else state.symbol


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

    async def all_open(self) -> list[TradeState]:
        async with self._lock:
            return [t for t in self._trades.values() if t.status in ("open", "closing")]


position_store = PositionStore(settings.state_file)
