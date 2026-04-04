# Poly CopyBot by Super Sauna Club

Automated copy-trading bot for [Polymarket](https://polymarket.com). Follows top traders and copies their positions with real money via the Polymarket CLOB API.

## Why Copy Trading?

Polymarket's top traders make millions by analyzing sports, politics, and event outcomes. This bot watches their trades in real-time and mirrors them on your wallet — you profit from their research without doing it yourself.

**Example:** A top trader buys "Detroit Tigers to win" at 30¢ for $500. Your bot detects this within 5 seconds and buys the same position for $15. If the Tigers win, both of you cash out at $1.00 — the trader makes $835, you make $35.

## How the Sizing Works

The bot doesn't just copy blindly — it mirrors the trader's **conviction level**:

```
Trader normally bets $100 per trade (average).

Trade A: Trader bets $200 (2x average = high conviction)
  → Bot bets: $15 base × 2.0 ratio = $30

Trade B: Trader bets $10 (0.1x average = low conviction / noise)
  → Bot bets: $15 base × 0.2 ratio (min) = $3

Trade C: Trader bets $100 (1x average = normal)
  → Bot bets: $15 base × 1.0 ratio = $15
```

Big trader bet = strong signal → we bet more. Small trader bet = noise → we bet less. This is controlled by `RATIO_MIN` (floor) and `RATIO_MAX` (ceiling).

## Hedge Detection

Some traders buy **both sides** of a market (e.g. Over AND Under) as a hedge. Copying both sides means guaranteed loss from fees. The bot detects this:

```
1. Trader buys "Cardinals Over 9" → Bot queues trade, waits 60 seconds
2. Within 60s, trader buys "Cardinals Under 9" → HEDGE DETECTED → skip both
3. If no opposite buy within 60s → conviction trade → execute
```

Configurable per trader via `HEDGE_WAIT_TRADERS=tradername:60`.

## Features

- **Copy Trading** — Copies positions from followed traders within 5 seconds
- **Proportional Sizing** — Bet size scales with trader conviction (0.2x–2.0x)
- **Hedge Detection** — Detects and skips both-sides hedges (per-trader configurable)
- **Fast-Sell** — Mirrors trader sells within 5 seconds
- **Auto-Sell** — Sells won positions at 96¢+ to recycle capital
- **Auto-Close** — Marks lost positions (0¢) as closed
- **Auto-Redeem** — Redeems resolved positions via Builder Relayer (gas-free)
- **Performance Report** — Auto-generated every 10 min with per-trader P&L breakdown
- **Live Dashboard** — Real-time web UI with equity curve, activity log, position tables
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
```

Edit `.env` with your values:

```env
# Required
POLYMARKET_PRIVATE_KEY=your_private_key
POLYMARKET_FUNDER=your_proxy_wallet_address
FOLLOWED_TRADERS=TraderName:0xAddress,AnotherTrader:0xAddress

# Optional but recommended
LIVE_MODE=true
STARTING_BALANCE=200          # How much you deposited (for P&L tracking)
HEDGE_WAIT_TRADERS=TraderName:60  # Traders that hedge (wait 60s before copying)

# For auto-redeem (get from polymarket.com/settings → Builder)
BUILDER_KEY=your_key
BUILDER_SECRET=your_secret
BUILDER_PASSPHRASE=your_passphrase
```

### 3. Find Traders to Follow

Go to [polymarket.com/leaderboard](https://polymarket.com/leaderboard) and find profitable traders. Look for:
- Consistent positive P&L across all timeframes
- High win rate (>55%)
- Focus on sports/events you understand
- Reasonable position sizes (not 100% on one bet)
- Low hedging (not buying both sides constantly)

Copy their wallet address from their profile page and add to `FOLLOWED_TRADERS`.

### 4. Run

```bash
# Paper mode first (no real money, just tracks)
LIVE_MODE=false python main.py

# When ready for real money
LIVE_MODE=true python main.py
```

Dashboard at `http://localhost:8090`

### 5. Auto-Redeem (optional)

Won positions need to be redeemed to get USDC back. Run periodically:

```bash
python redeem_positions.py --exec

# Or set up a cron/systemd timer for every 15 minutes
```

## Configuration Reference

### Position Sizing
| Parameter | Default | Description |
|-----------|---------|-------------|
| `BET_SIZE_PCT` | 0.05 | Base bet = 5% of portfolio (~$15 at $300) |
| `MAX_POSITION_SIZE` | 30 | Hard cap per position |
| `MIN_TRADE_SIZE` | 1.0 | Minimum bet size |
| `RATIO_MIN` | 0.2 | Floor multiplier (small trader bet → small copy) |
| `RATIO_MAX` | 2.0 | Ceiling multiplier (big trader bet → bigger copy) |

### Filters
| Parameter | Default | Description |
|-----------|---------|-------------|
| `MIN_TRADER_USD` | 3 | Ignore trader buys below $3 (noise filter) |
| `MIN_ENTRY_PRICE` | 0.05 | Skip trash farming below 5¢ |
| `MAX_ENTRY_PRICE` | 0.92 | Skip near-certain bets above 92¢ |
| `MAX_COPIES_PER_MARKET` | 1 | One copy per market (prevents doubling up) |
| `ENTRY_TRADE_SEC` | 300 | Ignore trades older than 5 minutes |

### Hedge Detection
| Parameter | Default | Description |
|-----------|---------|-------------|
| `HEDGE_WAIT_SECS` | 60 | Default hedge detection window |
| `HEDGE_WAIT_TRADERS` | | Per-trader: `name:seconds,name:seconds` |

### Other
| Parameter | Default | Description |
|-----------|---------|-------------|
| `LIVE_MODE` | false | false = paper trading, true = real money |
| `STARTING_BALANCE` | 320 | Your deposit amount (for P&L calculation) |
| `COPY_SCAN_INTERVAL` | 5 | Seconds between scans |
| `CASH_FLOOR` | 0 | Stop buying below this cash level |
| `MAX_OPEN_POSITIONS` | 100 | Maximum simultaneous positions |
| `MAX_EXPOSURE_PER_TRADER` | 0.33 | Default max % per trader |
| `TRADER_EXPOSURE_MAP` | | Per-trader: `name:pct,name:pct` |
| `DASHBOARD_PORT` | 8090 | Web dashboard port |
| `DASHBOARD_SECRET` | changeme | Secret key for follow/unfollow API |

## Dashboard

The dashboard shows real-time data directly from the Polymarket API:

- **Metric Cards** — Total value, profit, wallet, open P&L, win rate
- **Performance Report** — Auto-generated every 10 min, per-trader breakdown
- **Activity Log** — Live feed of buys, sells, wins, losses with sport emojis
- **Active Positions** — All open positions with entry/current price and P&L
- **Closed Positions** — Trade history sorted by most recent
- **Equity Curve** — Portfolio value over time

## Architecture

```
main.py                      → Scheduler + Flask + Startup
├── bot/copy_trader.py       → Core: scan, filters, hedge-wait, fast-sell
├── bot/order_executor.py    → CLOB orders (Buy/Sell)
├── bot/wallet_scanner.py    → Activity feed, positions API
├── bot/ws_price_tracker.py  → WebSocket live prices
├── bot/ai_report.py         → Performance report generator
├── database/db.py           → All DB operations
├── redeem_positions.py      → Auto-redeem via Builder Relayer
└── dashboard/
    ├── app.py               → Flask app, SSE, REST APIs
    └── templates/
        ├── dashboard.html   → Main dashboard
        ├── index.html       → Settings page
        └── history.html     → Position history
```

## Risk Management

The bot has several layers of protection built in:

### Trader Exposure Limit
Each trader has a maximum percentage of your total portfolio (cash + positions) they can use. Set globally via `MAX_EXPOSURE_PER_TRADER` or per-trader via `TRADER_EXPOSURE_MAP`.

```env
# Default: 33% per trader
MAX_EXPOSURE_PER_TRADER=0.33

# Override per trader: give more room to your best traders
TRADER_EXPOSURE_MAP=sovereign2013:0.50,xsaghav:0.50,Jargs:0.50
```

Limits are independent — they don't need to add up to 100%. A 50/50/50 split means each trader can use up to half the portfolio, but `CASH_FLOOR` prevents the wallet from going to zero.

```
Portfolio: $400 (wallet $200 + positions $200)

Trader A (50%): max $200 → has $150 → can open $50 more
Trader B (50%): max $200 → has $80  → can open $120 more
Trader C (50%): max $200 → has $0   → can open $200

Trader A wins, positions close → exposure drops → can copy again
```

The limit is based on **total portfolio value** (not just cash), so it doesn't shrink as you open more positions.

### Hedge Detection
Waits 60s before executing a trade. If the trader buys the opposite side within that window, both are cancelled. This prevents copying hedged positions where you'd lose to fees regardless of outcome.

### Proportional Sizing
Small trader bets (noise/testing) get small copies. Large trader bets (high conviction) get larger copies. Your bet mirrors the trader's conviction level, not just a flat amount.

### Auto-Sell at 96¢
Won positions are automatically sold at 96¢+ to recycle capital. No need to wait for market resolution — the bot takes profit and frees up cash for new trades.

### One Copy Per Market
`MAX_COPIES_PER_MARKET=1` prevents the bot from doubling up on the same market when a trader adds to their position in waves.

## Risks

- **Slippage** — 5s scan delay means you get worse prices than the trader
- **Fees** — Polymarket charges 2% (200 bps) per trade
- **Losses** — Traders can lose. Past performance doesn't guarantee future results
- **Liquidity** — Small markets may not have enough liquidity for your orders
- **Binary outcomes** — Positions go to $0 or $1, no stop-loss possible

## Tech Stack

- Python 3.12+
- Flask (dashboard + SSE)
- SQLite with WAL mode
- py-clob-client (Polymarket CLOB API)
- poly-web3 (Builder Relayer)
- WebSocket (real-time prices)
- Chart.js (equity curve)

## License

MIT
