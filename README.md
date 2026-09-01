# TradingView → IBKR Webhook

Bot für die "Fibonacci TF Entry 0.975"-Strategie: nimmt Entry- und
Exit-Leiter-Alerts vom TradingView-Indikator entgegen und verwaltet die
komplette Position bei Interactive Brokers automatisch — Einstieg,
Breakeven-Stop, gestaffelte Teilverkäufe und nachgezogener Stop.

## Wie Pine Script und Bot zusammenspielen

**Pine Script kennt eure echte Stückzahl nicht** — nur IBKR weiß, wie viele
Aktien tatsächlich gefüllt wurden. Deshalb ist die Arbeit klar aufgeteilt:

- **[pine/fib_tf_entry_bot.pine](pine/fib_tf_entry_bot.pine)** berechnet
  die Fibonacci-Preise (das kann nur Pine) und feuert bei jeder relevanten
  Level-Berührung einen JSON-Webhook-Alert: welches Level, welcher
  Ziel-Stop-Preis.
- **Der Python-Bot** hält den State (offene Position pro Symbol,
  verbleibende Stückzahl), fragt die tatsächliche Stückzahl live bei IBKR
  ab und entscheidet, was konkret zu tun ist.

### Events, die das Skript feuert

| Event | Wann | Bot-Aktion |
|---|---|---|
| `entry` | 0.975-Level bestätigt (bestehende Logik) | Handelszeiten/Spread/Cash prüfen, Stückzahl berechnen, Market-Buy + Stop-Order platzieren |
| `stop_to_breakeven` | 0.843 berührt | Stop auf den **tatsächlichen** IBKR-Einstandspreis verschieben (nicht den theoretischen Pine-Wert) |
| `level` | 0.786 / 0.707 / 0.618 / 0.5 / 0.382 / 0.236 / 0 berührt | 50% der aktuellen Restposition verkaufen (Market), Stop auf den mitgelieferten Preis (= 2 Level zurück) verschieben |
| `exit_all` | -0.236 berührt | komplette Restposition verkaufen, Stop-Order canceln |

Die Stop-Nachführung "immer 2 Level hinter dem zuletzt berührten Level"
ist im Skript fest verdrahtet: 786→Stop 0.9, 707→Stop 0.843, 618→Stop
0.786, 0.5→Stop 0.707, 0.382→Stop 0.618, 0.236→Stop 0.5, 0→Stop 0.382.

Jedes Event trägt eine `trade_id` (die Bar-Position des Fib-Ankers) — der
Bot ignoriert Level-Events, deren `trade_id` nicht zur aktuell offenen
Position passt (Schutz gegen veraltete/verwechselte Signale).

## Architektur (3 Docker-Container)

- **ib-gateway** — headless IB Gateway, Login/2FA-Automatisierung via IBC
  (Image `ghcr.io/gnzsnz/ib-gateway`). Nur im internen Docker-Netz erreichbar.
- **webhook** — FastAPI-Service (`app/`), verwaltet Positionen und Orders.
  Persistiert offene Trades in `/data/positions.json` (Docker-Volume),
  überlebt also einen Container-Neustart.
- **caddy** — Reverse Proxy mit automatischem Let's-Encrypt-HTTPS.

## Voraussetzungen

- Hostinger VPS (Ubuntu 22.04/24.04), Docker + Docker Compose Plugin
- Eine Domain/Subdomain, deren A-Record auf die VPS-IP zeigt
- IBKR Paper-Trading-Zugang (separate Login-Daten vom Live-Konto)
- **Echtzeit-Marktdaten-Abo** für die gehandelten Symbole (Aktien und
  Futures) bei IBKR — der Spread-Check und die EUR/USD-Kursabfrage brauchen
  Live-Kurse. Ohne Abo, oder wenn IBKR für ein Symbol nur Delayed-Daten
  liefert, wird der Entry abgelehnt — das ist ein bewusst *sicherer*
  Fehlerzustand: keine Echtzeitdaten → keine Order, statt eine Order auf
  Basis veralteter Kurse abzusetzen

Docker installieren, falls noch nicht vorhanden:

```bash
curl -fsSL https://get.docker.com | sh
```

## Setup

```bash
cp .env.example .env
```

`.env` bearbeiten:

- `DOMAIN` — deine Subdomain
- `WEBHOOK_SECRET` — mit `openssl rand -hex 32` generieren
- `IB_USERID` / `IB_PASSWORD` — deine IBKR **Paper**-Zugangsdaten
- `SIZING_MODE=fixed_shares`, `FIXED_SHARES_QTY=1` für den ersten Test
  (später auf `fixed_risk` umstellen, siehe unten)
- `DRY_RUN=true` für den ersten Test lassen

Stack starten:

```bash
docker compose up -d --build
```

IB-Gateway-Login prüfen (kann beim ersten Mal etwas dauern):

```bash
docker compose logs -f ib-gateway
```

Webhook-Status:

```bash
curl https://DEINE_DOMAIN/health
```

Sollte `{"status":"ok","ib_connected":true}` liefern.

## Pine Script in TradingView einrichten

1. [pine/fib_tf_entry_bot.pine](pine/fib_tf_entry_bot.pine) in den
   TradingView Pine-Editor kopieren, als Indikator zum Chart hinzufügen.
2. In den Indikator-Einstellungen unter **Webhook** das Feld
   `Webhook Secret` auf denselben Wert wie `WEBHOOK_SECRET` in `.env` setzen.
3. Einen einzigen Alert auf dieses Skript anlegen:
   - **Condition:** das Indikator-Skript auswählen
   - **Trigger:** "Any alert() function call"
   - **Webhook URL:** `https://DEINE_DOMAIN/webhook`
   - Nachricht/Message-Feld kann leer bleiben — der Text kommt dynamisch
     aus den `alert()`-Aufrufen im Skript.

Alle vier Event-Typen (entry, stop_to_breakeven, level, exit_all) laufen
über diesen einen Alert.

## Testen

Mit `DRY_RUN=true` kannst du einen Entry manuell simulieren, ohne dass
etwas bei IBKR ausgelöst wird:

```bash
curl -X POST https://DEINE_DOMAIN/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "secret": "DEIN_WEBHOOK_SECRET",
    "event": "entry",
    "symbol": "AAPL",
    "trade_id": "12345",
    "entry": 220.00,
    "stop": 215.00
  }'
```

Antwort zeigt die berechnete Stückzahl, den geprüften Spread und den
FX-Kurs, ohne eine echte Order zu senden. Wenn das passt: `DRY_RUN=false`
setzen, `docker compose up -d` erneut ausführen, und im TradingView-Chart
live mitverfolgen bzw. mit `docker compose logs -f webhook` prüfen, wie
Entry, Breakeven-Stop und Teilverkäufe nacheinander reinkommen.

## Von "1 Aktie" auf risikobasierte Größe umstellen

Phase 1 (`SIZING_MODE=fixed_shares`, `FIXED_SHARES_QTY=1`) prüft nur, ob
die komplette Kette (Entry → Breakeven → Teilverkäufe → Exit) sauber
durchläuft. Sobald das zuverlässig funktioniert:

```
SIZING_MODE=fixed_risk
FIXED_RISK_EUR=100
MAX_POSITION_EUR=50000
```

Dann: Stückzahl = (100€ × aktueller EUR/USD-Kurs) ÷ |Entry − Stop| in USD,
gedeckelt durch die 50.000€-Positionsgrenze.

## Live schalten

Erst wenn Paper-Trading zuverlässig läuft:

1. In `.env`: `IB_USERID`/`IB_PASSWORD` auf die **Live**-Zugangsdaten setzen
2. `TRADING_MODE=live`, `IB_PORT=4003`
3. `docker compose up -d`

## Bot-Regeln — Umsetzung & bewusste Annahmen

- **Handelszeiten:** 16–21 Uhr CEST (Sommer) und 15–20 Uhr CET (Winter)
  sind beide identisch 14:00–19:00 UTC — deshalb fix in UTC hinterlegt,
  keine DST-Logik nötig. Das Zeitfenster gilt **nur für neue Entries**;
  eine bereits offene Position wird auch außerhalb davon weiter verwaltet
  (Stop/Teilverkäufe), damit nichts unbeaufsichtigt offen bleibt.
- **Spread-Filter:** Entry wird abgelehnt, wenn `(Ask-Bid)/Mid > 0.05%`
  (`MAX_SPREAD_PCT` in `.env`).
- **Cash-Limit:** vor jedem Entry wird `TotalCashValue` (Basiswährung EUR)
  live bei IBKR abgefragt; reicht das nicht für die neue Position, wird
  sie abgelehnt. Mehrere Symbole können parallel offen sein, solange
  jeweils genug Cash da ist — es gibt keine Margin-Nutzung.
- **Teilverkäufe als Market-Order:** wie besprochen — da der Spread schon
  beim Entry geprüft wird, ist eine Limit-Order mit Ausführungsrisiko
  hier nicht nötig.
- **Kleine Stückzahlen:** bei `FIXED_SHARES_QTY=1` (oder sehr kleiner
  risikobasierter Größe) läuft die Halbierungs-Leiter schnell auf 0 —
  sobald die Restposition ≤1 Stück ist, verkauft ein Level-Event den
  kompletten Rest statt nochmal zu halbieren.
- **EUR/USD:** live über eine Forex-Kursabfrage bei IBKR (`EURUSD`), nicht
  über IBKR's internen `ExchangeRate`-Account-Tag, um Verwechslung der
  Umrechnungsrichtung auszuschließen.
- **Partial Fills (Teilausführungen):** Nach dem Market-Buy wartet der Bot
  auf die tatsächliche Ausführung (`IBKRClient.wait_for_fill`) und liest die
  wirklich gefüllte Stückzahl aus dem Order-Status aus. Bekommt er z. B. nur
  900 statt der berechneten 1000 Aktien, wird die Stop-Order für genau diese
  900 Stück gesetzt und auch der gespeicherte Position-State (`quantity`)
  auf 900 korrigiert — nicht auf die ursprünglich angeforderte Menge. Das
  Gesamtrisiko in EUR sinkt dadurch automatisch proportional (Risiko pro
  Aktie bleibt gleich, nur weniger Aktien). Die Gewinnmitnahme-Leiter
  (`level`-Events) fragt ohnehin bei jedem Schritt die live-Position bei
  IBKR ab statt den gespeicherten State zu benutzen, rechnet also immer
  automatisch auf die tatsächlich vorhandene Stückzahl um.
- **Nur Echtzeitdaten bei IBKR:** Vor jedem Entry prüft der Bot den
  `marketDataType` des IBKR-Tickers (1 = Live). Liefert IBKR für ein Symbol
  nur Delayed- oder Frozen-Daten (kein Echtzeit-Abo für dieses Symbol/diese
  Aktien-/Futures-Kontraktklasse abgeschlossen), wird der Entry mit
  `"reason": "delayed_market_data"` abgelehnt statt auf veralteten Kursen zu
  handeln. Betrifft sowohl Aktien als auch Futures.
- **Zeitverzug bei TradingView:** Der Bot bekommt aus dem Webhook-Payload
  keine Information darüber, ob TradingView das Chart mit 15-Minuten-Verzug
  anzeigt (das ist ein reines TradingView-Datenabo-Thema, das im Pine-Skript
  technisch nicht auslesbar ist). Der obige Echtzeit-Check auf IBKR-Seite
  fängt zuverlässig ab, dass der Bot selbst nie auf Basis veralteter Kurse
  eine Order platziert — für TradingView-seitig verzögerte Symbole muss die
  Watchlist/der Alert manuell auf Symbole mit TradingView-Echtzeit-Abo
  beschränkt werden.

## Dateien

- `pine/fib_tf_entry_bot.pine` — dein Original-Indikator + Webhook-Alerts
  für Entry und die komplette Exit-Leiter
- `app/main.py` — Webhook-Endpunkt, Auth, Event-Routing
- `app/handlers.py` — Logik pro Event (entry/breakeven/level/exit_all)
- `app/sizing.py` — Stückzahl-Berechnung (fixed_shares / fixed_risk)
- `app/state.py` — persistenter Positions-State pro Symbol
- `app/trading_hours.py` — UTC-Handelsfenster
- `app/ibkr_client.py` — IBKR-Verbindung, Kurse, Orders (ib_async)
- `app/models.py` — erwartetes Alert-JSON-Format
- `app/config.py` — alle Einstellungen (aus `.env`)
