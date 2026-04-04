import os
from dotenv import load_dotenv

load_dotenv()

# AI (Groq - Fallback)
AI_MODEL = "llama-3.1-8b-instant"
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# Z.ai / Zhipu GLM (primäre AI)
ZAI_API_KEY = os.getenv("ZAI_API_KEY", "")
ZAI_MODEL = "glm-5"
ZAI_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"

# Anthropic Claude (3. Fallback)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# Google Gemini (4. Fallback - kostenlos)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash-lite"

# Scanner Settings
SCAN_WALLET_LIMIT = 500          # Wallets vom Leaderboard holen
MIN_PNL = 50                     # Min PNL Filter (keine Verlierer)
AUTO_FOLLOW_COUNT = 2            # Top N Trader automatisch folgen
MAX_AI_ANALYSES = 50             # Max Wallets per Scan mit AI analysieren
TOP_N_REPORT = 10                # Top N Wallets im Report
MIN_VOLUME = 1000                # Min Volume Filter
SCAN_INTERVAL_HOURS = 24         # Täglicher Scan

# Polymarket CLOB API
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY", "")
POLYMARKET_SECRET = os.getenv("POLYMARKET_SECRET", "")
POLYMARKET_PASSPHRASE = os.getenv("POLYMARKET_PASSPHRASE", "")
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_FUNDER = os.getenv("POLYMARKET_FUNDER", "")

# --- Copybot Parameters ---
LIVE_MODE = os.getenv("LIVE_MODE", "true").lower() in ("true", "1", "yes")
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "200"))
MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "5"))
MIN_TRADE_SIZE = float(os.getenv("MIN_TRADE_SIZE", "1.0"))
CASH_FLOOR = float(os.getenv("CASH_FLOOR", "20"))
CASH_RECOVERY = float(os.getenv("CASH_RECOVERY", "6"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "30"))
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.05"))
ENTRY_TRADE_SEC = int(os.getenv("ENTRY_TRADE_SEC", "300"))
COPY_SCAN_INTERVAL = int(os.getenv("COPY_SCAN_INTERVAL", "5"))
BET_SIZE_PCT = float(os.getenv("BET_SIZE_PCT", "0.02"))

# --- RN1 Smart-Filter (High-Frequency Trader Protection) ---
# Nur Trades kopieren wo der Trader mindestens X USD einsetzt (filtert Noise/Hedges)
MIN_TRADER_USD = float(os.getenv("MIN_TRADER_USD", "50"))
# Trades bei extremen Preisen skippen (Trash-Farming bei 1-3c, Hedges bei 95-99c)
MIN_ENTRY_PRICE = float(os.getenv("MIN_ENTRY_PRICE", "0.05"))
MAX_ENTRY_PRICE = float(os.getenv("MAX_ENTRY_PRICE", "0.92"))
# Max Kopien desselben Marktes pro Wallet (verhindert Spam-Kopien)
MAX_COPIES_PER_MARKET = int(os.getenv("MAX_COPIES_PER_MARKET", "2"))
# Hedge-Wait: hold trades for X seconds to detect if trader buys opposite side
# If both sides bought within this window → skip both (hedge detected)
# Set to 0 to disable
HEDGE_WAIT_SECS = int(os.getenv("HEDGE_WAIT_SECS", "120"))
# Comma-separated usernames that need hedge-wait (e.g. "xsaghav,RN1")
HEDGE_WAIT_TRADERS = os.getenv("HEDGE_WAIT_TRADERS", "")

# Followed traders: comma-separated "username:address" pairs
# e.g. "Jargs:0xf164...,xsaghav:0xdbb3..."
FOLLOWED_TRADERS = os.getenv("FOLLOWED_TRADERS", "")

# AI Report
REPORT_INTERVAL_HOURS = int(os.getenv("REPORT_INTERVAL_HOURS", "0"))  # 0 = manual only
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Massive.com Market Data
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY", "")

# Dashboard
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8090"))
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")

# Database
DB_PATH = os.path.join(os.path.dirname(__file__), "database", "scanner.db")

# Logging
LOG_PATH = os.path.join(os.path.dirname(__file__), "logs", "scanner.log")

# Reports
REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")
