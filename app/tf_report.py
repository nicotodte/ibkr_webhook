"""Readable German page for the TF trade journal: how the trades ran, and why
alerts were not bought. Plain HTML built from the CSV rows, no IBKR imports.
"""
from datetime import datetime
from html import escape
from typing import Optional

import journal

STATUS_DE = {
    "submitted": "gekauft",
    "dry_run": "Testlauf (nicht gekauft)",
    "rejected": "nicht gekauft",
    "ignored": "nicht gekauft",
    "error": "Fehler",
    "stop_updated": "Stop nachgezogen",
    "partial_exit": "Teilverkauf",
    "closed": "komplett verkauft",
}

REASON_DE = {
    "outside_trading_hours": "außerhalb der Handelszeit (erste 30 Min. / letzte Stunde)",
    "position_already_open": "es lief schon ein Trade in diesem Wert",
    "doji_position_open": "ein Doji-Trade lief schon in diesem Wert",
    "delayed_market_data": "keine Live-Kurse (verzögert)",
    "no_market_data": "kein Kurs von IBKR bekommen",
    "no_valid_quotes": "kein gültiger Kurs (Bid/Ask)",
    "no_fx_rate": "kein EUR/USD-Kurs",
    "no_cash_balance": "Kontostand nicht lesbar",
    "insufficient_cash": "nicht genug Geld auf dem Konto",
    "order_not_filled": "Kauforder wurde nicht ausgeführt",
    "spread_too_wide": "Spread (Bid/Ask) zu groß",
    "spread_too_wide_for_risk": "Spread zu groß im Verhältnis zum Risiko",
    "price_ran_away": "Kurs war schon zu weit ueber dem Signal weggelaufen",
    "stop_not_below_entry": "Stop lag nicht unter dem Einstieg",
    "price_at_or_below_stop": "Kurs war schon am oder unter dem Stop",
    "no_open_position": "kein offener Trade",
    "trade_id_mismatch": "Alarm gehörte zu einem anderen Trade",
    "no_live_position": "Position bei IBKR nicht mehr vorhanden",
}


def reason_de(reason: str) -> str:
    if not reason:
        return ""
    if reason in REASON_DE:
        return REASON_DE[reason]
    if reason.startswith("calculated quantity < 1"):
        return "Stückzahl wäre unter 1 (Risiko pro Aktie zu groß fuer das Risikobudget)"
    return reason


def _local(iso: str) -> str:
    """UTC ISO time as Berlin time, e.g. 17.11. 15:45."""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(iso).astimezone(ZoneInfo("Europe/Berlin"))
        return dt.strftime("%d.%m. %H:%M")
    except Exception:
        return iso[:16].replace("T", " ")


def _num(x, digits: int = 2) -> str:
    try:
        return f"{float(x):.{digits}f}".replace(".", ",")
    except (TypeError, ValueError):
        return ""


def _cls(x) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return ""
    return "pos" if v > 0.1 else "neg" if v < -0.1 else ""


def _table(head: list, rows: list) -> str:
    if not rows:
        return "<p class=muted>Noch nichts.</p>"
    th = "".join(f"<th>{escape(h)}</th>" for h in head)
    body = "".join(
        "<tr>" + "".join(f"<td class='{c}'>{escape(str(v))}</td>" for v, c in r) + "</tr>"
        for r in rows
    )
    return f"<div class=scroll><table><tr>{th}</tr>{body}</table></div>"


def _stats_rows(stats: dict) -> list:
    rows = []
    for name, b in stats.items():
        label = "alle" if name == "total" else f"Level {name}"
        rows.append([
            (label, ""), (b["trades"], ""),
            (f'{b["won"]} / {b["lost"]} / {b["breakeven"]}', ""),
            (_num(b["win_rate_pct"], 0) + " %" if b["win_rate_pct"] is not None else "", ""),
            (_num(b["net_pnl_usd"]), _cls(b["net_pnl_usd"])),
            (_num(b["sum_r"]), _cls(b["sum_r"])),
            (_num(b["avg_r"]), _cls(b["avg_r"])),
        ])
    return rows


def _stand_rows(trades: list, versions: list) -> list:
    """One row per Stand of the bot: when it started, what changed, how the
    trades under it did."""
    stats = journal.stats_by_stand(trades)
    rows = []
    for v in versions:
        b = stats.get(v.get("stand"), journal.bucket([]))
        change = v.get("settings_changes", "")
        if v.get("code_changed") == "ja" and change != "erster Eintrag":
            change = ("Programm geändert; " + change).rstrip("; ")
        rows.append([
            (v.get("stand", ""), ""), (_local(v.get("first_seen_utc", "")), ""), (change, ""),
            (b["trades"], ""),
            (_num(b["win_rate_pct"], 0) + " %" if b["win_rate_pct"] is not None else "", ""),
            (_num(b["sum_r"]), _cls(b["sum_r"])), (_num(b["net_pnl_usd"]), _cls(b["net_pnl_usd"])),
        ])
    return list(reversed(rows))


def render(trades: list, signals: list, open_trades: list, versions: Optional[list] = None,
           current_stand: str = "") -> str:
    entries = [s for s in signals if s.get("event") == "entry"]
    skipped = [s for s in entries if s.get("status") != "submitted"]
    reasons: dict = {}
    for s in skipped:
        key = (STATUS_DE.get(s.get("status", ""), s.get("status", "")) + ": " + reason_de(s.get("reason", ""))).rstrip(": ")
        reasons[key] = reasons.get(key, 0) + 1

    stats_html = _table(
        ["", "Trades", "gewonnen / verloren / null", "Trefferquote", "Gewinn netto $", "Summe R", "Schnitt R"],
        _stats_rows(journal.tf_stats(trades)),
    )

    stand_html = _table(
        ["Stand", "seit", "Was sich geändert hat", "Trades", "Trefferquote", "Summe R", "Gewinn netto $"],
        _stand_rows(trades, versions or []),
    )

    open_html = _table(
        ["Seit", "Aktie", "Level", "Stück", "Einstieg", "Stop", "Schon erreicht"],
        [[(_local(t.get("opened_at", "")), ""), (t["symbol"], ""), (t.get("entry_level") or "?", ""),
          (t["quantity"], ""), (_num(t["entry"], 4), ""), (_num(t["stop"], 4), ""),
          (", ".join(t.get("levels_hit") or []), "")] for t in open_trades],
    )

    trade_rows = []
    for t in reversed(trades):
        trade_rows.append([
            (_local(t.get("opened_utc", "")), ""), (t.get("symbol", ""), ""), (t.get("entry_level") or "?", ""),
            (t.get("quantity", ""), ""), (_num(t.get("fill_price"), 4), ""), (_num(t.get("alert_stop"), 4), ""),
            (_num(t.get("entry_slippage_pct"), 2) + " %" if t.get("entry_slippage_pct") not in ("", None) else "", ""),
            (_num(t.get("max_r")), ""), (_num(t.get("min_r")), ""),
            (t.get("levels_hit", ""), ""), (t.get("exit_reason", ""), ""),
            (_num(t.get("net_pnl_usd")), _cls(t.get("r_multiple"))),
            (_num(t.get("r_multiple")), _cls(t.get("r_multiple"))),
            (t.get("result", ""), _cls(t.get("r_multiple"))),
            (t.get("stand", "") or "vorher", ""),
        ])
    trades_html = _table(
        ["Gekauft", "Aktie", "Level", "Stück", "Einstieg (echt)", "Stop", "Abweichung zum Signal",
         "höchster Kurs (R)", "tiefster Kurs (R)", "erreichte Level", "Ausstieg", "Gewinn netto $", "R", "Ergebnis", "Stand"],
        trade_rows,
    )

    why_rows = [[(k, ""), (v, "")] for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])]
    why_html = _table(["Warum nicht gekauft", "Anzahl"], why_rows)

    sig_rows = []
    for s in reversed(entries):
        bought = s.get("status") == "submitted"
        sig_rows.append([
            (_local(s.get("time_utc", "")), ""), (s.get("symbol", ""), ""), (s.get("level") or "?", ""),
            (_num(s.get("alert_entry"), 4), ""), (_num(s.get("alert_stop"), 4), ""),
            (("gekauft: " + str(s.get("quantity", "")) + " Stück") if bought
             else STATUS_DE.get(s.get("status", ""), s.get("status", "")), "pos" if bought else ""),
            (reason_de(s.get("reason", "")) or s.get("detail", ""), ""),
            ("Spread " + _num(s.get("spread_pct"), 3) + " %" if s.get("spread_pct") not in ("", None) else "", ""),
        ])
    sig_html = _table(["Zeit", "Aktie", "Level", "Signal-Einstieg", "Signal-Stop", "Was passierte", "Grund", "Kurs"], sig_rows)

    return f"""<!doctype html>
<html lang=de><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>TF-Trade-Protokoll</title>
<style>
:root{{--bg:#fff;--fg:#1b1f24;--mut:#6a737d;--line:#d8dee4;--pos:#1a7f37;--neg:#cf222e}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0d1117;--fg:#e6edf3;--mut:#8b949e;--line:#30363d;--pos:#3fb950;--neg:#f85149}}}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}}
h1{{font-size:20px}}h2{{font-size:16px;margin-top:28px}}
.scroll{{overflow-x:auto}}table{{border-collapse:collapse;white-space:nowrap}}
th,td{{padding:4px 10px;border-bottom:1px solid var(--line);text-align:left}}th{{color:var(--mut);font-weight:600}}
.pos{{color:var(--pos)}}.neg{{color:var(--neg)}}.muted{{color:var(--mut)}}
</style></head><body>
<h1>TF-Trade-Protokoll</h1>
<p class=muted>Alle Zeiten Berliner Zeit. R = Vielfaches des Risikos (1 R = Einstieg minus Stop, mal Stückzahl).
Ergebnis: gewonnen ab +0,1 R, verloren ab -0,1 R, sonst null. Gewinn ist nach Kommission.</p>
<h2>Gesamtbild</h2>{stats_html}
<h2>Versionen des Bots</h2>
<p class=muted>Jede Änderung am Programm oder an den Einstellungen bekommt einen neuen Stand. Jeder Trade ist
dem Stand zugeordnet, unter dem er gekauft wurde. So siehst du, ob es nach einer Änderung besser oder schlechter lief.
Aktueller Stand: {escape(current_stand) or "unbekannt"}</p>
{stand_html}
<h2>Jetzt offen</h2>{open_html}
<h2>Abgeschlossene Trades</h2>{trades_html}
<h2>Warum Signale nicht gekauft wurden</h2>{why_html}
<h2>Alle Einstiegs-Signale</h2>{sig_html}
</body></html>"""
