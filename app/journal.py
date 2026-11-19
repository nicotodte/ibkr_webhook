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
import shutil
from datetime import datetime, timezone
from typing import Optional

SIGNAL_FIELDS = [
    "time_utc", "symbol", "tier", "trade_id", "alert_entry", "alert_stop",
    "status", "reason", "quantity", "bid", "ask", "spread_pct",
    "spread_pct_of_risk", "slippage_pct_of_risk", "detail", "breadth", "risk_usd", "level", "info", "stand",
]

TRADE_FIELDS = [
    "closed_utc", "opened_utc", "symbol", "tier", "trade_id",
    "alert_entry", "alert_stop", "fill_price", "initial_stop", "risk_per_share",
    "quantity", "planned_risk_usd", "high", "low", "max_r", "min_r",
    "reached_trail", "exit_reason", "sold_quantity", "avg_exit_price",
    "pnl_usd", "commission_usd", "net_pnl_usd", "r_multiple", "stand",
]


# The Stand (see stand.py) written into every row that has a "stand" column.
STAND = ""


def set_stand(value: str) -> None:
    global STAND
    STAND = value


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_row(path: str, fields: list, row: dict) -> None:
    if "stand" in fields and not row.get("stand"):
        row = {**row, "stand": STAND}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, newline="", encoding="utf-8") as fh:
            header = next(csv.reader(fh), [])
        if header != fields:
            _migrate(path, fields)
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def _migrate(path: str, fields: list) -> None:
    """Columns changed with an update: rewrite the file under the new header,
    so the history stays in the one table that stats, reports and downloads
    read. Old rows get empty new columns (an empty Stand counts as "vorher").
    The file as it was is kept next to it as .bak, which also holds any
    column that was dropped. Not .old: read_all would count it twice."""
    old_rows = read_rows(path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    shutil.copy2(path, f"{path}.{stamp}.bak")
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in old_rows:
            w.writerow({k: v for k, v in r.items() if k is not None})
    os.replace(tmp, path)


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


def bucket(rs: list) -> dict:
    """Win rate, P&L and R of closed-trade rows. A trade counts as won above
    +0.1 R, lost below -0.1 R, else breakeven."""
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


def stats(rows: list) -> dict:
    """Win rate, P&L and R per tier and in total from doji_trades.csv rows."""
    out = {"total": bucket(rows)}
    for tier in ("green", "yellow", "orange"):
        out[tier] = bucket([x for x in rows if x.get("tier") == tier])
    return out


def _secs(iso: str) -> Optional[int]:
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except Exception:
        return None


def _doji_bar_secs(trade_id: str) -> Optional[int]:
    """trade_id is TradingView's timenow (ms) when the alert fired, i.e. a
    few seconds into the minute after the Doji candle. Returns the start of
    the Doji candle itself."""
    try:
        ms = int(float(trade_id))
    except Exception:
        return None
    return (ms // 60000 - 1) * 60


TIER_CODE = {"green": "g", "yellow": "y", "orange": "o"}


def tv_export(trade_rows: list, signal_rows: list, open_trades: list, max_chars: int = 4000) -> str:
    """Compact text for the "Doji Bot Trades" TradingView indicator, newest
    last, cut from the oldest end to max_chars. Records, separated by ";":

      T,SYM,tier,doji_s,fill,stop,exit_s,exit,r,net_usd,max_r   closed trade
      O,SYM,tier,doji_s,fill,stop                               open trade
      S,SYM,tier,doji_s,status:reason                           not bought
    """
    recs = []  # (sort time, text)
    bought = set()
    for r in trade_rows:
        t0 = _doji_bar_secs(r.get("trade_id", ""))
        t1 = _secs(r.get("closed_utc", ""))
        if t0 is None or t1 is None:
            continue
        bought.add(r.get("trade_id"))
        recs.append((t0, ",".join([
            "T", r.get("symbol", ""), TIER_CODE.get(r.get("tier", ""), "o"), str(t0),
            r.get("fill_price", ""), r.get("initial_stop", ""), str(t1), r.get("avg_exit_price", ""),
            str(r.get("r_multiple", "") or 0), r.get("net_pnl_usd", ""), str(r.get("max_r", "") or 0),
        ])))
    for t in open_trades:
        t0 = _doji_bar_secs(t["trade_id"])
        if t0 is None:
            continue
        bought.add(t["trade_id"])
        recs.append((t0, ",".join([
            "O", t["symbol"], TIER_CODE.get(t["tier"], "o"), str(t0),
            f'{t["entry"]:.4f}', f'{t["stop"]:.4f}',
        ])))
    for s in signal_rows:
        if s.get("status") == "submitted" or s.get("trade_id") in bought:
            continue
        t0 = _doji_bar_secs(s.get("trade_id", ""))
        if t0 is None:
            continue
        why = s.get("status", "") + (":" + s["reason"] if s.get("reason") else "")
        recs.append((t0, ",".join(["S", s.get("symbol", ""), TIER_CODE.get(s.get("tier", ""), "o"), str(t0), why])))
    recs.sort()
    out = []
    size = 0
    for _, text in reversed(recs):
        if size + len(text) + 1 > max_chars:
            break
        out.append(text)
        size += len(text) + 1
    return ";".join(reversed(out))


def read_all(path: str) -> list:
    """The journal file plus any copies moved aside after a column change."""
    import glob
    rows = []
    for old in sorted(glob.glob(path + ".*.old")):
        rows += read_rows(old)
    return rows + read_rows(path)


# ---------------------------------------------------------------- TF trades
# tf_signals.csv  one row per TF alert that reached the bot: the entry alerts
#                 (bought or why not) and the ladder alerts of open trades.
# tf_trades.csv   one row per closed TF trade with the real fills.

TF_SIGNAL_FIELDS = [
    "time_utc", "symbol", "event", "trade_id", "level", "alert_entry", "alert_stop",
    "status", "reason", "quantity", "fill_price", "bid", "ask", "spread_pct",
    "spread_pct_of_risk", "slippage_pct_of_risk", "detail", "info", "stand",
]

TF_TRADE_FIELDS = [
    "closed_utc", "opened_utc", "symbol", "trade_id", "entry_level",
    "alert_entry", "alert_stop", "fill_price", "entry_slippage_pct",
    "risk_per_share", "quantity", "risk_usd", "high", "low", "max_r", "min_r",
    "levels_hit", "exit_reason", "sold_quantity", "avg_exit_price",
    "pnl_usd", "commission_usd", "net_pnl_usd", "r_multiple", "result", "info", "stand",
]

# Texts the bot ignores (Achtung / HOLD / K=0 of the Longterm script), kept for later review.
OTHER_ALERT_FIELDS = ["time_utc", "text", "stand"]


def result_label(r_multiple) -> str:
    try:
        r = float(r_multiple)
    except (TypeError, ValueError):
        return ""
    return "gewonnen" if r > 0.1 else "verloren" if r < -0.1 else "breakeven"


def entry_level_from(level: Optional[str], info: Optional[str]) -> str:
    """The fib level an entry came from: the alert's "level" if it has one,
    else the label text the Pine script sends as info ("TF 0.95 R: ...")."""
    if level:
        return str(level)
    import re
    # "TF 0.95 R: ...", "TF ALT: 0.975 R: ..." and "TF ALT2: 0.975 R: ..." are
    # the labels of the script's entries; the ALT variants keep their own name
    # so the review can tell them apart from the plain 0.975.
    m = re.match(r"\s*TF\s+(?:(ALT\d*):\s*)?(\d+(?:\.\d+)?)", info or "")
    if not m:
        return ""
    return m.group(2) + (" " + m.group(1) if m.group(1) else "")


def tf_trade_row(
    *,
    symbol: str,
    trade_id: str,
    opened_utc: str,
    entry_level: str,
    alert_entry: float,
    alert_stop: float,
    fill_price: float,
    quantity: int,
    high: Optional[float],
    low: Optional[float],
    levels_hit: list,
    exit_reason: str,
    fills: list,
    info: str = "",
    closed_utc: Optional[str] = None,
    stand: str = "",
) -> dict:
    risk = fill_price - alert_stop
    sells = [f for f in fills if f.get("side") == "SLD"]
    sold = sum(f["qty"] for f in sells)
    avg_exit = sum(f["qty"] * f["price"] for f in sells) / sold if sold else 0.0
    pnl = (avg_exit - fill_price) * sold if sold else 0.0
    commission = sum(f.get("commission") or 0.0 for f in fills)
    net = pnl - commission
    risk_usd = risk * quantity if risk > 0 else 0.0
    r_mult = round(net / risk_usd, 2) if risk_usd else ""
    return {
        "closed_utc": closed_utc or now_utc(),
        "opened_utc": opened_utc,
        "symbol": symbol,
        "trade_id": trade_id,
        "entry_level": entry_level,
        "alert_entry": alert_entry,
        "alert_stop": alert_stop,
        "fill_price": round(fill_price, 4),
        "entry_slippage_pct": round((fill_price - alert_entry) / alert_entry * 100, 3) if alert_entry else "",
        "risk_per_share": round(risk, 4),
        "quantity": quantity,
        "risk_usd": round(risk_usd, 2),
        "high": round(high, 4) if high else "",
        "low": round(low, 4) if low else "",
        "max_r": round((high - fill_price) / risk, 2) if high and risk > 0 else "",
        "min_r": round((low - fill_price) / risk, 2) if low and risk > 0 else "",
        "levels_hit": " ".join(levels_hit),
        "exit_reason": exit_reason,
        "sold_quantity": sold,
        "avg_exit_price": round(avg_exit, 4),
        "pnl_usd": round(pnl, 2),
        "commission_usd": round(commission, 2),
        "net_pnl_usd": round(net, 2),
        "r_multiple": r_mult,
        "result": result_label(r_mult) if sold else "",
        "info": info,
        "stand": stand,
    }


def tf_stats(rows: list) -> dict:
    """Totals and one bucket per entry level (0.9 / 0.95 / 0.975)."""
    out = {"total": bucket(rows)}
    for level in sorted({x.get("entry_level") or "?" for x in rows}):
        out[level] = bucket([x for x in rows if (x.get("entry_level") or "?") == level])
    return out


def stats_by_stand(rows: list) -> dict:
    """One bucket per Stand of the bot, in the order the Stände first appear."""
    out: dict = {}
    for r in rows:
        out.setdefault(r.get("stand") or "vorher", []).append(r)
    return {k: bucket(v) for k, v in out.items()}
