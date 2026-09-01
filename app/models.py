from typing import Literal, Optional

from pydantic import BaseModel, field_validator


class BotAlert(BaseModel):
    """Unified JSON shape for every alert the Pine script fires.

    - event="entry": symbol, trade_id, entry, stop are required. Opens a
      new bot-managed position.
    - event="stop_to_breakeven": trade_id must match the open position.
      The bot looks up the actual IBKR fill price itself (not a value from
      TradingView) and moves the stop there.
    - event="level": trade_id must match. Sells ~50% of the current live
      position and moves the stop to new_stop (a price computed by Pine).
    - event="exit_all": trade_id must match. Closes the entire remaining
      position at market and cancels the stop.
    """

    secret: str
    event: Literal["entry", "stop_to_breakeven", "level", "exit_all"]
    symbol: str
    trade_id: str
    entry: Optional[float] = None
    stop: Optional[float] = None
    level: Optional[str] = None
    new_stop: Optional[float] = None

    @field_validator("symbol")
    @classmethod
    def uppercase_symbol(cls, v: str) -> str:
        return v.strip().upper()
