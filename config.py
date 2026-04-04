import os
from dotenv import load_dotenv

load_dotenv()

# Scanner Settings (legacy, mostly unused by copybot)
SCAN_WALLET_LIMIT = 500
MIN_PNL = 50
AUTO_FOLLOW_COUNT = 2
MAX_AI_ANALYSES = 50
TOP_N_REPORT = 10
MIN_VOLUME = 1000
SCAN_INTERVAL_HOURS = 24

# AI Models (legacy)
AI_MODEL = "llama-3.1-8b-instant"
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
ZAI_API_KEY = os.getenv("ZAI_API_KEY", "")
ZAI_MODEL = "glm-5"
ZAI_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash-lite"

# Polymarket CLOB API
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY", "")
POLYMARKET_SECRET = os.getenv("POLYMARKET_SECRET", "")
POLYMARKET_PASSPHRASE = os.getenv("POLYMARKET_PASSPHRASE", "")
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_FUNDER = os.getenv("POLYMARKET_FUNDER", "")

# --- Copybot Core ---
LIVE_MODE = os.getenv("LIVE_MODE", "true").lower() in ("true", "1", "yes")
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "320"))
COPY_SCAN_INTERVAL = int(os.getenv("COPY_SCAN_INTERVAL", "5"))

# --- Position Sizing ---
BET_SIZE_PCT = float(os.getenv("BET_SIZE_PCT", "0.05"))       # Base % of portfolio per trade
MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "30")) # Max $ per single position
MIN_TRADE_SIZE = float(os.getenv("MIN_TRADE_SIZE", "1.0"))     # Min $ to place a trade
RATIO_MIN = float(os.getenv("RATIO_MIN", "0.2"))               # Min trader conviction multiplier
RATIO_MAX = float(os.getenv("RATIO_MAX", "3.0"))               # Max trader conviction multiplier

# --- Cash Management ---
CASH_FLOOR = float(os.getenv("CASH_FLOOR", "0"))
CASH_RECOVERY = float(os.getenv("CASH_RECOVERY", "6"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "100"))

# --- Trade Filters ---
MIN_TRADER_USD = float(os.getenv("MIN_TRADER_USD", "3"))        # Min trader trade size to copy
MIN_ENTRY_PRICE = float(os.getenv("MIN_ENTRY_PRICE", "0.05"))   # Skip trash farming below this
MAX_ENTRY_PRICE = float(os.getenv("MAX_ENTRY_PRICE", "0.92"))   # Skip hedges above this
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.05"))
MAX_COPIES_PER_MARKET = int(os.getenv("MAX_COPIES_PER_MARKET", "2"))
ENTRY_TRADE_SEC = int(os.getenv("ENTRY_TRADE_SEC", "300"))      # Max age of trade to copy (seconds)

# --- Hedge Detection ---
HEDGE_WAIT_SECS = int(os.getenv("HEDGE_WAIT_SECS", "60"))      # Default wait (if not per-trader)
HEDGE_WAIT_TRADERS = os.getenv("HEDGE_WAIT_TRADERS", "")        # Per-trader: "name:secs,name:secs"

# --- Followed Traders ---
FOLLOWED_TRADERS = os.getenv("FOLLOWED_TRADERS", "")            # "name:address,name:address"

# --- Dashboard ---
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8090"))
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET", "changeme")

# --- Builder API (auto-redeem) ---
BUILDER_KEY = os.getenv("BUILDER_KEY", "")
BUILDER_SECRET = os.getenv("BUILDER_SECRET", "")
BUILDER_PASSPHRASE = os.getenv("BUILDER_PASSPHRASE", "")

# Massive.com Market Data
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY", "")

# Database
DB_PATH = os.path.join(os.path.dirname(__file__), "database", "scanner.db")

# Logging
LOG_PATH = os.path.join(os.path.dirname(__file__), "logs", "scanner.log")

# Reports
REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")
