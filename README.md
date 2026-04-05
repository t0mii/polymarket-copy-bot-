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

All settings are optional — defaults work out of the box. Only `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`, and `FOLLOWED_TRADERS` are required.

### Core
| Parameter | Default | Description |
|-----------|---------|-------------|
| `LIVE_MODE` | false | false = paper trading, true = real money |
| `STARTING_BALANCE` | 320 | Your deposit amount (for P&L calculation) |
| `COPY_SCAN_INTERVAL` | 5 | Seconds between scans |
| `DASHBOARD_PORT` | 8090 | Web dashboard port |
| `DASHBOARD_SECRET` | changeme | Secret key for follow/unfollow/reset API |

### Position Sizing
| Parameter | Default | Description |
|-----------|---------|-------------|
| `BET_SIZE_PCT` | 0.05 | Base bet = 5% of portfolio (~$15 at $300) |
| `MAX_POSITION_SIZE` | 30 | Hard cap per position |
| `MIN_TRADE_SIZE` | 1.0 | Minimum bet size |
| `RATIO_MIN` | 0.2 | Floor multiplier (small trader bet → small copy) |
| `RATIO_MAX` | 2.0 | Ceiling multiplier (big trader bet → bigger copy) |
| `BET_SIZE_BASIS` | cash | `cash` = size from wallet, `portfolio` = wallet + positions |
| `BET_SIZE_MAP` | | Per-trader base bet override (e.g. `xsaghav:0.08,sovereign2013:0.03`) |
| `DEFAULT_AVG_TRADER_SIZE` | 10.0 | Fallback avg trade size when no trader data |

### Price Signal Multipliers

The bot adjusts bet size based on how far the price is from 50c. Extreme prices (near 0c or 100c) indicate stronger trader conviction.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `PRICE_EDGE_HIGH` | 0.30 | Edge threshold for strong signal (20c or 80c) |
| `PRICE_MULT_HIGH` | 1.50 | Bet multiplier for strong signal |
| `PRICE_EDGE_MED` | 0.15 | Edge threshold for normal signal (35c or 65c) |
| `PRICE_MULT_MED` | 1.00 | Bet multiplier for normal signal |
| `PRICE_MULT_LOW` | 0.60 | Bet multiplier for weak signal (near 50c coinflips) |

```
Price 15c → edge 0.35 → strong signal → bet × 1.5
Price 30c → edge 0.20 → normal signal → bet × 1.0
Price 45c → edge 0.05 → weak signal  → bet × 0.6
```

### Trade Filters
| Parameter | Default | Description |
|-----------|---------|-------------|
| `MIN_TRADER_USD` | 3 | Default min trade size to copy |
| `MIN_TRADER_USD_MAP` | | Per-trader: `name:amount` (e.g. `sovereign2013:500`) |
| `MIN_ENTRY_PRICE` | 0.15 | Skip lottery tickets below 15c |
| `MAX_ENTRY_PRICE` | 0.92 | Skip near-certain bets above 92c |
| `MAX_COPIES_PER_MARKET` | 1 | One copy per market (prevents doubling up) |
| `ENTRY_TRADE_SEC` | 300 | Ignore trades older than 5 minutes |
| `MAX_HOURS_BEFORE_EVENT` | 0 | Queue trades if event > X hours away (0=disabled) |
| `EVENT_WAIT_MIN_CASH` | 0 | Only queue distant events when cash < $X (0=always queue) |
| `MAX_PER_EVENT` | 15 | Max $ per event/game (0=disabled) |
| `NO_REBUY_MINUTES` | 0 | Block re-entry after close (0=disabled) |
| `MAX_SPREAD` | 0.05 | Max bid/ask spread tolerance (5%) |

### Entry Mechanics
| Parameter | Default | Description |
|-----------|---------|-------------|
| `ENTRY_SLIPPAGE` | 0.0 | Added to entry price (e.g. 0.01 = 1c buffer) |
| `MAX_ENTRY_PRICE_CAP` | 0.97 | Hard ceiling after slippage applied |
| `TRADE_SEC_FROM_RESOLVE` | 120 | Stop buying within X seconds of market close |
| `CASH_RESERVE` | 0 | Dollars permanently reserved (never used for betting) |

### Hedge Detection
| Parameter | Default | Description |
|-----------|---------|-------------|
| `HEDGE_WAIT_SECS` | 60 | Default hedge detection window (seconds) |
| `HEDGE_WAIT_TRADERS` | | Per-trader: `name:seconds,name:seconds` |

### Cash Management
| Parameter | Default | Description |
|-----------|---------|-------------|
| `CASH_FLOOR` | 0 | Stop buying below this cash level |
| `CASH_RECOVERY` | 6 | Must recover $X above floor before resuming |
| `SAVE_POINT_STEP` | 1.0 | Floor increases by $X per recovery cycle |
| `MAX_OPEN_POSITIONS` | 100 | Maximum simultaneous positions |
| `MAX_EXPOSURE_PER_TRADER` | 0.33 | Default max % of portfolio per trader |
| `TRADER_EXPOSURE_MAP` | | Per-trader: `name:pct,name:pct` |

### Risk Management

All disabled by default (0 = off). Enable by setting a value > 0.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MAX_DAILY_LOSS` | 0 | Stop trading if daily realized losses exceed $X |
| `MAX_DAILY_TRADES` | 0 | Maximum new trades per calendar day |
| `STOP_LOSS_PCT` | 0 | Auto-sell if position drops by X% (e.g. 0.50 = 50%) |
| `TAKE_PROFIT_PCT` | 0 | Auto-sell if position gains X% (e.g. 1.00 = 100%) |

```env
# Example: conservative risk settings
MAX_DAILY_LOSS=50           # Stop after $50 daily loss
MAX_DAILY_TRADES=20         # Max 20 trades per day
STOP_LOSS_PCT=0.60          # Sell if position drops 60%
TAKE_PROFIT_PCT=2.00        # Sell if position gains 200%
```

### Pending Buy Queue

Queue trades below a price threshold and wait for confirmation before executing. Disabled by default.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `BUY_THRESHOLD` | 0.0 | Queue trades below this price (0=disabled) |
| `PENDING_BUY_MIN_SECS` | 210 | Min wait before firing queued trade |
| `PENDING_BUY_MAX_SECS` | 900 | Max wait before discarding |

### Feature Toggles
| Parameter | Default | Description |
|-----------|---------|-------------|
| `COPY_SELLS` | true | Copy sell signals from traders (false = hold until resolve) |
| `POSITION_DIFF_ENABLED` | true | Enable position-diff fallback scan |
| `IDLE_REPLACE_ENABLED` | false | Auto-replace inactive traders from leaderboard |
| `IDLE_TRIGGER_SECS` | 1200 | Seconds of inactivity before replacement (20 min) |
| `IDLE_REPLACE_COOLDOWN` | 1800 | Cooldown after replacing a trader (30 min) |

### Scan Throttling
| Parameter | Default | Description |
|-----------|---------|-------------|
| `MAX_TRADES_PER_SCAN` | 3 | Max new trades per 5-second scan cycle |
| `RECENT_TRADES_LIMIT` | 50 | Trades to fetch per wallet per scan |

### Circuit Breaker
| Parameter | Default | Description |
|-----------|---------|-------------|
| `CB_THRESHOLD` | 8 | Consecutive API failures to trip breaker |
| `CB_PAUSE_SECS` | 60 | Seconds to pause when breaker trips |

### API Tuning
| Parameter | Default | Description |
|-----------|---------|-------------|
| `API_TIMEOUT` | 10 | GET request timeout (seconds) |
| `API_MAX_RETRIES` | 3 | Retry attempts per failed request |
| `LIVE_PRICE_MIN` | 0.05 | Min live price to accept (below = use trader price) |
| `LIVE_PRICE_MAX_DEVIATION` | 0.50 | Max % deviation from trader price to accept live price |

### Fill Verification
| Parameter | Default | Description |
|-----------|---------|-------------|
| `FILL_VERIFY_DELAY_SECS` | 2 | Seconds to wait after buy before checking fill amount |
| `MIN_FILL_AMOUNT` | 0.10 | Min fill amount ($) to count as valid |

### Position Tracking
| Parameter | Default | Description |
|-----------|---------|-------------|
| `MIN_POSITION_SIZE_FILTER` | 0.50 | Min position size to include in scans |
| `MISS_COUNT_TO_CLOSE` | 180 | Consecutive scan misses before closing stale position |

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

### Per-Trader Bet Sizing
Give your best-performing traders bigger bets via `BET_SIZE_MAP`. The full sizing formula:

```
final_size = base × price_multiplier × conviction_ratio

base = wallet × BET_SIZE_MAP[trader]   (or BET_SIZE_PCT if no override)
price_multiplier = 1.5x (strong), 1.0x (normal), 0.6x (weak signal)
conviction_ratio = trader's bet / trader's average (clamped RATIO_MIN–RATIO_MAX)
```

```env
# Example: trust xsaghav most, sovereign least
BET_SIZE_MAP=xsaghav:0.08,sovereign2013:0.03,Jargs:0.05

# At $300 wallet, xsaghav bets 2x his average on a 20c market:
# base=$24 × 1.5x(strong) × 2.0x(conviction) = $72 → capped to MAX_POSITION_SIZE=$30
```

### Auto-Sell at 96¢
Won positions are automatically sold at 96¢+ to recycle capital. No need to wait for market resolution — the bot takes profit and frees up cash for new trades.

### One Copy Per Market
`MAX_COPIES_PER_MARKET=1` prevents the bot from doubling up on the same market when a trader adds to their position in waves.

### Max Per Event
Limits total $ invested per event/game. A trader might place 5 different bets on the same NBA game (Spread -17.5, Spread -18.5, O/U 245.5, O/U 246.5, O/U 248.5). All share the same underlying thesis — if the game goes wrong, all 5 lose simultaneously.

```env
MAX_PER_EVENT=15   # Max $15 per game — first bet copied, rest blocked
MAX_PER_EVENT=0    # Disabled — copy all bets on same game
```

Real example: sovereign2013 placed 5 bets on Wizards vs Heat ($39 total). All lost. With `MAX_PER_EVENT=15`, only 1 bet ($10) would have been copied — saving $29.

### No-Rebuy (optional)
After selling a position, optionally block re-entering the same market for X minutes.

```env
NO_REBUY_MINUTES=60   # Block re-entry for 1 hour after close
NO_REBUY_MINUTES=0    # Disabled (default) — allow re-entry
```

### Event Timing Filter (optional)
When enabled, trades on events starting more than X hours from now are **queued** instead of bought immediately. When the event enters the time window, the trade is executed with fresh pricing. This prevents capital being locked in positions hours before games start.

```env
# Disabled (default) — copies immediately when trader buys
MAX_HOURS_BEFORE_EVENT=0

# Always wait: queue if event > 3 hours away, buy when < 3 hours
MAX_HOURS_BEFORE_EVENT=3
EVENT_WAIT_MIN_CASH=0

# Wait only when low on cash: queue if event > 3h AND cash < $100
MAX_HOURS_BEFORE_EVENT=3
EVENT_WAIT_MIN_CASH=100
```

| Scenario | Cash $200, Event in 5h | Cash $50, Event in 5h | Event in 2h |
|----------|----------------------|---------------------|-------------|
| `MIN_CASH=0` | Queue | Queue | Buy now |
| `MIN_CASH=100` | Buy now | Queue | Buy now |

Works for all sports (NBA, MLB, NHL, NCAA). For esports where the Gamma API doesn't have start times, the check is skipped and trades copy normally.

## Risks

- **Slippage** — 5s scan delay means you get worse prices than the trader
- **Fees** — Polymarket charges 2% (200 bps) per trade
- **Losses** — Traders can lose. Past performance doesn't guarantee future results
- **Liquidity** — Small markets may not have enough liquidity for your orders
- **Binary outcomes** — Positions go to $0 or $1. Optional stop-loss via `STOP_LOSS_PCT`

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
