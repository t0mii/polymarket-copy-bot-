import os
from dotenv import load_dotenv

load_dotenv()

# Polymarket CLOB API
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_FUNDER = os.getenv("POLYMARKET_FUNDER", "")

# Builder API (auto-redeem)
BUILDER_KEY = os.getenv("BUILDER_KEY", "")
BUILDER_SECRET = os.getenv("BUILDER_SECRET", "")
BUILDER_PASSPHRASE = os.getenv("BUILDER_PASSPHRASE", "")

# --- Copybot Core ---
LIVE_MODE = os.getenv("LIVE_MODE", "true").lower() in ("true", "1", "yes")
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "320"))
COPY_SCAN_INTERVAL = int(os.getenv("COPY_SCAN_INTERVAL", "5"))

# --- Position Sizing ---
BET_SIZE_PCT = float(os.getenv("BET_SIZE_PCT", "0.05"))
MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "30"))
MIN_TRADE_SIZE = float(os.getenv("MIN_TRADE_SIZE", "1.0"))
RATIO_MIN = float(os.getenv("RATIO_MIN", "0.2"))
RATIO_MAX = float(os.getenv("RATIO_MAX", "3.0"))
# Sizing basis: "cash" = wallet balance only, "portfolio" = wallet + active positions
BET_SIZE_BASIS = os.getenv("BET_SIZE_BASIS", "cash").lower()
# Per-trader bet size override: "name:pct,name:pct" (overrides BET_SIZE_PCT per trader)
BET_SIZE_MAP = os.getenv("BET_SIZE_MAP", "")

# --- Cash Management ---
CASH_FLOOR = float(os.getenv("CASH_FLOOR", "0"))
CASH_RECOVERY = float(os.getenv("CASH_RECOVERY", "6"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "100"))
MAX_EXPOSURE_PER_TRADER = float(os.getenv("MAX_EXPOSURE_PER_TRADER", "0.33"))  # Default max % per trader
# Per-trader override: "name:pct,name:pct" e.g. "sovereign2013:0.40,xsaghav:0.30"
TRADER_EXPOSURE_MAP = os.getenv("TRADER_EXPOSURE_MAP", "")

# --- Trade Filters ---
MIN_TRADER_USD = float(os.getenv("MIN_TRADER_USD", "3"))
# Per-trader override: "name:amount,name:amount" (overrides MIN_TRADER_USD per trader)
MIN_TRADER_USD_MAP = os.getenv("MIN_TRADER_USD_MAP", "")
MIN_ENTRY_PRICE = float(os.getenv("MIN_ENTRY_PRICE", "0.15"))
MAX_ENTRY_PRICE = float(os.getenv("MAX_ENTRY_PRICE", "0.92"))
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.05"))
MAX_COPIES_PER_MARKET = int(os.getenv("MAX_COPIES_PER_MARKET", "1"))
ENTRY_TRADE_SEC = int(os.getenv("ENTRY_TRADE_SEC", "300"))
MAX_HOURS_BEFORE_EVENT = float(os.getenv("MAX_HOURS_BEFORE_EVENT", "0"))  # Only buy X hours before event starts (0=disabled)
EVENT_WAIT_MIN_CASH = float(os.getenv("EVENT_WAIT_MIN_CASH", "0"))  # Only queue distant events when cash below $X (0=always queue)
NO_REBUY_MINUTES = int(os.getenv("NO_REBUY_MINUTES", "0"))  # Don't re-enter closed markets for X min (0=disabled)
MAX_PER_EVENT = float(os.getenv("MAX_PER_EVENT", "15"))  # Max $ invested per event/game (0=disabled)

# --- Position Sizing: Price Signal ---
PRICE_EDGE_HIGH = float(os.getenv("PRICE_EDGE_HIGH", "0.30"))
PRICE_MULT_HIGH = float(os.getenv("PRICE_MULT_HIGH", "1.50"))
PRICE_EDGE_MED = float(os.getenv("PRICE_EDGE_MED", "0.15"))
PRICE_MULT_MED = float(os.getenv("PRICE_MULT_MED", "1.00"))
PRICE_MULT_LOW = float(os.getenv("PRICE_MULT_LOW", "0.60"))
DEFAULT_AVG_TRADER_SIZE = float(os.getenv("DEFAULT_AVG_TRADER_SIZE", "10.0"))

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

# --- Live Price Validation ---
LIVE_PRICE_MIN = float(os.getenv("LIVE_PRICE_MIN", "0.05"))
LIVE_PRICE_MAX_DEVIATION = float(os.getenv("LIVE_PRICE_MAX_DEVIATION", "0.50"))

# --- Fill Verification ---
FILL_VERIFY_DELAY_SECS = int(os.getenv("FILL_VERIFY_DELAY_SECS", "2"))
MIN_FILL_AMOUNT = float(os.getenv("MIN_FILL_AMOUNT", "0.10"))

# --- Position Tracking ---
MIN_POSITION_SIZE_FILTER = float(os.getenv("MIN_POSITION_SIZE_FILTER", "0.50"))
MISS_COUNT_TO_CLOSE = int(os.getenv("MISS_COUNT_TO_CLOSE", "180"))

# --- Idle Trader Replacement ---
IDLE_REPLACE_ENABLED = os.getenv("IDLE_REPLACE_ENABLED", "false").lower() in ("true", "1", "yes")
IDLE_TRIGGER_SECS = int(os.getenv("IDLE_TRIGGER_SECS", "1200"))
IDLE_REPLACE_COOLDOWN = int(os.getenv("IDLE_REPLACE_COOLDOWN", "1800"))

# --- Risk Management ---
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "0"))
MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", "0"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0"))

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
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
ZAI_API_KEY = os.getenv("ZAI_API_KEY", "")
ZAI_MODEL = "glm-5"
ZAI_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash-lite"
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY", "")
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY", "")
POLYMARKET_SECRET = os.getenv("POLYMARKET_SECRET", "")
POLYMARKET_PASSPHRASE = os.getenv("POLYMARKET_PASSPHRASE", "")

# Database
DB_PATH = os.path.join(os.path.dirname(__file__), "database", "scanner.db")

# Logging
LOG_PATH = os.path.join(os.path.dirname(__file__), "logs", "scanner.log")

# Reports
REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")
