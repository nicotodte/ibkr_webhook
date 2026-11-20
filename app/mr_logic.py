"""Index mean reversion ("MR"), signal logging only -- the rules and the
bookkeeping, no IBKR or config imports, so it can be tested on its own.

Rule (Connors RSI(2), tested 2000-2026 on SPY/QQQ/IWM/DIA, +35 bp per trade
after costs, 71 % winners, losing years 2018/2020/2022):
  buy   when RSI(2) < 10 and the close is above its 200-day average,
  sell  at the first later close above the 5-day average.
The bot decides a few minutes before the close with the live price standing
in for today's close, and books the shadow trade at the real ask (buy) or bid
(sell) with the commission IBKR quotes for that order. No order is sent.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from typing import Optional

RSI_LEN = 2


def rsi(closes: list, n: int = RSI_LEN) -> Optional[float]:
    """Wilder RSI (exponential, alpha = 1/n), the same as the backtest's
    pandas ewm(alpha=1/n, adjust=False) on up and down moves."""
    if len(closes) < n + 1:
        return None
    a = 1.0 / n
    au = ad = None
    for prev, cur in zip(closes, closes[1:]):
        d = cur - prev
        up, dn = max(d, 0.0), max(-d, 0.0)
        if au is None:
            au, ad = up, dn
        else:
            au = au * (1 - a) + up * a
            ad = ad * (1 - a) + dn * a
    if ad == 0:
        return 100.0 if au > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + au / ad)


def sma(closes: list, n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


@dataclass
class Indicators:
    close: float
    rsi2: Optional[float]
    sma5: Optional[float]
    sma200: Optional[float]

    @property
    def entry_signal(self) -> bool:
        return (self.rsi2 is not None and self.sma200 is not None
                and self.rsi2 < 10 and self.close > self.sma200)

    @property
    def exit_signal(self) -> bool:
        return self.sma5 is not None and self.close > self.sma5


def indicators(closes: list) -> Indicators:
    """closes: daily closes, oldest first, the last one being today's live
    price."""
    return Indicators(close=closes[-1], rsi2=rsi(closes), sma5=sma(closes, 5), sma200=sma(closes, 200))


def with_today(bars: list, today: date, live_price: float) -> list:
    """bars: (date, close) daily bars, oldest first. IBKR includes today's
    unfinished bar during the session; its close is replaced by the live
    price, or the live price is appended when the bar is not there yet."""
    closes = [c for d, c in bars if d != today]
    if bars and bars[-1][0] > today:
        raise ValueError("daily bars run past today")
    return closes + [live_price]


def session_close(liquid_hours: str, day: date) -> Optional[time]:
    """Today's regular close from IBKR's liquidHours, e.g.
    '20261127:0930-20261127:1300;20261128:CLOSED'. None when the market is
    closed that day or the string has no entry for it (format with or without
    the date on the end part, as IBKR has used both)."""
    key = day.strftime("%Y%m%d")
    for part in (liquid_hours or "").split(";"):
        part = part.strip()
        if not part.startswith(key):
            continue
        if "CLOSED" in part:
            return None
        try:
            end = part.split("-")[1]
            hhmm = end.split(":")[-1]
            return time(int(hhmm[:2]), int(hhmm[2:4]))
        except (IndexError, ValueError):
            return None
    return None


def fallback_commission(qty: int, per_share: float = 0.005, minimum: float = 1.0, max_pct: float = 1.0,
                        price: float = 0.0) -> float:
    """IBKR fixed pricing for US shares and share CFDs: 0.005 USD per share,
    at least 1 USD, at most 1 % of the trade value. Used only when IBKR's own
    what-if quote gives no commission."""
    c = max(minimum, per_share * qty)
    if price > 0:
        c = min(c, max_pct / 100.0 * qty * price)
    return round(c, 4)


def valid_number(x) -> bool:
    """IBKR fills unknown doubles with 1.797e308 (UNSET_DOUBLE)."""
    try:
        return x is not None and math.isfinite(float(x)) and abs(float(x)) < 1e12
    except (TypeError, ValueError):
        return False


@dataclass
class ShadowPosition:
    symbol: str
    opened_utc: str
    entry_day: str
    quantity: int
    entry_ask: float
    entry_bid: float
    entry_last: float
    entry_commission: float
    commission_source: str
    rsi2: float
    sma200: float
    notional_usd: float
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ShadowPosition":
        return ShadowPosition(**d)


def nights_between(entry_day: str, exit_day: str) -> int:
    """Calendar nights held: IBKR charges CFD financing for every night,
    weekends included."""
    return max(0, (date.fromisoformat(exit_day) - date.fromisoformat(entry_day)).days)


def close_trade(pos: ShadowPosition, *, exit_day: str, closed_utc: str, exit_bid: float, exit_ask: float,
                exit_last: float, exit_commission: float, exit_commission_source: str,
                financing_pct_year: float, sma5: float) -> dict:
    """The finished shadow trade, in the columns of TRADE_FIELDS."""
    nights = nights_between(pos.entry_day, exit_day)
    gross = (exit_bid - pos.entry_ask) * pos.quantity
    financing = pos.notional_usd * financing_pct_year / 100.0 / 360.0 * nights
    commission = pos.entry_commission + exit_commission
    net = gross - commission - financing
    # What the backtest would have booked: last price in and out, no costs.
    close_based = (exit_last - pos.entry_last) * pos.quantity
    return {
        "closed_utc": closed_utc,
        "opened_utc": pos.opened_utc,
        "symbol": pos.symbol,
        "entry_day": pos.entry_day,
        "exit_day": exit_day,
        "nights": nights,
        "quantity": pos.quantity,
        "notional_usd": round(pos.notional_usd, 2),
        "entry_ask": pos.entry_ask,
        "entry_bid": pos.entry_bid,
        "entry_last": pos.entry_last,
        "exit_bid": exit_bid,
        "exit_ask": exit_ask,
        "exit_last": exit_last,
        "rsi2_at_entry": round(pos.rsi2, 2),
        "sma5_at_exit": round(sma5, 4),
        "gross_pnl_usd": round(gross, 2),
        "commission_usd": round(commission, 4),
        "commission_source": f"{pos.commission_source}/{exit_commission_source}",
        "financing_usd": round(financing, 2),
        "net_pnl_usd": round(net, 2),
        "net_pct": round(net / pos.notional_usd * 100.0, 4) if pos.notional_usd else "",
        "close_based_pnl_usd": round(close_based, 2),
        "spread_cost_usd": round(close_based - gross, 2),
    }


SIGNAL_FIELDS = [
    "time_utc", "day", "symbol", "action", "reason", "live_price", "bid", "ask", "rsi2", "sma5", "sma200",
    "bars", "quantity", "commission_usd", "commission_source", "detail", "stand",
]

TRADE_FIELDS = [
    "closed_utc", "opened_utc", "symbol", "entry_day", "exit_day", "nights", "quantity", "notional_usd",
    "entry_ask", "entry_bid", "entry_last", "exit_bid", "exit_ask", "exit_last", "rsi2_at_entry",
    "sma5_at_exit", "gross_pnl_usd", "commission_usd", "commission_source", "financing_usd", "net_pnl_usd",
    "net_pct", "close_based_pnl_usd", "spread_cost_usd", "stand",
]


def summary(trades: list, open_positions: list) -> dict:
    """Totals for /mr/stats and the monthly review: what was invested, what
    came out, after IBKR fees, spread and financing."""
    def f(row, k):
        try:
            return float(row.get(k) or 0)
        except (TypeError, ValueError):
            return 0.0
    n = len(trades)
    net = sum(f(t, "net_pnl_usd") for t in trades)
    wins = sum(1 for t in trades if f(t, "net_pnl_usd") > 0)
    return {
        "closed_trades": n,
        "winners": wins,
        "win_rate_pct": round(100.0 * wins / n, 1) if n else None,
        "invested_usd_total": round(sum(f(t, "notional_usd") for t in trades), 2),
        "gross_pnl_usd": round(sum(f(t, "gross_pnl_usd") for t in trades), 2),
        "commission_usd": round(sum(f(t, "commission_usd") for t in trades), 2),
        "financing_usd": round(sum(f(t, "financing_usd") for t in trades), 2),
        "spread_cost_usd": round(sum(f(t, "spread_cost_usd") for t in trades), 2),
        "net_pnl_usd": round(net, 2),
        "close_based_pnl_usd": round(sum(f(t, "close_based_pnl_usd") for t in trades), 2),
        "open_positions": open_positions,
        "open_notional_usd": round(sum(float(p.get("notional_usd") or 0) for p in open_positions), 2),
    }
