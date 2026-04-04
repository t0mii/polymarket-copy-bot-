# Poly Copybot

Automated copy-trading bot for [Polymarket](https://polymarket.com). Follows top traders and automatically copies their positions with real money via the Polymarket CLOB API.

## Features

- **Copy Trading** — Automatically copies trades from followed wallets (e.g. RN1)
- **Smart Filters** — Min trade size, price range, hedge detection, duplicate blocking
- **Fast-Sell Detection** — Detects when trader sells and mirrors within 5 seconds
- **Auto-Close** — Closes positions when markets resolve (via Positions API + Gamma API fallback)
- **Auto-Redeem** — Redeems resolved positions via Polymarket Builder Relayer (gas-free)
- **Live Dashboard** — Real-time web dashboard with SSE updates, trade alerts, equity curve
- **Sound Alerts** — Browser audio notification for new trades and closes
- **Activity Log** — CLI-style live feed of all bot actions

## Dashboard

![Dashboard](https://img.shields.io/badge/Live-Dashboard-gold)

Real-time dashboard showing portfolio value, P&L, active positions, trade record, and activity history. Built with vanilla JS, Chart.js, and Server-Sent Events.

## Architecture

```
main.py                      → Scheduler + Flask + Startup validation
├── bot/copy_trader.py       → Core: trade detection, smart filters, fast-sell
├── bot/order_executor.py    → Real CLOB orders (Buy/Sell via py-clob-client)
├── bot/wallet_scanner.py    → Leaderboard scan, wallet positions, activity feed
├── bot/wallet_analyzer.py   → AI-powered trader analysis (4 AI fallbacks)
├── bot/ws_price_tracker.py  → WebSocket live prices from Polymarket CLOB
├── bot/massive_data.py      → Market data (SPY, BTC etc.)
├── bot/report_generator.py  → HTML report generator
├── database/models.py       → SQLite schema
├── database/db.py           → All DB operations
├── redeem_positions.py      → Auto-redeem via Builder Relayer
└── dashboard/
    ├── app.py               → Flask app, SSE, REST APIs (reads from Polymarket API)
    ├── static/style.css     → Shared CSS
    └── templates/            → Dashboard pages
```

## Setup

### 1. Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your keys
```

Required keys:
- `POLYMARKET_PRIVATE_KEY` — Your Polygon wallet private key
- `POLYMARKET_FUNDER` — Your Polymarket proxy wallet address

Optional:
- `BUILDER_KEY/SECRET/PASSPHRASE` — For auto-redeem (get from polymarket.com/settings → Builder)
- AI API keys for wallet analysis (Groq, Anthropic, Gemini, Z.ai)

### 3. Run

```bash
# Paper mode (no real money)
LIVE_MODE=false python main.py

# Live mode
LIVE_MODE=true python main.py
```

Dashboard at `http://localhost:8090`

### 4. Follow a Trader

```bash
# Via API (replace ADDRESS and KEY)
curl -X POST "http://localhost:8090/api/wallet/ADDRESS/follow?key=YOUR_SECRET"
```

### 5. Auto-Redeem (optional)

```bash
# One-time
python redeem_positions.py --exec

# As systemd timer (every 15 min)
# See PROJECT_INFO.md for systemd setup
```

## Trading Parameters

All configurable via `.env`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `LIVE_MODE` | false | true = real money |
| `STARTING_BALANCE` | 200 | Initial deposit for P&L tracking |
| `MAX_POSITION_SIZE` | 5 | Max $ per position |
| `MIN_TRADE_SIZE` | 1.0 | Min $ per position |
| `CASH_FLOOR` | 0 | Stop buying below this |
| `MAX_OPEN_POSITIONS` | 100 | Max simultaneous positions |
| `COPY_SCAN_INTERVAL` | 5 | Seconds between scans |
| `BET_SIZE_PCT` | 0.02 | % of portfolio per position |
| `MIN_TRADER_USD` | 15 | Only copy trades where trader spends $X+ |
| `MIN_ENTRY_PRICE` | 0.05 | Skip trash farming (<5c) |
| `MAX_ENTRY_PRICE` | 0.92 | Skip hedges (>92c) |
| `MAX_COPIES_PER_MARKET` | 2 | Max copies of same market per wallet |

## Tech Stack

- Python 3.12+
- Flask (dashboard)
- SQLite (trade tracking + activity log)
- py-clob-client (Polymarket CLOB API)
- poly-web3 (Builder Relayer for redemption)
- WebSocket (real-time prices)
- Chart.js (equity curve)

## License

MIT
