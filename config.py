# Version
BOT_VERSION = "2.0.0"
import os
import sys
from dotenv import load_dotenv

# Load secrets first, then settings — NO legacy .env fallback
_dir = os.path.dirname(os.path.abspath(__file__))
_secrets_path = os.path.join(_dir, "secrets.env")
_settings_path = os.path.join(_dir, "settings.env")

_missing = []
if not os.path.exists(_secrets_path):
    _missing.append("secrets.env")
if not os.path.exists(_settings_path):
    _missing.append("settings.env")
if _missing:
    print("=" * 60)
    print("FEHLER: Fehlende Config-Dateien: %s" % ", ".join(_missing))
    print()
    print("Erstelle sie aus den Vorlagen:")
    for f in _missing:
        print("  cp %s.example.env %s" % (f.replace(".env", ""), f))
    print()
    print("secrets.env  = Private Keys, API-Credentials (NICHT committen!)")
    print("settings.env = Bot-Einstellungen (Trader, Groessen, Filter)")
    print("=" * 60)
    sys.exit(1)

load_dotenv(_secrets_path)
load_dotenv(_settings_path)

# Polymarket CLOB API
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_FUNDER = os.getenv("POLYMARKET_FUNDER", "")

# Builder API (auto-redeem)
BUILDER_KEY = os.getenv("BUILDER_KEY", "")
BUILDER_SECRET = os.getenv("BUILDER_SECRET", "")
BUILDER_PASSPHRASE = os.getenv("BUILDER_PASSPHRASE", "")

# --- Copybot Core ---
LIVE_MODE = os.getenv("LIVE_MODE", "true").lower() in ("true", "1", "yes")
ML_ENABLED = os.getenv("ML_ENABLED", "true").lower() in ("true", "1", "yes")
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "100"))
COPY_SCAN_INTERVAL = int(os.getenv("COPY_SCAN_INTERVAL", "5"))

# --- Position Sizing ---
BET_SIZE_PCT = float(os.getenv("BET_SIZE_PCT", "0.05"))
MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "4"))
MIN_TRADE_SIZE = float(os.getenv("MIN_TRADE_SIZE", "1.0"))
RATIO_MIN = float(os.getenv("RATIO_MIN", "0.2"))
RATIO_MAX = float(os.getenv("RATIO_MAX", "1.0"))
# Sizing basis: "cash" = wallet balance only, "portfolio" = wallet + active positions
BET_SIZE_BASIS = os.getenv("BET_SIZE_BASIS", "cash").lower()
# Per-trader bet size override: "name:pct,name:pct" (overrides BET_SIZE_PCT per trader)
BET_SIZE_MAP = os.getenv("BET_SIZE_MAP", "")

# --- Cash Management ---
CASH_FLOOR = float(os.getenv("CASH_FLOOR", "10"))
CASH_RECOVERY = float(os.getenv("CASH_RECOVERY", "6"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "100"))
MAX_EXPOSURE_PER_TRADER = float(os.getenv("MAX_EXPOSURE_PER_TRADER", "0.33"))  # Default max % per trader
# Per-trader override: "name:pct,name:pct" e.g. "sovereign2013:0.40,xsaghav:0.30"
TRADER_EXPOSURE_MAP = os.getenv("TRADER_EXPOSURE_MAP", "")

# --- Trade Filters ---
MIN_TRADER_USD = float(os.getenv("MIN_TRADER_USD", "3"))
# Per-trader override: "name:amount,name:amount" (overrides MIN_TRADER_USD per trader)
MIN_TRADER_USD_MAP = os.getenv("MIN_TRADER_USD_MAP", "")
MIN_ENTRY_PRICE = float(os.getenv("MIN_ENTRY_PRICE", "0.42"))
MIN_ENTRY_PRICE_MAP = os.getenv("MIN_ENTRY_PRICE_MAP", "")
MAX_ENTRY_PRICE = float(os.getenv("MAX_ENTRY_PRICE", "0.92"))
MAX_ENTRY_PRICE_MAP = os.getenv("MAX_ENTRY_PRICE_MAP", "")
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.05"))
MAX_FEE_BPS = int(os.getenv("MAX_FEE_BPS", "0"))  # Max fee in bps (0=disabled, 500=skip >5% fee). Achtung: Esports hat 10% Fee!
MAX_COPIES_PER_MARKET = int(os.getenv("MAX_COPIES_PER_MARKET", "1"))
MAX_COPIES_PER_MARKET_MAP = os.getenv("MAX_COPIES_PER_MARKET_MAP", "")
# Per-trader category blacklist: "trader:cat1|cat2,trader:cat1" (categories: NBA,MLB,NHL,NFL,Tennis,Soccer,CS,LoL,Valorant,Dota,Geopolitics,Cricket)
CATEGORY_BLACKLIST_MAP = os.getenv("CATEGORY_BLACKLIST_MAP", "")
# Min conviction ratio: only copy trades where trader bets >= X times their average (0=disabled)
MIN_CONVICTION_RATIO = float(os.getenv("MIN_CONVICTION_RATIO", "0"))
# Per-trader override: "name:ratio,name:ratio" (e.g. "sovereign2013:1.5" = only 1.5x+ conviction)
MIN_CONVICTION_RATIO_MAP = os.getenv("MIN_CONVICTION_RATIO_MAP", "")
ENTRY_TRADE_SEC = int(os.getenv("ENTRY_TRADE_SEC", "300"))
MAX_HOURS_BEFORE_EVENT = float(os.getenv("MAX_HOURS_BEFORE_EVENT", "0"))  # Only buy X hours before event starts (0=disabled)
MAX_MARKET_HOURS = float(os.getenv("MAX_MARKET_HOURS", "0"))  # Only buy markets resolving within X hours (0=disabled)
EVENT_WAIT_MIN_CASH = float(os.getenv("EVENT_WAIT_MIN_CASH", "0"))  # Only queue distant events when cash below $X (0=always queue)
# Max price drift allowed when executing queued trades (per price range)
QUEUE_DRIFT_LOTTERY = float(os.getenv("QUEUE_DRIFT_LOTTERY", "0.30"))   # <20c: 30%
QUEUE_DRIFT_UNDERDOG = float(os.getenv("QUEUE_DRIFT_UNDERDOG", "0.40")) # 20-40c: 40%
QUEUE_DRIFT_COINFLIP = float(os.getenv("QUEUE_DRIFT_COINFLIP", "0.03")) # 40-60c: 3%
QUEUE_DRIFT_FAVORITE = float(os.getenv("QUEUE_DRIFT_FAVORITE", "0.05")) # 60-85c: 5%
NO_REBUY_MINUTES = int(os.getenv("NO_REBUY_MINUTES", "0"))  # Don't re-enter closed markets for X min (0=disabled)
MAX_PER_EVENT = float(os.getenv("MAX_PER_EVENT", "15"))  # Max $ invested per event/game (0=disabled)
MAX_PER_MATCH = float(os.getenv("MAX_PER_MATCH", "15"))  # Max $ across related markets (Map 1 + Map 2 + BO3 = 1 match)

# --- Position Sizing: Price Signal ---
PRICE_EDGE_HIGH = float(os.getenv("PRICE_EDGE_HIGH", "0.30"))
PRICE_MULT_HIGH = float(os.getenv("PRICE_MULT_HIGH", "1.50"))
PRICE_EDGE_MED = float(os.getenv("PRICE_EDGE_MED", "0.15"))
PRICE_MULT_MED = float(os.getenv("PRICE_MULT_MED", "1.00"))
PRICE_MULT_LOW = float(os.getenv("PRICE_MULT_LOW", "0.60"))
DEFAULT_AVG_TRADER_SIZE = float(os.getenv("DEFAULT_AVG_TRADER_SIZE", "10.0"))
AVG_TRADER_SIZE_MAP = os.getenv("AVG_TRADER_SIZE_MAP", "")

# --- Entry Mechanics ---
CASH_RESERVE = float(os.getenv("CASH_RESERVE", "0"))
ENTRY_SLIPPAGE = float(os.getenv("ENTRY_SLIPPAGE", "0.0"))
MAX_ENTRY_PRICE_CAP = float(os.getenv("MAX_ENTRY_PRICE_CAP", "0.97"))
TRADE_SEC_FROM_RESOLVE = int(os.getenv("TRADE_SEC_FROM_RESOLVE", "120"))

# --- Pending Buy Queue ---
BUY_THRESHOLD = float(os.getenv("BUY_THRESHOLD", "0.0"))
PENDING_BUY_MIN_SECS = int(os.getenv("PENDING_BUY_MIN_SECS", "210"))
PENDING_BUY_MAX_SECS = int(os.getenv("PENDING_BUY_MAX_SECS", "900"))

# --- Scan Throttling ---
MAX_TRADES_PER_SCAN = int(os.getenv("MAX_TRADES_PER_SCAN", "3"))
RECENT_TRADES_LIMIT = int(os.getenv("RECENT_TRADES_LIMIT", "50"))

# --- Cash Floor Recovery ---
SAVE_POINT_STEP = float(os.getenv("SAVE_POINT_STEP", "1.0"))

# --- Circuit Breaker ---
CB_THRESHOLD = int(os.getenv("CB_THRESHOLD", "8"))
CB_PAUSE_SECS = int(os.getenv("CB_PAUSE_SECS", "60"))

# --- API Tuning ---
API_TIMEOUT = int(os.getenv("API_TIMEOUT", "10"))
API_MAX_RETRIES = int(os.getenv("API_MAX_RETRIES", "3"))
GAMMA_API_TIMEOUT = int(os.getenv("GAMMA_API_TIMEOUT", "5"))
DATA_API_TIMEOUT = int(os.getenv("DATA_API_TIMEOUT", "15"))

# --- Live Price Validation ---
LIVE_PRICE_MIN = float(os.getenv("LIVE_PRICE_MIN", "0.05"))
LIVE_PRICE_MAX_DEVIATION = float(os.getenv("LIVE_PRICE_MAX_DEVIATION", "0.50"))

# --- Order Execution ---
# Slippage levels for buy/sell retry (comma-separated, e.g. "0.05,0.08,0.12")
BUY_SLIPPAGE_LEVELS = os.getenv("BUY_SLIPPAGE_LEVELS", "0.05,0.08,0.12")
SELL_SLIPPAGE_LEVELS = os.getenv("SELL_SLIPPAGE_LEVELS", "0.01,0.03,0.06")
DELAYED_BUY_VERIFY_SECS = int(os.getenv("DELAYED_BUY_VERIFY_SECS", "8"))
DELAYED_SELL_VERIFY_SECS = int(os.getenv("DELAYED_SELL_VERIFY_SECS", "6"))
SELL_VERIFY_THRESHOLD = float(os.getenv("SELL_VERIFY_THRESHOLD", "0.05"))  # 5% = require 95%+ shares sold

# --- Fill Verification ---
FILL_VERIFY_DELAY_SECS = int(os.getenv("FILL_VERIFY_DELAY_SECS", "2"))
MIN_FILL_AMOUNT = float(os.getenv("MIN_FILL_AMOUNT", "0.10"))

# --- Position Tracking ---
MIN_POSITION_SIZE_FILTER = float(os.getenv("MIN_POSITION_SIZE_FILTER", "0.50"))
MISS_COUNT_TO_CLOSE = int(os.getenv("MISS_COUNT_TO_CLOSE", "180"))
EVENT_WAIT_MAX_SECS = int(os.getenv("EVENT_WAIT_MAX_SECS", "14400"))
RECENTLY_CLOSED_SECS = int(os.getenv("RECENTLY_CLOSED_SECS", "600"))

# --- WebSocket ---
WS_RECONNECT_SECS = int(os.getenv("WS_RECONNECT_SECS", "10"))

# --- Idle Trader Replacement ---
IDLE_REPLACE_ENABLED = os.getenv("IDLE_REPLACE_ENABLED", "false").lower() in ("true", "1", "yes")
IDLE_TRIGGER_SECS = int(os.getenv("IDLE_TRIGGER_SECS", "1200"))
IDLE_REPLACE_COOLDOWN = int(os.getenv("IDLE_REPLACE_COOLDOWN", "1800"))

# --- Risk Management ---
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "0"))
MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", "0"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.25"))
STOP_LOSS_MAP = os.getenv("STOP_LOSS_MAP", "")
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "2.0"))
TAKE_PROFIT_MAP = os.getenv("TAKE_PROFIT_MAP", "")

# --- Auto-Sell / Auto-Close Thresholds ---
AUTO_SELL_PRICE = float(os.getenv("AUTO_SELL_PRICE", "0.96"))  # Sell won positions above this price
AUTO_CLOSE_WON_PRICE = float(os.getenv("AUTO_CLOSE_WON_PRICE", "0.99"))  # Mark as won above this
AUTO_CLOSE_LOST_PRICE = float(os.getenv("AUTO_CLOSE_LOST_PRICE", "0.01"))  # Mark as lost below this

# --- Feature Toggles ---
COPY_SELLS = os.getenv("COPY_SELLS", "true").lower() in ("true", "1", "yes")
POSITION_DIFF_ENABLED = os.getenv("POSITION_DIFF_ENABLED", "true").lower() in ("true", "1", "yes")

# --- Hedge Detection ---
HEDGE_WAIT_SECS = int(os.getenv("HEDGE_WAIT_SECS", "60"))
HEDGE_WAIT_TRADERS = os.getenv("HEDGE_WAIT_TRADERS", "")

# --- Followed Traders ---
FOLLOWED_TRADERS = os.getenv("FOLLOWED_TRADERS", "")

# --- Dashboard ---
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8090"))
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET", "changeme")

# --- Legacy (wallet scanner, disabled in copybot mode) ---
SCAN_WALLET_LIMIT = 500
MIN_PNL = 50
AUTO_FOLLOW_COUNT = 2
MAX_AI_ANALYSES = 50
TOP_N_REPORT = 10
MIN_VOLUME = 1000
SCAN_INTERVAL_HOURS = 24
AI_MODEL = "llama-3.1-8b-instant"
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
ZAI_API_KEY = os.getenv("ZAI_API_KEY", "")
ZAI_MODEL = "glm-5"
ZAI_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash-lite"
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY", "")
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY", "")

# PandaScore API (esports livestream links)
PANDASCORE_API_KEY = os.getenv("PANDASCORE_API_KEY", "")
POLYMARKET_SECRET = os.getenv("POLYMARKET_SECRET", "")
POLYMARKET_PASSPHRASE = os.getenv("POLYMARKET_PASSPHRASE", "")

# Database
DB_PATH = os.path.join(os.path.dirname(__file__), "database", "scanner.db")

# Logging
LOG_PATH = os.path.join(os.path.dirname(__file__), "logs", "scanner.log")

# Reports
REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")

# --- Trailing Stop ---
# Wenn eine Position im Plus war und wieder auf Entry zurueckfaellt → verkaufen
# TRAILING_STOP_MARGIN: Puffer unter Entry (z.B. 0.02 = verkaufe 2c unter Entry)
TRAILING_STOP_ENABLED = os.getenv('TRAILING_STOP_ENABLED', 'true').lower() in ('true', '1', 'yes', 'on')
TRAILING_STOP_MARGIN = float(os.getenv('TRAILING_STOP_MARGIN', '0.10'))
# Min peak gain before trailing activates (e.g. 0.03 = price must have been 3c above entry)
TRAILING_STOP_ACTIVATE = float(os.getenv('TRAILING_STOP_ACTIVATE', '0.20'))

# --- Zero-Risk Category Filter ---
# Block underdog copies in esports categories where markets frequently
# resolve to 0 (not just lose, but total stake loss). Observed 2026-04-13:
# KING7777777 bought both maps of a CS match at 0.266 and both resolved
# to 0 (#3128 + #3129, combined -$4.62). Esports maps are "bin-or-bust"
# — unlike sports spreads where the loser still has meaningful residual
# value, a lost CS map is worth 0 cents.
ZERO_RISK_CATEGORIES = os.getenv("ZERO_RISK_CATEGORIES", "cs,lol,valorant,dota")
ZERO_RISK_MIN_PRICE = float(os.getenv("ZERO_RISK_MIN_PRICE", "0.40"))

AUTONOMOUS_PAPER_MODE = os.getenv("AUTONOMOUS_PAPER_MODE", "true").lower() in ("true", "1", "yes")
MAX_RESOLVE_HOURS = int(os.getenv("MAX_RESOLVE_HOURS", "24"))

# --- Auto-Discovery Promotion Gate ---
# Previously auto_discovery.py automatically called add_followed_wallet() on
# any candidate meeting WR + PnL thresholds, causing unapproved whale wallets
# to be silently auto-followed. Now gated: default false, user must explicitly
# enable OR add the wallet manually via dashboard/settings.
AUTO_DISCOVERY_AUTO_PROMOTE = os.getenv('AUTO_DISCOVERY_AUTO_PROMOTE', 'false').lower() in ('true', '1', 'yes')

# --- Performance Since ---
# ISO timestamp. When set, all trader/category performance aggregations
# (get_trader_rolling_pnl, brain._classify_losses category WR, trade_scorer
# category WR) exclude trades with closed_at < this value. Use this to
# reset the "performance counter" after a settings regime change so brain
# doesn't re-apply blocks based on stale pre-regime data.
# Empty string = no filter (backward compat).
PERFORMANCE_SINCE = os.getenv('PERFORMANCE_SINCE', '').strip()