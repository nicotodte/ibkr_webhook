"""Trade journal for Doji trades: plain CSV files in the data volume.

doji_signals.csv  one row per Doji alert, whatever happened to it (bought,
                  rejected for the spread, ignored outside hours, ...).
doji_trades.csv   one row per closed Doji trade with the real fills: average
                  exit, P&L in USD and in R, commission, highest and lowest
                  price seen while it was open.

No IBKR or config imports, so it can be tested on its own.
"""
import csv
import os
from datetime import datetime, timezone
from typing import Optional

SIGNAL_FIELDS = [
    "time_utc", "symbol", "tier", "trade_id", "alert_entry", "alert_stop",
    "status", "reason", "quantity",
]

TRADE_FIELDS = [
    "closed_utc", "opened_utc", "symbol", "tier", "trade_id",
    "alert_entry", "alert_stop", "fill_price", "initial_stop", "risk_per_share",
    "quantity", "planned_risk_usd", "high", "low", "max_r", "min_r",
    "reached_trail", "exit_reason", "sold_quantity", "avg_exit_price",
    "pnl_usd", "commission_usd", "net_pnl_usd", "r_multiple",
]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_row(path: str, fields: list, row: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def read_rows(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def merge_fills(known: list, fresh: list) -> bool:
    """Adds fills not seen yet (by exec_id) and fills in commissions that
    arrived late. Returns True if anything changed."""
    by_id = {f["exec_id"]: f for f in known}
    changed = False
    for f in fresh:
        old = by_id.get(f["exec_id"])
        if old is None:
            known.append(dict(f))
            by_id[f["exec_id"]] = known[-1]
            changed = True
        elif f.get("commission") and not old.get("commission"):
            old["commission"] = f["commission"]
            changed = True
    return changed


def sold_quantity(fills: list) -> float:
    return sum(f["qty"] for f in fills if f.get("side") == "SLD")


def trade_row(
    *,
    symbol: str,
    tier: str,
    trade_id: str,
    opened_utc: str,
    alert_entry: float,
    alert_stop: float,
    fill_price: float,
    initial_stop: float,
    quantity: int,
    planned_risk_usd: float,
    high: float,
    low: float,
    reached_trail: bool,
    exit_reason: str,
    fills: list,
    closed_utc: Optional[str] = None,
) -> dict:
    risk = fill_price - initial_stop
    sells = [f for f in fills if f.get("side") == "SLD"]
    sold = sum(f["qty"] for f in sells)
    avg_exit = sum(f["qty"] * f["price"] for f in sells) / sold if sold else 0.0
    pnl = (avg_exit - fill_price) * sold if sold else 0.0
    commission = sum(f.get("commission") or 0.0 for f in fills)
    net = pnl - commission
    risk_usd = risk * quantity if risk > 0 else 0.0
    return {
        "closed_utc": closed_utc or now_utc(),
        "opened_utc": opened_utc,
        "symbol": symbol,
        "tier": tier,
        "trade_id": trade_id,
        "alert_entry": alert_entry,
        "alert_stop": alert_stop,
        "fill_price": round(fill_price, 4),
        "initial_stop": round(initial_stop, 4),
        "risk_per_share": round(risk, 4),
        "quantity": quantity,
        "planned_risk_usd": planned_risk_usd,
        "high": round(high, 4),
        "low": round(low, 4),
        "max_r": round((high - fill_price) / risk, 2) if risk > 0 else "",
        "min_r": round((low - fill_price) / risk, 2) if risk > 0 and low else "",
        "reached_trail": "ja" if reached_trail else "nein",
        "exit_reason": exit_reason,
        "sold_quantity": sold,
        "avg_exit_price": round(avg_exit, 4),
        "pnl_usd": round(pnl, 2),
        "commission_usd": round(commission, 2),
        "net_pnl_usd": round(net, 2),
        "r_multiple": round(net / risk_usd, 2) if risk_usd else "",
    }


def stats(rows: list) -> dict:
    """Win rate, P&L and R per tier and in total from doji_trades.csv rows.
    A trade counts as won above +0.1 R, lost below -0.1 R, else breakeven."""

    def bucket(rs):
        n = len(rs)
        r = [float(x["r_multiple"]) for x in rs if x.get("r_multiple") not in ("", None)]
        net = sum(float(x.get("net_pnl_usd") or 0) for x in rs)
        won = sum(1 for v in r if v > 0.1)
        lost = sum(1 for v in r if v < -0.1)
        return {
            "trades": n,
            "won": won,
            "lost": lost,
            "breakeven": len(r) - won - lost,
            "win_rate_pct": round(won / (won + lost) * 100, 1) if won + lost else None,
            "net_pnl_usd": round(net, 2),
            "sum_r": round(sum(r), 2),
            "avg_r": round(sum(r) / len(r), 2) if r else None,
            "best_r": max(r) if r else None,
            "worst_r": min(r) if r else None,
        }

    out = {"total": bucket(rows)}
    for tier in ("green", "yellow", "orange"):
        out[tier] = bucket([x for x in rows if x.get("tier") == tier])
    return out
