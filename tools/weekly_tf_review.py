#!/usr/bin/env python3
"""Weekly review of the TF alerts (Fibonacci TF Entry script).

Answers, for a week of alerts: what did the bot buy, what did it skip and why,
how would the skipped ones have ended, and would other trading hours or another
spread limit have paid better (return on the money put into positions).

It reads the bot's journals (tf_signals.csv, tf_trades.csv, tf_ladder.csv) from
the server (needs the journal token) or from a folder, loads 5-minute prices
from Yahoo Finance and replays every entry alert under the bot's ladder rules:

  stop under the candle low; 0.843 -> stop to the entry price; every further
  level sells half of what is left and moves the stop; -0.236 sells the rest;
  whatever is left is sold at 15:50 New York time.

The ladder alerts come from the script itself (tf_ladder.csv), so the replay of
a signal the bot did NOT take follows the same path the script's ladder took.
Prices of those sells are the close of the 5-minute candle of the alert, stops
fill at the stop (at the open after a gap). Commissions and slippage are not
included, so treat results as a ranking of rules, not as exact profit.

Usage:
  python3 tools/weekly_tf_review.py --token TOKEN [--days 7] [--out review.md]
  python3 tools/weekly_tf_review.py --dir folder_with_the_csv_files
"""
import argparse
import csv
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")
DEFAULT_BASE = "https://srv1922478.hstgr.cloud"
BAR_SECONDS = 300
RISK_USD = 100.0
FLATTEN_HOUR, FLATTEN_MINUTE = 15, 50

# Entry windows tried (New York time, minutes after midnight, start inclusive).
WINDOWS = [
    ("jetzt: 10:00-15:00 (16:00-21:00 Berlin)", 10 * 60, 15 * 60),
    ("ab Börseneröffnung: 09:30-15:00 (15:30-21:00 Berlin)", 9 * 60 + 30, 15 * 60),
    ("bis zur Glattstellung: 10:00-15:50 (16:00-21:50 Berlin)", 10 * 60, 15 * 60 + 50),
    ("ganze Börsenzeit: 09:30-15:50 (15:30-21:50 Berlin)", 9 * 60 + 30, 15 * 60 + 50),
]
SPREAD_LIMITS = [0.05, 0.10, 0.15, 0.25, None]  # percent of the price; None = no limit


# ------------------------------------------------------------------ loading
def parse_csv(text: str) -> list:
    return list(csv.DictReader(io.StringIO(text)))


def read_source(name: str, base: str, token: str, folder: str) -> list:
    """name is e.g. "tf/signals.csv" (server) / "tf_signals.csv" (folder)."""
    if folder:
        path = os.path.join(folder, name.replace("/", "_"))
        if not os.path.exists(path):
            return []
        with open(path, newline="", encoding="utf-8") as fh:
            return parse_csv(fh.read())
    url = f"{base}/{name}?token={urllib.parse.quote(token)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return parse_csv(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # nothing recorded yet
            return []
        raise


def epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def fetch_bars(symbol: str) -> list:
    """5-minute bars of the last month: [(start_epoch, open, high, low, close)]."""
    ysym = urllib.parse.quote(symbol.replace(".", "-"))
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?interval=5m&range=1mo"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    res = data["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(res.get("timestamp") or []):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None not in (o, h, l, c):
            out.append((t, o, h, l, c))
    return out


# ---------------------------------------------------------------- simulation
def flatten_ts(entry_time: float) -> float:
    day = datetime.fromtimestamp(entry_time, NY).replace(
        hour=FLATTEN_HOUR, minute=FLATTEN_MINUTE, second=0, microsecond=0
    )
    return day.timestamp()


def simulate(entry_time: float, entry: float, stop: float, events: list, bars: list, flatten: float):
    """Replays one entry under the ladder rules. events: [{"ts", "event",
    "level", "new_stop"}]. Returns {"r", "exit", "max_r"} or None when it can't
    be judged (no prices, or the stop is not below the entry)."""
    risk = entry - stop
    if risk <= 0:
        return None
    seq = [b for b in bars if b[0] + BAR_SECONDS > entry_time and b[0] < flatten]
    if not seq:
        return None
    evs = sorted((e for e in events if e["ts"] >= entry_time), key=lambda e: e["ts"])
    ei = 0
    rem, realized, cur_stop, max_high = 1.0, 0.0, stop, entry
    exit_reason, last_close = "Glattstellung 15:50", entry
    for t, o, h, l, c in seq:
        end = t + BAR_SECONDS
        # The candle that contains the entry can't be judged for the stop.
        if t >= entry_time and l <= cur_stop:
            realized += rem * (min(cur_stop, o) - entry)
            rem = 0.0
            exit_reason = ("Stop" if cur_stop < entry else "Stop auf Einstieg" if cur_stop == entry
                           else "nachgezogener Stop")
            break
        max_high = max(max_high, h)
        while ei < len(evs) and evs[ei]["ts"] < end and rem > 0:
            ev = evs[ei]
            ei += 1
            if ev["event"] == "stop_to_breakeven":
                cur_stop = max(cur_stop, entry)
            elif ev["event"] == "level":
                realized += 0.5 * rem * (c - entry)
                rem *= 0.5
                if ev.get("new_stop"):
                    cur_stop = max(cur_stop, ev["new_stop"])
            elif ev["event"] == "exit_all":
                realized += rem * (c - entry)
                rem = 0.0
                exit_reason = "Ausstieg -0.236"
        if rem <= 0:
            break
        last_close = c
    if rem > 0:
        realized += rem * (last_close - entry)
    return {"r": realized / risk, "exit": exit_reason, "max_r": (max_high - entry) / risk}


# ------------------------------------------------------------------ analysis
def float_or_none(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def build_signals(signal_rows: list, ladder_rows: list, trade_rows: list, bars_for) -> list:
    """One dict per entry alert (first one per symbol and trade_id), with the
    replayed outcome in R and, if the bot bought it, the real result."""
    ladder: dict = {}
    for r in ladder_rows:
        key = (r["symbol"], r["trade_id"])
        ladder.setdefault(key, []).append({
            "ts": epoch(r["time_utc"]), "event": r["event"], "level": r.get("level", ""),
            "new_stop": float_or_none(r.get("new_stop")),
        })
    real = {(t["symbol"], t["trade_id"]): t for t in trade_rows}
    seen, out = set(), []
    for r in signal_rows:
        if r.get("event") != "entry":
            continue
        key = (r["symbol"], r["trade_id"])
        if key in seen:
            continue
        seen.add(key)
        entry, stop = float_or_none(r.get("alert_entry")), float_or_none(r.get("alert_stop"))
        ts = epoch(r["time_utc"])
        sim = None
        if entry and stop:
            try:
                sim = simulate(ts, entry, stop, ladder.get(key, []), bars_for(r["symbol"]), flatten_ts(ts))
            except Exception as exc:  # no prices for this symbol
                sys.stderr.write(f"keine Kurse für {r['symbol']}: {exc}\n")
        t = real.get(key)
        et = datetime.fromtimestamp(ts, NY)
        out.append({
            "time": r["time_utc"], "symbol": r["symbol"], "level": r.get("level") or "?",
            "entry": entry, "stop": stop, "status": r.get("status", ""), "reason": r.get("reason", ""),
            "spread_pct": float_or_none(r.get("spread_pct")),
            "minute": et.hour * 60 + et.minute,
            "r": sim["r"] if sim else None, "exit": sim["exit"] if sim else "",
            "max_r": sim["max_r"] if sim else None,
            "bought": r.get("status") == "submitted",
            "real_r": float_or_none(t.get("r_multiple")) if t else None,
            "ladder_events": len(ladder.get(key, [])),
        })
    return out


def summarize(sigs: list) -> dict:
    rs = [s["r"] for s in sigs if s["r"] is not None]
    pnl = sum(r * RISK_USD for r in rs)
    notional = sum(RISK_USD * s["entry"] / (s["entry"] - s["stop"]) for s in sigs if s["r"] is not None)
    won = sum(1 for r in rs if r > 0.1)
    lost = sum(1 for r in rs if r < -0.1)
    return {
        "n": len(rs), "won": won, "lost": lost,
        "win_rate": round(won / (won + lost) * 100) if won + lost else None,
        "sum_r": round(sum(rs), 2), "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
        "pnl": round(pnl), "notional": round(notional),
        "roi": round(pnl / notional * 100, 2) if notional else None,
    }


def berlin_shift_hours() -> int:
    """Hours Berlin is ahead of New York today (6, or 5 in the weeks the two
    countries change their clocks on different days)."""
    now = datetime.now(timezone.utc)
    return int((now.astimezone(BERLIN).utcoffset() - now.astimezone(NY).utcoffset()).total_seconds() // 3600)


def in_window(s: dict, start: int, end: int) -> bool:
    return start <= s["minute"] < end


def spread_ok(s: dict, limit) -> bool:
    return limit is None or s["spread_pct"] is None or s["spread_pct"] <= limit


# -------------------------------------------------------------------- report
def fmt(v, suffix=""):
    return "-" if v is None else f"{str(v).replace('.', ',')}{suffix}"


def table(head: list, rows: list) -> str:
    if not rows:
        return "_Nichts._\n"
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def sum_row(label: str, s: dict) -> list:
    return [label, s["n"], f'{s["won"]}/{s["lost"]}', fmt(s["win_rate"], " %"), fmt(s["sum_r"]),
            fmt(s["avg_r"]), fmt(s["pnl"], " $"), fmt(s["roi"], " %")]


SUM_HEAD = ["", "Signale", "gewonnen/verloren", "Trefferquote", "Summe R", "Ø R", "Gewinn bei 100 $ Risiko", "Rendite auf Positionsgröße"]


def reason_text(s: dict) -> str:
    from_app = None
    try:  # reuse the report's German wording when run from the repo
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
        import tf_report
        from_app = tf_report
    except Exception:
        pass
    if s["bought"]:
        return "gekauft"
    reason = s["reason"]
    return from_app.reason_de(reason) if from_app and reason else (reason or s["status"])


def build_report(sigs: list, trade_rows: list, since: str, until: str, versions: list,
                 title: str = "TF-Wochenauswertung") -> str:
    w = []
    w.append(f"# {title} {since} bis {until}\n")
    judged = [s for s in sigs if s["r"] is not None]
    w.append(f"{len(sigs)} Einstiegs-Signale, davon {sum(1 for s in sigs if s['bought'])} vom Bot gekauft, "
             f"{len(judged)} mit Kursen nachgespielt. Kleine Zahlen: unter etwa 30 Signalen je Zeile ist "
             f"das ein Hinweis, kein Beweis.\n")
    if versions:
        w.append("Stände des Bots in diesem Zeitraum: " + ", ".join(
            f"{v['stand']} (seit {v['first_seen_utc'][:16]}, {v.get('settings_changes', '')[:80]})" for v in versions) + "\n")

    # 1. what the bot really did
    bought = [s for s in sigs if s["bought"]]
    real = [float(t["r_multiple"]) for t in trade_rows if t.get("r_multiple") not in ("", None)]
    net = sum(float(t.get("net_pnl_usd") or 0) for t in trade_rows)
    w.append("## 1. Woran die Woche war: was der Bot wirklich getan hat\n")
    w.append(table(["Gekaufte Trades", "gewonnen/verloren", "Summe R", "Gewinn netto $"],
                   [[len(trade_rows), f"{sum(1 for r in real if r > 0.1)}/{sum(1 for r in real if r < -0.1)}",
                     fmt(round(sum(real), 2)), fmt(round(net, 2))]]))
    w.append(table(["Zeit (Berlin)", "Aktie", "Level", "Nachgespielt R", "echt R", "Ausstieg"],
                   [[datetime.fromisoformat(s["time"]).astimezone(BERLIN).strftime("%d.%m. %H:%M"), s["symbol"], s["level"],
                     fmt(None if s["r"] is None else round(s["r"], 2)), fmt(s["real_r"]), s["exit"]] for s in bought]))
    if bought:
        w.append("Die Spalte „nachgespielt“ ist die Probe, ob das Nachspielen stimmt: Sie sollte nahe an „echt“ liegen.\n")

    # 2. taken vs not taken, by reason
    w.append("## 2. Was er mitgenommen hat und was nicht (und warum)\n")
    groups: dict = {}
    for s in sigs:
        groups.setdefault(reason_text(s), []).append(s)
    rows = []
    for name, g in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        sm = summarize(g)
        rows.append([name, len(g), sm["n"], fmt(sm["sum_r"]), fmt(sm["avg_r"]), fmt(sm["pnl"], " $")])
    w.append(table(["Was passierte", "Signale", "nachgespielt", "Summe R", "Ø R", "Gewinn bei 100 $ Risiko"], rows))
    w.append("Bei „nicht gekauft“ heißt eine **positive** Summe R: Die Regel hat Gewinn gekostet. "
             "Eine **negative**: Die Regel hat Verlust erspart.\n")

    # 3. by level
    w.append("## 3. Nach Fib-Level\n")
    rows = []
    for lvl in sorted({s["level"] for s in sigs}):
        rows.append(sum_row(f"Level {lvl}", summarize([s for s in sigs if s["level"] == lvl])))
    w.append(table(SUM_HEAD, rows))

    # 4. trading hours
    w.append("## 4. Handelszeiten: wann zahlt es sich aus?\n")
    w.append("Alle Signale in der Zeit, ohne Spread-Regel (nur Signale mit Kursdaten). Rendite = Gewinn geteilt "
             "durch die Summe der Positionsgrößen.\n")
    w.append(table(SUM_HEAD, [sum_row(n, summarize([s for s in sigs if in_window(s, a, b)])) for n, a, b in WINDOWS]))
    hours = []
    shift = berlin_shift_hours()
    for hour in range(9, 16):
        a, b = max(hour * 60, 9 * 60 + 30), (hour + 1) * 60
        label = f"{hour:02d}:{a % 60:02d} NY ({hour + shift:02d}:{a % 60:02d} Berlin)"
        hours.append(sum_row(label, summarize([s for s in sigs if in_window(s, a, b)])))
    w.append("Einzelne Stunden (New Yorker Zeit):\n")
    w.append(table(SUM_HEAD, hours))

    # 5. spread
    w.append("## 5. Spread: wo liegt die beste Grenze?\n")
    known = sum(1 for s in sigs if s["spread_pct"] is not None)
    w.append(f"Nur {known} von {len(sigs)} Signalen haben einen gemessenen Spread (die anderen wurden vorher "
             f"wegen der Handelszeit aussortiert). Signale ohne Messung zählen hier als „passt“. "
             f"Handelszeit: jetzige Regel 10:00-15:00.\n")
    base = [s for s in sigs if in_window(s, 10 * 60, 15 * 60)]
    w.append(table(SUM_HEAD, [sum_row("Spread höchstens " + fmt(l, " %") if l else "ohne Spread-Grenze",
                                      summarize([s for s in base if spread_ok(s, l)])) for l in SPREAD_LIMITS]))
    rej = [s for s in sigs if s["reason"] in ("spread_too_wide", "spread_too_wide_for_risk")]
    if rej:
        w.append("Wegen Spread abgelehnte Signale einzeln:\n")
        w.append(table(["Aktie", "Level", "Spread %", "Nachgespielt R", "Ausstieg"],
                       [[s["symbol"], s["level"], fmt(s["spread_pct"]), fmt(None if s["r"] is None else round(s["r"], 2)),
                         s["exit"]] for s in rej]))

    # 6. exits
    w.append("## 6. Wie die Trades endeten (nachgespielt)\n")
    ex: dict = {}
    for s in judged:
        ex.setdefault(s["exit"], []).append(s["r"])
    w.append(table(["Ausstieg", "Anzahl", "Ø R"], [[k, len(v), fmt(round(sum(v) / len(v), 2))] for k, v in sorted(ex.items())]))
    w.append("## 7. Alle Signale einzeln\n")
    w.append(table(
        ["Zeit (Berlin)", "Aktie", "Level", "Was passierte", "Einstieg", "Stop", "höchster Kurs (R)", "nachgespielt R", "Ausstieg"],
        [[datetime.fromisoformat(s["time"]).astimezone(BERLIN).strftime("%d.%m. %H:%M"), s["symbol"], s["level"],
          reason_text(s), fmt(s["entry"]), fmt(s["stop"]), fmt(None if s["max_r"] is None else round(s["max_r"], 2)),
          fmt(None if s["r"] is None else round(s["r"], 2)), s["exit"]] for s in sorted(sigs, key=lambda x: x["time"])]))
    w.append("„Höchster Kurs (R)“: wie weit der Kurs bis zum Ausstieg über den Einstieg lief, in Vielfachen des Risikos "
             "(1 R = Einstieg minus Stop).\n")
    miss = [s for s in sigs if s["r"] is None]
    if miss:
        w.append(f"Nicht nachspielbar (keine Kurse oder Stop nicht unter Einstieg): {len(miss)} Signale.\n")
    no_ladder = sum(1 for s in judged if s["ladder_events"] == 0)
    if no_ladder:
        w.append(f"Bei {no_ladder} Signalen kam kein Leiter-Alarm an. Die laufen nachgespielt nur bis Stop oder 15:50 "
                 f"und sind deshalb eher pessimistisch.\n")
    return "\n".join(w)


# ----------------------------------------------------------------------- cli
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--token", default=os.environ.get("JOURNAL_TOKEN", ""))
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--dir", default="", help="read tf_signals.csv etc. from this folder instead of the server")
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--out", default="")
    ap.add_argument("--title", default="TF-Wochenauswertung", help="heading of the report")
    a = ap.parse_args(argv)
    if not a.dir and not a.token:
        ap.error("--token (oder JOURNAL_TOKEN) oder --dir angeben")

    signals = read_source("tf/signals.csv", a.base, a.token, a.dir)
    ladder = read_source("tf/ladder.csv", a.base, a.token, a.dir)
    trades = read_source("tf/trades.csv", a.base, a.token, a.dir)
    versions = read_source("versions.csv", a.base, a.token, a.dir)
    cutoff = datetime.now(timezone.utc) - timedelta(days=a.days)
    signals = [r for r in signals if datetime.fromisoformat(r["time_utc"]) >= cutoff]
    trades = [r for r in trades if datetime.fromisoformat(r["opened_utc"]) >= cutoff]
    since = cutoff.astimezone(BERLIN).strftime("%d.%m.%Y")
    until = datetime.now(BERLIN).strftime("%d.%m.%Y")

    cache: dict = {}

    def bars_for(symbol):
        if symbol not in cache:
            cache[symbol] = fetch_bars(symbol)
        return cache[symbol]

    sigs = build_signals(signals, ladder, trades, bars_for)
    report = build_report(sigs, trades, since, until, versions, a.title)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(report)
        print(f"Auswertung gespeichert: {a.out}")
    print(report)


if __name__ == "__main__":
    main()
