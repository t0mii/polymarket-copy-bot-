# Poly CopyBot by Super Sauna Club

Ein automatischer Copy-Trading Bot fuer [Polymarket](https://polymarket.com). Folgt den besten Tradern und kopiert ihre Wetten mit echtem Geld.

---

## Was ist Polymarket?

Polymarket ist eine Wettboerse im Internet. Du kannst auf quasi alles wetten: Sport, Politik, Wirtschaft, Wetter — alles. Der Preis zeigt dir, wie wahrscheinlich etwas ist:

| Preis | Bedeutung | Beispiel |
|-------|-----------|----------|
| **10 Cent** | Unwahrscheinlich (10%) | "Wird es morgen in der Sahara schneien?" |
| **30 Cent** | Aussenseiter (30%) | "Gewinnen die Tigers heute?" |
| **50 Cent** | Muenzwurf (50/50) | "Kopf oder Zahl" |
| **80 Cent** | Sehr wahrscheinlich (80%) | "Gewinnt der Tabellenerste?" |
| **95 Cent** | Fast sicher (95%) | "Geht morgen die Sonne auf?" |

**So funktioniert das Geld:**

```
Du kaufst "Lakers gewinnen" fuer 30 Cent pro Anteil.
Du kaufst 10 Anteile = $3.00 investiert.

Fall A: Lakers gewinnen → 10 Anteile x $1.00 = $10.00 (Gewinn: $7.00)
Fall B: Lakers verlieren → 10 Anteile x $0.00 = $0.00 (Verlust: $3.00)
```

Je billiger du kaufst, desto mehr Gewinn wenn es klappt — aber desto unwahrscheinlicher ist es.

---

## Was macht der Bot?

Auf Polymarket gibt es Profis die den ganzen Tag Statistiken analysieren und damit Millionen verdienen. Unser Bot **schaut denen ueber die Schulter** und kopiert ihre Wetten automatisch.

```
1. Trader kauft "Lakers gewinnen" fuer $500 bei 40 Cent
2. Bot erkennt das nach 5 Sekunden
3. Bot kauft dasselbe fuer $15 bei 40 Cent
4. Lakers gewinnen!
5. Trader bekommt $1250 (Gewinn $750)
6. Bot bekommt $37.50 (Gewinn $22.50)
```

Du musst nichts ueber Sport oder Politik wissen. Der Bot kopiert einfach die Leute die sich auskennen.

### Warum nicht einfach ALLES kopieren?

Weil selbst Profis manchmal Mist machen. Deshalb hat der Bot ein ganzes System an Filtern und Sicherheiten. Dazu gleich mehr.

---

## Features

- **Copy Trading** — Kopiert Positionen von gefolgten Tradern innerhalb von 5 Sekunden
- **Proportionales Sizing** — Wetteinsatz skaliert mit der Ueberzeugung des Traders (0.2x bis 3.0x)
- **Hedge-Erkennung** — Erkennt wenn ein Trader beide Seiten kauft und ueberspringt beides
- **Fast-Sell** — Kopiert Verkaeufe der Trader innerhalb von 5 Sekunden
- **Auto-Sell** — Verkauft gewonnene Positionen ab 96 Cent automatisch (Kapital recyceln)
- **Auto-Close** — Markiert verlorene Positionen (0 Cent) als geschlossen
- **Auto-Redeem** — Loest gewonnene Positionen ueber den Builder Relayer ein (keine Gaskosten)
- **Stale-Position-Erkennung** — Schliesst Positionen die aus der Trader-Wallet verschwunden sind
- **Performance Report** — Automatischer Bericht alle 10 Minuten mit Gewinn/Verlust pro Trader
- **Live Dashboard** — Echtzeit-Weboberflaeche mit Equity-Kurve, Aktivitaets-Log, Meme-GIFs
- **Sport-Erkennung** — Automatische Emoji-Tags (MLB, NBA, NHL, NFL, CS2, LoL, Dota, Valorant, Tennis, Geopolitik)

---

## Wie funktioniert das Sizing?

Der Bot kopiert nicht einfach blind — er schaut wie UEBERZEUGT der Trader ist.

### Conviction Ratio (Ueberzeugung)

Jeder Trader hat eine durchschnittliche Wettgroesse. Wenn er mehr als normal setzt, ist er sich sicher. Wenn weniger, ist es wahrscheinlich nur ein Test.

```
Trader setzt normalerweise $100 pro Wette (Durchschnitt).

Wette A: Trader setzt $300 (3x Durchschnitt = sehr ueberzeugt)
  → Bot setzt: $15 Basis x 3.0 Ratio = $45

Wette B: Trader setzt $100 (1x Durchschnitt = normal)
  → Bot setzt: $15 Basis x 1.0 Ratio = $15

Wette C: Trader setzt $10 (0.1x Durchschnitt = nur ein Test)
  → Bot setzt: $15 Basis x 0.2 Ratio (Minimum) = $3
```

Der Ratio wird begrenzt durch `RATIO_MIN` (Standard: 0.2x) und `RATIO_MAX` (Standard: 3.0x).

### Preis-Signal

Wetten weit weg von 50 Cent zeigen staerkere Ueberzeugung. Wenn ein Trader bei 15 Cent kauft (= "das passiert mit 15% Chance, aber ich glaub dran"), ist das ein staerkeres Signal als bei 50 Cent (= Muenzwurf).

```
Preis 15c → Starkes Signal  → Einsatz x 1.5
Preis 30c → Normales Signal → Einsatz x 1.0
Preis 45c → Schwaches Signal → Einsatz x 0.6
```

### Die komplette Formel

```
Endgroesse = Basis x Preis-Multiplikator x Conviction-Ratio

Basis     = Wallet x BET_SIZE_PCT (oder BET_SIZE_MAP pro Trader)
Preis     = 1.5x (stark) / 1.0x (normal) / 0.6x (schwach)
Conviction = Trader-Wette / Trader-Durchschnitt (begrenzt auf RATIO_MIN bis RATIO_MAX)
```

**Konkretes Beispiel:**
```
Wallet: $300
Trader: xsaghav (BET_SIZE_MAP = 0.07 = 7%)
Trader-Durchschnitt: $25
Trader setzt diesmal: $75 (= 3x Durchschnitt)
Preis: 20 Cent (Edge = 0.30 → starkes Signal)

Basis:      $300 x 0.07 = $21
Preis-Mult: 1.5x (stark)
Conviction: 3.0x (begrenzt auf RATIO_MAX=3.0)

Endgroesse: $21 x 1.5 x 3.0 = $94.50
→ Begrenzt auf MAX_POSITION_SIZE = $30
```

---

## Wie funktioniert die Hedge-Erkennung?

Manche Trader kaufen BEIDE Seiten eines Marktes. Zum Beispiel "Lakers gewinnen" UND "Celtics gewinnen". Das ist wie gleichzeitig auf Rot UND Schwarz setzen — egal was passiert, du verlierst die Gebuehren.

Der Bot erkennt das:

```
Sekunde 0:  Trader kauft "Lakers gewinnen"
            → Bot: "Hmm, warte 60 Sekunden..."

Sekunde 30: Trader kauft "Celtics gewinnen"
            → Bot: "AHA! Hedge erkannt! Beide uebersprungen."

Haette der Trader NICHTS mehr gekauft:
Sekunde 60: → Bot: "Keine Gegenwette → echte Ueberzeugung → KAUFEN!"
```

Konfigurierbar pro Trader: `HEDGE_WAIT_TRADERS=xsaghav:60,sovereign2013:60`

---

## Wie funktioniert Event-Timing?

Wenn ein NBA-Spiel erst in 8 Stunden anfaengt, macht es keinen Sinn jetzt schon zu kaufen. Der Preis kann sich noch stark aendern, und das Geld ist stundenlang blockiert.

```
MAX_HOURS_BEFORE_EVENT=2  (nur 2 Stunden vorher kaufen)

Trader kauft "Lakers gewinnen" — Spiel ist in 6 Stunden
  → Bot: "Zu frueh. Ich merke mir das und warte."

4 Stunden spaeter (Spiel in 2 Stunden):
  → Bot: "Jetzt passt's!" → Schaut aktuellen Preis → Kauft (oder nicht)
```

### Preis-Drift-Filter

Wenn der Trader bei 50 Cent gekauft hat, aber 4 Stunden spaeter steht der Preis bei 60 Cent — dann ist der Trade nicht mehr so gut. Der Bot prueft ob sich der Preis zu stark bewegt hat:

| Einstiegspreis | Max erlaubte Drift | Beispiel |
|---|---|---|
| Unter 20c (Lotterie) | 30% | 15c → 19c OK, 15c → 20c SKIP |
| 20-40c (Aussenseiter) | 40% | 30c → 42c OK, 30c → 43c SKIP |
| 40-60c (Muenzwurf) | 3% | 50c → 51c OK, 50c → 52c SKIP |
| 60-85c (Favorit) | 5% | 70c → 73c OK, 70c → 74c SKIP |

Muenzwuerfe (40-60c) haben nur minimale Gewinnmarge — da reicht schon 3% Drift um den Profit zu zerstoeren. Aussenseiter (20-40c) haben viel mehr Marge und vertragen mehr Drift.

---

## Wie kauft und verkauft der Bot?

### Kaufen (Buy)

1. Bot erkennt neuen Trade vom Trader
2. Alle Filter werden geprueft (Preis, Groesse, Hedge, Exposure, etc.)
3. Bot berechnet Einsatzgroesse (Sizing-Formel)
4. Bot schickt Kauf-Order an Polymarket CLOB API
5. Falls die Order nicht durchgeht: Bot versucht mit mehr Slippage (+5c, +8c, +12c)
6. Falls die Order "delayed" (verzoegert) ist: Bot wartet 8 Sekunden und prueft ob USDC abgebucht wurde
7. Bot speichert den Trade in der Datenbank

### Verkaufen (Sell)

Der Bot verkauft in mehreren Situationen:

- **Fast-Sell** — Trader verkauft → Bot verkauft auch (innerhalb 5 Sekunden)
- **Auto-Sell** — Preis steigt auf 96+ Cent → Bot verkauft automatisch (Kapital recyceln)
- **Take-Profit** — Position hat X% Gewinn erreicht → Bot verkauft
- **Stop-Loss** — Position hat X% Verlust → Bot verkauft
- **Auto-Close** — Preis faellt auf 0-1 Cent → Position als Verlust markiert (kein Verkauf noetig, wertlos)
- **Stale-Close** — Position verschwunden aus Trader-Wallet → nach 180 Pruefungen automatisch geschlossen

Sell-Orders haben auch Retry mit Slippage (-1c, -3c, -6c) und Delayed-Verification.

### Redeem (Gewinne einloesen)

Wenn ein Markt offiziell ausgewertet wird ("resolved"), muessen die Gewinne eingeloest werden. Das geht ueber den Builder Relayer (keine Gaskosten noetig):

```bash
python redeem_positions.py --exec
```

---

## Alle Sicherheits-Features

### Cash Floor (Notbremse)

Unter einem bestimmten Betrag kauft der Bot nichts mehr. Damit du nie auf $0 gehst.

```
CASH_FLOOR=20        → Unter $20 wird nicht mehr gekauft
CASH_RECOVERY=6      → Erst wenn Cash $26 erreicht ($20 + $6) wird wieder gekauft
SAVE_POINT_STEP=1    → Danach steigt der Floor auf $21, naechste Recovery bei $27, usw.
```

**Beispiel-Ablauf:**
```
Cash faellt auf $20  → STOP (Cash Floor erreicht)
Cash steigt auf $26  → Kaufen erlaubt! Floor steigt auf $21
Cash faellt auf $21  → STOP
Cash steigt auf $27  → Kaufen erlaubt! Floor steigt auf $22
...und so weiter
```

### Max Exposure pro Trader

Jeder Trader darf nur einen bestimmten Prozentsatz deines Portfolios nutzen. Damit ein einzelner Trader nicht alles verzocken kann.

```
Portfolio: $400 (Wallet $200 + Positionen $200)
MAX_EXPOSURE_PER_TRADER=0.33 (33%)

Trader A: max $132 → hat $100 investiert → kann noch $32 mehr
Trader B: max $132 → hat $50 investiert  → kann noch $82 mehr
Trader C: max $132 → hat $0 investiert   → kann noch $132 mehr
```

Pro Trader ueberschreibbar: `TRADER_EXPOSURE_MAP=xsaghav:0.65,sovereign2013:0.40`

Die Limits sind UNABHAENGIG — sie muessen nicht 100% ergeben. 65% + 40% + 30% ist voellig OK. Der Cash Floor verhindert dass die Wallet leer wird.

### Max pro Event/Match

Ein Trader setzt manchmal 5 verschiedene Wetten auf dasselbe Spiel (Spread -17.5, Spread -18.5, O/U 245, O/U 246, O/U 248). Wenn das Spiel schiefgeht, verlierst du bei ALLEN gleichzeitig.

```
MAX_PER_EVENT=15   → Maximal $15 pro Spiel (erste Wette kopiert, Rest blockiert)
MAX_PER_MATCH=15   → Gilt auch fuer zusammengehoerende Maerkte (Map 1 + Map 2 + BO3 = 1 Match)
```

**Echtes Beispiel:** sovereign2013 hat 5 Wetten auf Wizards vs Heat platziert ($39 total). Alle verloren. Mit `MAX_PER_EVENT=15` waere nur 1 Wette ($10) kopiert worden — $29 gespart.

### Circuit Breaker (Sicherung)

Wenn die Polymarket API 8x hintereinander fehlschlaegt, pausiert der Bot 60 Sekunden. Verhindert dass der Bot in einer Stoerung wild Orders schickt.

```
CB_THRESHOLD=8      → Nach 8 Fehlern: Pause
CB_PAUSE_SECS=60    → 60 Sekunden warten, dann weiter
```

### Taegliche Limits (optional)

```
MAX_DAILY_LOSS=50     → Bot stoppt nach $50 Tagesverlust
MAX_DAILY_TRADES=20   → Maximal 20 neue Trades pro Tag
STOP_LOSS_PCT=0.50    → Verkauft automatisch bei 50% Verlust
TAKE_PROFIT_PCT=2.0   → Verkauft automatisch bei 200% Gewinn
```

Alle standardmaessig deaktiviert (0 = aus).

---

## Dashboard

Eine Website auf deinem Server die in Echtzeit zeigt was passiert:

- **Kennzahlen** — Gesamtwert, Gewinn/Verlust, Wallet-Balance, offene Positionen, Win-Rate
- **Equity-Kurve** — Wie sich dein Portfolio ueber Zeit entwickelt (4H/1D/1W/1M/Alles)
- **Performance-Report** — Automatischer Bericht pro Trader mit Gewinn/Verlust-Aufteilung
- **Aktivitaets-Log** — Live-Feed aller Kaeufe, Verkaeufe, Gewinne, Verluste mit Sport-Emojis
- **Aktive Positionen** — Alle offenen Wetten mit aktuellem Gewinn/Verlust
- **Geschlossene Positionen** — Handelshistorie sortiert nach Datum
- **Exposure-Meter** — Balken pro Trader der zeigt wie viel vom Budget verbraucht ist
- **Meme-System** — Weil Wetten Spass machen soll:
  - Gewinn: Hasbulla Geld-GIF + "Here Comes The Money" Sound
  - Grosser Gewinn (50%+ ROI): Vince McMahon Reaktion + Geld-Sound
  - Verlust: GTA "WASTED" + Bildschirm-Wackeln + zufaellige Sprueche
  - Neuer Trade: "Shut Up And Take My Money" GIF + WWE Glocke
  - Gewinnserie (3x/5x/7x): John Cena GIF + eskalierende Sounds
  - Verlustserie (3x/5x/7x): "This is Fine" / Clown Makeup + Curb Your Enthusiasm
  - Auszahlung: Dave Chappelle Geld-GIF + Kasse-Sound
- **Sound-Einstellungen** — Alles einzeln ein/ausschaltbar mit Test-Buttons
- **Widescreen-Modus** — Fullscreen-Layout fuer grosse Bildschirme
- **Mobile** — Tabellen horizontal scrollbar auf kleinen Screens

---

## Installation

### 1. Code herunterladen

```bash
git clone https://github.com/Super-Sauna-Club/polymarket-copy-bot.git
cd polymarket-copy-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Konfiguration

Es gibt **zwei** Config-Dateien (nicht eine!):

| Datei | Inhalt | Im Git? |
|-------|--------|---------|
| `secrets.env` | Private Keys, API-Keys, Passwoerter | **NEIN** (gitignored) |
| `settings.env` | Bot-Einstellungen (Trader, Groessen, Filter) | **NEIN** (gitignored) |
| `secrets.example.env` | Vorlage fuer secrets.env | Ja |
| `settings.example.env` | Vorlage fuer settings.env | Ja |

```bash
cp secrets.example.env secrets.env      # Deine Keys eintragen
cp settings.example.env settings.env    # Bot-Settings anpassen
```

#### secrets.env (Pflicht)

```env
# Polymarket CLOB API (PFLICHT fuer Live-Trading)
# Private Key aus deiner Polymarket Wallet exportieren
POLYMARKET_PRIVATE_KEY=dein_private_key
POLYMARKET_FUNDER=deine_proxy_wallet_adresse

# Builder API (PFLICHT fuer Auto-Redeem von Gewinnen)
# Holen von: polymarket.com/settings → Builder → Create New
BUILDER_KEY=dein_key
BUILDER_SECRET=dein_secret
BUILDER_PASSPHRASE=dein_passphrase

# Dashboard-Passwort (fuer Follow/Unfollow/Reset API)
DASHBOARD_SECRET=dein_passwort
```

#### settings.env (Anpassen)

Kopiere `settings.example.env` und passe an. Die wichtigsten Einstellungen:

```env
# Welche Trader kopieren? (Name:Wallet-Adresse)
FOLLOWED_TRADERS=TraderName:0xAdresse,AndererTrader:0xAdresse

# Echtgeld oder nur tracken?
LIVE_MODE=true

# Wie viel hast du eingezahlt? (fuer Gewinn/Verlust-Berechnung)
STARTING_BALANCE=200

# Hedge-Erkennung: 60 Sekunden warten bevor kopiert wird
HEDGE_WAIT_TRADERS=TraderName:60,AndererTrader:60
```

Alle weiteren Einstellungen sind optional und haben sinnvolle Standardwerte. Siehe `settings.example.env` fuer die komplette Liste mit Erklaerungen.

### 3. Trader finden

Geh auf [polymarket.com/leaderboard](https://polymarket.com/leaderboard) und suche profitable Trader. Worauf achten:

- **Konstant positiver Gewinn** ueber alle Zeitraeume (nicht nur letzte Woche)
- **Win-Rate ueber 55%** (unter 55% verlierst du durch Gebuehren)
- **Fokus auf eine Kategorie** (Sport-Spezialist > Allrounder)
- **Vernuenftige Positionsgroessen** (nicht alles auf eine Wette)
- **Wenig Hedging** (nicht staendig beide Seiten kaufen)

Wallet-Adresse vom Profil kopieren und in `FOLLOWED_TRADERS` eintragen.

### 4. Starten

```bash
# ZUERST im Paper-Modus testen (kein echtes Geld)
LIVE_MODE=false python main.py

# Wenn alles funktioniert: Echtgeld
python main.py
```

Dashboard oeffnen: `http://localhost:8090`

### 5. Auto-Redeem (optional)

Gewonnene Positionen muessen eingeloest werden um USDC zurueckzubekommen:

```bash
# Erst trocken schauen was eingeloest werden kann
python redeem_positions.py

# Dann wirklich einloesen
python redeem_positions.py --exec
```

Am besten als Cron-Job alle 15 Minuten einrichten.

### 6. Als Service einrichten (Server)

Damit der Bot im Hintergrund laeuft und nach Neustart automatisch startet:

```bash
sudo nano /etc/systemd/system/polybot.service
```

```ini
[Unit]
Description=Polymarket Copy Trading Bot
After=network.target

[Service]
Type=simple
User=dein_user
WorkingDirectory=/pfad/zum/polymarket-copy-bot
ExecStart=/pfad/zum/polymarket-copy-bot/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable polybot
sudo systemctl start polybot

# Logs anschauen
sudo journalctl -u polybot -f
```

---

## Alle Einstellungen

Alle Einstellungen kommen in `settings.env`. Nur `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER` und `FOLLOWED_TRADERS` sind Pflicht. Alles andere hat sinnvolle Standardwerte.

### Kern

| Einstellung | Standard | Erklaerung |
|-------------|----------|------------|
| `LIVE_MODE` | false | false = nur tracken (kein Geld), true = echtes Geld |
| `STARTING_BALANCE` | 320 | Wie viel du eingezahlt hast (fuer Gewinn/Verlust-Berechnung) |
| `COPY_SCAN_INTERVAL` | 5 | Alle X Sekunden nach neuen Trades schauen |
| `DASHBOARD_PORT` | 8090 | Port fuer die Web-Oberflaeche |

### Wettgroesse

| Einstellung | Standard | Erklaerung |
|-------------|----------|------------|
| `BET_SIZE_PCT` | 0.05 | Basis-Wette = 5% vom Portfolio (~$15 bei $300) |
| `MAX_POSITION_SIZE` | 30 | Maximum $30 pro einzelne Position |
| `MIN_TRADE_SIZE` | 1.0 | Minimum $1 pro Wette |
| `RATIO_MIN` | 0.2 | Minimum-Multiplikator (kleine Trader-Wette → 0.2x) |
| `RATIO_MAX` | 3.0 | Maximum-Multiplikator (grosse Trader-Wette → 3.0x) |
| `BET_SIZE_BASIS` | cash | `cash` = nur Wallet-Balance, `portfolio` = Wallet + Positionen |
| `BET_SIZE_MAP` | | Pro Trader eigene Basis (z.B. `xsaghav:0.07,Jargs:0.02`) |
| `DEFAULT_AVG_TRADER_SIZE` | 10.0 | Fallback Durchschnittswette wenn keine Daten |
| `AVG_TRADER_SIZE_MAP` | | Pro Trader Durchschnittswette (z.B. `xsaghav:25,sovereign2013:1400`) |

### Preis-Signal-Multiplikatoren

| Einstellung | Standard | Erklaerung |
|-------------|----------|------------|
| `PRICE_EDGE_HIGH` | 0.30 | Ab dieser Distanz von 50c → starkes Signal |
| `PRICE_MULT_HIGH` | 1.50 | Multiplikator fuer starkes Signal |
| `PRICE_EDGE_MED` | 0.15 | Ab dieser Distanz → normales Signal |
| `PRICE_MULT_MED` | 1.00 | Multiplikator fuer normales Signal |
| `PRICE_MULT_LOW` | 0.60 | Multiplikator fuer schwaches Signal (nahe 50c) |

### Trade-Filter

| Einstellung | Standard | Erklaerung |
|-------------|----------|------------|
| `MIN_TRADER_USD` | 5 | Trader muss mindestens $5 setzen damit kopiert wird |
| `MIN_TRADER_USD_MAP` | | Pro Trader (z.B. `sovereign2013:750` = nur grosse Wetten) |
| `MIN_ENTRY_PRICE` | 0.08 | Unter 8 Cent nicht kaufen (zu spekulativ) |
| `MIN_ENTRY_PRICE_MAP` | | Pro Trader (z.B. `sovereign2013:0.40` = nur ab 40c) |
| `MAX_ENTRY_PRICE` | 0.85 | Ueber 85 Cent nicht kaufen (zu wenig Gewinnmarge) |
| `MAX_ENTRY_PRICE_MAP` | | Pro Trader (z.B. `sovereign2013:0.75` = max 75c) |
| `MAX_COPIES_PER_MARKET` | 1 | 1 Kopie pro Markt (kein Doppelkauf) |
| `ENTRY_TRADE_SEC` | 300 | Trades aelter als 5 Minuten ignorieren |
| `MAX_SPREAD` | 0.05 | Max 5% Spread (Differenz zwischen Kauf- und Verkaufspreis) |
| `NO_REBUY_MINUTES` | 0 | Nach Verkauf X Minuten Sperre fuer selben Markt (0=aus) |

### Event-Timing

| Einstellung | Standard | Erklaerung |
|-------------|----------|------------|
| `MAX_HOURS_BEFORE_EVENT` | 2 | Erst X Stunden vor Event kaufen (0=sofort) |
| `EVENT_WAIT_MIN_CASH` | 0 | Nur warten wenn Cash unter $X (0=immer warten) |
| `EVENT_WAIT_MAX_SECS` | 14400 | Gequeute Trades max 4 Stunden aufheben |

### Queue-Drift-Filter

| Einstellung | Standard | Erklaerung |
|-------------|----------|------------|
| `QUEUE_DRIFT_LOTTERY` | 0.30 | Unter 20c: max 30% Preisanstieg |
| `QUEUE_DRIFT_UNDERDOG` | 0.40 | 20-40c: max 40% Preisanstieg |
| `QUEUE_DRIFT_COINFLIP` | 0.03 | 40-60c: max 3% Preisanstieg |
| `QUEUE_DRIFT_FAVORITE` | 0.05 | 60-85c: max 5% Preisanstieg |

### Maximale Einsaetze pro Spiel

| Einstellung | Standard | Erklaerung |
|-------------|----------|------------|
| `MAX_PER_EVENT` | 15 | Max $15 pro Event/Spiel |
| `MAX_PER_MATCH` | 15 | Max $15 ueber zusammengehoerende Maerkte (Map 1 + Map 2 + BO3) |

### Hedge-Erkennung

| Einstellung | Standard | Erklaerung |
|-------------|----------|------------|
| `HEDGE_WAIT_SECS` | 60 | Standard-Wartezeit in Sekunden |
| `HEDGE_WAIT_TRADERS` | | Pro Trader: `name:sekunden,name:sekunden` |

### Cash-Management

| Einstellung | Standard | Erklaerung |
|-------------|----------|------------|
| `CASH_FLOOR` | 0 | Unter diesem Betrag nicht mehr kaufen |
| `CASH_RECOVERY` | 6 | Cash muss um $6 ueber Floor steigen bevor wieder gekauft wird |
| `SAVE_POINT_STEP` | 1.0 | Floor steigt um $1 pro Recovery-Zyklus |
| `CASH_RESERVE` | 0 | Dollar die NIEMALS fuer Wetten verwendet werden |
| `MAX_OPEN_POSITIONS` | 100 | Maximale gleichzeitig offene Positionen |
| `MAX_EXPOSURE_PER_TRADER` | 0.33 | Standard: max 33% vom Portfolio pro Trader |
| `TRADER_EXPOSURE_MAP` | | Pro Trader: `name:prozent` (z.B. `xsaghav:0.65`) |

### Risiko-Management

Alles standardmaessig aus (0 = deaktiviert).

| Einstellung | Standard | Erklaerung |
|-------------|----------|------------|
| `MAX_DAILY_LOSS` | 0 | Trading stoppt bei $X Tagesverlust |
| `MAX_DAILY_TRADES` | 0 | Max neue Trades pro Tag |
| `STOP_LOSS_PCT` | 0 | Auto-Verkauf bei X% Verlust (z.B. 0.50 = 50%) |
| `TAKE_PROFIT_PCT` | 0 | Auto-Verkauf bei X% Gewinn (z.B. 2.0 = 200%) |
| `TAKE_PROFIT_MAP` | | Pro Trader (z.B. `xsaghav:9.0` = 900% TP) |

### Auto-Sell / Auto-Close

| Einstellung | Standard | Erklaerung |
|-------------|----------|------------|
| `AUTO_SELL_PRICE` | 0.96 | Gewonnene Positionen ab 96c verkaufen |
| `AUTO_CLOSE_WON_PRICE` | 0.99 | Ab 99c als gewonnen markieren |
| `AUTO_CLOSE_LOST_PRICE` | 0.01 | Unter 1c als verloren markieren |

### Order-Ausfuehrung

| Einstellung | Standard | Erklaerung |
|-------------|----------|------------|
| `BUY_SLIPPAGE_LEVELS` | 0.05,0.08,0.12 | Kauf-Retry: +5c, +8c, +12c Slippage |
| `SELL_SLIPPAGE_LEVELS` | 0.01,0.03,0.06 | Verkauf-Retry: -1c, -3c, -6c Slippage |
| `DELAYED_BUY_VERIFY_SECS` | 8 | Sekunden warten um verzoegerte Kauforder zu verifizieren |
| `DELAYED_SELL_VERIFY_SECS` | 6 | Sekunden warten um verzoegerte Verkauforder zu verifizieren |
| `SELL_VERIFY_THRESHOLD` | 0.5 | Anteil der Shares der verschwunden sein muss (0.5 = 50%) |

### Fill-Verifizierung

| Einstellung | Standard | Erklaerung |
|-------------|----------|------------|
| `FILL_VERIFY_DELAY_SECS` | 2 | Sekunden nach Kauf bevor Fill geprueft wird |
| `MIN_FILL_AMOUNT` | 0.10 | Minimum USDC-Aenderung um als gefuellt zu gelten |

### Pending-Buy-Queue

Trades unter einem Preis-Schwellenwert warten lassen. Standard: aus.

| Einstellung | Standard | Erklaerung |
|-------------|----------|------------|
| `BUY_THRESHOLD` | 0.0 | Unter diesem Preis warten (0=aus) |
| `PENDING_BUY_MIN_SECS` | 210 | Mindestens X Sekunden warten |
| `PENDING_BUY_MAX_SECS` | 900 | Nach X Sekunden verwerfen |

### Feature-Schalter

| Einstellung | Standard | Erklaerung |
|-------------|----------|------------|
| `COPY_SELLS` | true | Verkaeufe der Trader mitmachen? |
| `POSITION_DIFF_ENABLED` | true | Position-Diff Fallback-Scan aktiviert? |
| `IDLE_REPLACE_ENABLED` | false | Inaktive Trader automatisch ersetzen? |
| `IDLE_TRIGGER_SECS` | 1200 | Ab X Sekunden Inaktivitaet ersetzen (20 Min) |
| `IDLE_REPLACE_COOLDOWN` | 1800 | Cooldown nach Trader-Ersetzung (30 Min) |

### Scan-Throttling

| Einstellung | Standard | Erklaerung |
|-------------|----------|------------|
| `MAX_TRADES_PER_SCAN` | 3 | Max neue Trades pro 5-Sekunden-Scan |
| `RECENT_TRADES_LIMIT` | 50 | Trades pro Wallet pro Scan abrufen |

### Circuit Breaker

| Einstellung | Standard | Erklaerung |
|-------------|----------|------------|
| `CB_THRESHOLD` | 8 | Nach X API-Fehlern hintereinander: Pause |
| `CB_PAUSE_SECS` | 60 | X Sekunden Pause |

### API-Tuning

| Einstellung | Standard | Erklaerung |
|-------------|----------|------------|
| `API_TIMEOUT` | 10 | Allgemeiner Request-Timeout in Sekunden |
| `API_MAX_RETRIES` | 3 | Wiederholungsversuche pro Request |
| `GAMMA_API_TIMEOUT` | 5 | Gamma API Timeout (Event-Abfragen) |
| `DATA_API_TIMEOUT` | 15 | Data API Timeout (Positionen/Trades) |
| `WS_RECONNECT_SECS` | 10 | WebSocket Reconnect-Verzoegerung |
| `LIVE_PRICE_MIN` | 0.05 | Min Live-Preis um akzeptiert zu werden |
| `LIVE_PRICE_MAX_DEVIATION` | 0.50 | Max Abweichung vom Trader-Preis (50%) |

### Position-Tracking

| Einstellung | Standard | Erklaerung |
|-------------|----------|------------|
| `MIN_POSITION_SIZE_FILTER` | 0.50 | Min Positionsgroesse fuer Scans |
| `MISS_COUNT_TO_CLOSE` | 180 | Nach X fehlgeschlagenen Pruefungen Position schliessen (0=aus) |
| `RECENTLY_CLOSED_SECS` | 600 | Kuerzlich geschlossene Trades X Sekunden cachen |

---

## Architektur

```
main.py                      → Scheduler + Flask + Startup
├── bot/copy_trader.py       → Kern: Scan, Filter, Hedge-Wait, Fast-Sell, Sizing
├── bot/order_executor.py    → CLOB Orders (Kauf/Verkauf) mit Retry + Verification
├── bot/wallet_scanner.py    → Activity Feed, Positions API
├── bot/ws_price_tracker.py  → WebSocket Echtzeit-Preise
├── bot/ai_report.py         → Performance-Report Generator
├── database/db.py           → Alle Datenbank-Operationen (SQLite + WAL)
├── database/models.py       → Datenbank-Schema
├── config.py                → Laedt secrets.env → settings.env (mit Fallbacks)
├── redeem_positions.py      → Gewinne einloesen via Builder Relayer
└── dashboard/
    ├── app.py               → Flask App, SSE Stream, REST APIs
    └── templates/
        ├── dashboard.html   → Haupt-Dashboard
        ├── index.html       → Einstellungs-Seite
        └── history.html     → Handelshistorie
```

### Wie laedt der Bot die Config?

```python
# config.py laedt in dieser Reihenfolge:
1. secrets.env      → Private Keys, API-Credentials
2. settings.env     → Bot-Einstellungen
3. .env             → Fallback (Legacy, fuer Abwaertskompatibilitaet)
```

Werte aus frueheren Dateien werden NICHT ueberschrieben. Also: wenn etwas in `secrets.env` steht, wird der Wert aus `settings.env` ignoriert.

---

## Risiken

- **Slippage** — 5 Sekunden Verzoegerung heisst du bekommst einen schlechteren Preis als der Trader
- **Gebuehren** — Polymarket nimmt 2% (200 Basispunkte) pro Trade
- **Verluste** — Auch die besten Trader verlieren manchmal. Vergangene Performance garantiert nichts.
- **Liquiditaet** — Kleine Maerkte haben nicht genug Volumen fuer deine Orders
- **Binaere Ergebnisse** — Positionen gehen auf $0 oder $1. Es gibt kein "ein bisschen verloren"
- **API-Ausfaelle** — Polymarket kann down sein. Circuit Breaker schuetzt teilweise

---

## Tech Stack

- Python 3.12+
- Flask (Dashboard + SSE)
- SQLite mit WAL-Modus
- py-clob-client (Polymarket CLOB API)
- poly-web3 (Builder Relayer fuer Redeem)
- WebSocket (Echtzeit-Preise)
- Chart.js (Equity-Kurve)
- APScheduler (Job-Scheduler)

---

## Lizenz

MIT
