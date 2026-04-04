# Poly CopyBot by Super Sauna Club

Automated copy-trading bot for [Polymarket](https://polymarket.com). Follows top traders and copies their positions with real money via the Polymarket CLOB API. Proportional bet sizing mirrors trader conviction — big trader bets = bigger copies, small bets = smaller copies.

## Features

- **Copy Trading** — Copies positions from followed traders within 5 seconds
- **Proportional Sizing** — Bet size scales with trader conviction (configurable 0.2x–3x)
- **Hedge Detection** — Detects when a trader buys both sides and skips the hedge (per-trader configurable)
- **Fast-Sell** — Mirrors trader sells within 5 seconds
- **Auto-Sell** — Sells won positions at 96¢+ to recycle capital
- **Auto-Close** — Marks lost positions (0¢) as closed in DB
- **Auto-Redeem** — Redeems resolved positions via Builder Relayer (gas-free)
- **Performance Report** — Auto-generated every 10 min, shows last positions per trader with verdict
- **Live Dashboard** — Real-time web dashboard with SSE, equity curve, activity log
- **Sport Detection** — Auto emoji tags (⚾ MLB, 🏀 NBA, 🏒 NHL, 🏈 NFL, ⚽ Soccer, 🎮 CS, ⚔️ LOL, 🔫 VAL, 🧙 DOTA, 🎾 ATP)

## Quick Start

### 1. Install

```bash
git clone https://github.com/t0mii/polymarket-copy-bot-.git
cd polymarket-copy-bot-
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your keys and trader addresses
```

**Required:**
- `POLYMARKET_PRIVATE_KEY` — Your Polygon wallet private key
- `POLYMARKET_FUNDER` — Your Polymarket proxy wallet address
- `FOLLOWED_TRADERS` — Traders to copy (format: `Name:0xAddress,Name:0xAddress`)

**Optional:**
- `BUILDER_KEY/SECRET/PASSPHRASE` — For auto-redeem (get from polymarket.com/settings → Builder)
- `DASHBOARD_SECRET` — Protects follow/unfollow API endpoints (default: `changeme`)

### 3. Run

```bash
# Paper mode (no real money)
LIVE_MODE=false python main.py

# Live mode
LIVE_MODE=true python main.py
```

Dashboard at `http://localhost:8090`

### 4. Auto-Redeem (optional)

```bash
python redeem_positions.py --exec
```

## Configuration

All parameters configurable via `.env`:

### Trading
| Parameter | Default | Description |
|-----------|---------|-------------|
| `LIVE_MODE` | false | true = real money |
| `STARTING_BALANCE` | 320 | Total deposited (for P&L calculation) |
| `COPY_SCAN_INTERVAL` | 5 | Seconds between scans |

### Position Sizing
| Parameter | Default | Description |
|-----------|---------|-------------|
| `BET_SIZE_PCT` | 0.05 | Base: 5% of portfolio per trade |
| `MAX_POSITION_SIZE` | 30 | Max $ per single position |
| `MIN_TRADE_SIZE` | 1.0 | Min $ to place a trade |
| `RATIO_MIN` | 0.2 | Min conviction multiplier (small trader bet) |
| `RATIO_MAX` | 3.0 | Max conviction multiplier (large trader bet) |

### Filters
| Parameter | Default | Description |
|-----------|---------|-------------|
| `MIN_TRADER_USD` | 3 | Only copy when trader spends $X+ |
| `MIN_ENTRY_PRICE` | 0.05 | Skip trash farming below 5¢ |
| `MAX_ENTRY_PRICE` | 0.92 | Skip hedges above 92¢ |
| `MAX_COPIES_PER_MARKET` | 1 | One copy per market (no doubling up) |
| `MAX_SPREAD` | 0.05 | Max bid/ask spread |
| `ENTRY_TRADE_SEC` | 300 | Max trade age to copy (seconds) |

### Hedge Detection
| Parameter | Default | Description |
|-----------|---------|-------------|
| `HEDGE_WAIT_SECS` | 60 | Default wait time for hedge detection |
| `HEDGE_WAIT_TRADERS` | | Per-trader: `name:secs,name:secs` |

### Cash Management
| Parameter | Default | Description |
|-----------|---------|-------------|
| `CASH_FLOOR` | 0 | Stop buying below this cash level |
| `CASH_RECOVERY` | 6 | Recovery threshold |
| `MAX_OPEN_POSITIONS` | 100 | Max simultaneous positions |

## How It Works

1. **Scan** — Every 5s, fetches recent buys from followed traders via Polymarket Activity API
2. **Filter** — Applies smart filters (min size, price range, hedge detection, duplicates)
3. **Size** — Calculates proportional bet size based on trader's conviction signal
4. **Execute** — Places real CLOB order via py-clob-client
5. **Monitor** — Updates prices every 30s, auto-sells at 96¢+, marks losses at 0¢
6. **Report** — Auto-generates performance report every 10 minutes

## Architecture

```
main.py                      → Scheduler + Flask + Startup
├── bot/copy_trader.py       → Core: scan, filters, hedge-wait, fast-sell
├── bot/order_executor.py    → CLOB orders (Buy/Sell)
├── bot/wallet_scanner.py    → Activity feed, positions API
├── bot/ws_price_tracker.py  → WebSocket live prices
├── bot/ai_report.py         → Performance report generator
├── database/models.py       → SQLite schema
├── database/db.py           → All DB operations
├── redeem_positions.py      → Auto-redeem via Builder Relayer
└── dashboard/
    ├── app.py               → Flask app, SSE, REST APIs
    └── templates/
        ├── dashboard.html   → Main dashboard
        ├── index.html       → Settings page
        └── history.html     → Position history
```

## Tech Stack

- Python 3.12+
- Flask (dashboard + SSE)
- SQLite with WAL mode (concurrent access)
- py-clob-client (Polymarket CLOB API)
- poly-web3 (Builder Relayer for redemption)
- WebSocket (real-time prices)
- Chart.js (equity curve)

## License

MIT
