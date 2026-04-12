"""
Copy-Trading Module - LIVE MODE
Kopiert die Trades von gefolgten Wallets mit echtem Geld auf Polymarket.
"""
import logging

import requests

import config
from database import db
import time as _time

from bot.wallet_scanner import (
    fetch_wallet_positions, fetch_wallet_closed_positions,
    fetch_wallet_recent_trades, DATA_API,
)
from bot.ws_price_tracker import price_tracker
from bot.order_executor import buy_shares, sell_shares, test_connection
from bot.trade_scorer import score as score_trade

logger = logging.getLogger(__name__)

# --- Alle Trading-Parameter aus config.py (einstellbar via .env) ---
LIVE_MODE = config.LIVE_MODE
STARTING_BALANCE = config.STARTING_BALANCE
MAX_POSITION_SIZE = config.MAX_POSITION_SIZE
MIN_TRADE_SIZE = config.MIN_TRADE_SIZE
MAX_SPREAD = config.MAX_SPREAD
ENTRY_TRADE_SEC = config.ENTRY_TRADE_SEC
MAX_OPEN_POSITIONS = config.MAX_OPEN_POSITIONS
BET_SIZE_PCT = config.BET_SIZE_PCT

# Alle weiteren Parameter aus config.py (einstellbar via .env)
CASH_RESERVE = config.CASH_RESERVE
ENTRY_SLIPPAGE = config.ENTRY_SLIPPAGE
TRADE_SEC_FROM_RESOLVE = config.TRADE_SEC_FROM_RESOLVE
IDLE_TRIGGER_SECS = config.IDLE_TRIGGER_SECS
BUY_THRESHOLD = config.BUY_THRESHOLD
PENDING_BUY_MIN_SECS = config.PENDING_BUY_MIN_SECS
PENDING_BUY_MAX_SECS = config.PENDING_BUY_MAX_SECS
MAX_TRADES_PER_SCAN = config.MAX_TRADES_PER_SCAN

def _parse_float_map(raw: str, name: str) -> dict[str, float]:
    """Parse 'key:value,key:value' config strings safely."""
    result: dict[str, float] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" in entry:
            parts = entry.split(":", 1)
            try:
                result[parts[0].strip().lower()] = float(parts[1].strip())
            except ValueError:
                logger.warning("Config parse error in %s: '%s' — skipping", name, entry)
    return result

# Per-trader maps — refreshed from settings.env on each scan via _reload_maps()
_EXPOSURE_MAP = _parse_float_map(config.TRADER_EXPOSURE_MAP, "TRADER_EXPOSURE_MAP")
_MIN_TRADER_USD_MAP = _parse_float_map(config.MIN_TRADER_USD_MAP, "MIN_TRADER_USD_MAP")

# Pending Buy Queue (in-memory: condition_id → {trade_data, queued_at})
_pending_buys: dict = {}

# Idle-Replace Cooldown: verhindert Loop (address → letzter Replace-Zeitpunkt)
_idle_replaced_at: dict = {}

# Hedge-Detection Queue: holds trades for 120s to check if trader buys opposite side
# Key: event_slug or market group → {sides: {side: trade_data}, queued_at: timestamp}
_hedge_queue: dict = {}  # event_slug → {sides: {side: trade_data}, queued_at: ts, address: addr}

# Event-Wait Queue: trades queued because event starts too far in the future
# Key: condition_id → {trade_data, event_start_ts, queued_at}
_event_wait_queue: dict = {}

# Circuit Breaker: nach N aufeinanderfolgenden API-Fehlern → X Sekunden Pause
_CB_THRESHOLD = config.CB_THRESHOLD
_CB_PAUSE_SECS = config.CB_PAUSE_SECS
_cb_failures = 0
_cb_open_until = 0.0
_cb_lock = __import__("threading").Lock()

# Buy Lock: prevents race conditions between concurrent buy attempts
_buy_lock = __import__("threading").Lock()


def _log_block(trader: str, question: str, cid: str, side: str,
               price: float, reason: str, detail: str = "", path: str = "",
               asset: str = "", category: str = ""):
    """Log a blocked trade to the database for AI analysis."""
    try:
        cat = category or _detect_category(question)
        db.log_blocked_trade(trader, question, cid, side, price, reason, detail, path,
                             asset=asset, category=cat)
    except Exception:
        pass  # never let logging break the bot


# --- P&L helpers: use actual fill data when available, fallback to planned ---

def _get_entry_price(trade: dict) -> float:
    """Best available entry price (actual > planned)."""
    return trade.get("actual_entry_price") or trade.get("entry_price") or 0

def _get_size(trade: dict) -> float:
    """Best available investment size (actual > planned)."""
    return trade.get("actual_size") or trade.get("size") or 0

def _calc_pnl(trade: dict, close_price: float) -> tuple:
    """Calculate P&L using best available entry price. Returns (pnl, shares)."""
    ep = _get_entry_price(trade)
    sz = _get_size(trade)
    shares = sz / ep if ep > 0 else 0
    pnl = round((close_price - ep) * shares, 2)
    return pnl, shares


def _apply_fill_details(trade: dict, order_resp: dict, planned_size: float, planned_price: float):
    """Extract fill details from buy_shares response and apply to trade dict."""
    if not order_resp:
        return
    trade["actual_size"] = order_resp.get("usdc_spent") or planned_size
    trade["actual_entry_price"] = order_resp.get("effective_price") or planned_price
    trade["shares_held"] = order_resp.get("shares_bought") or 0
    # Correct size for exposure tracking
    trade["size"] = trade["actual_size"]


def _correct_sell_pnl(trade: dict, sell_resp: dict, trade_id: int):
    """If sell_shares returned actual USDC received, correct P&L in DB."""
    if not sell_resp:
        return
    usdc_received = sell_resp.get("usdc_received", 0)
    if usdc_received > 0:
        actual_cost = _get_size(trade)
        real_pnl = round(usdc_received - actual_cost, 2)
        db.update_closed_trade_pnl(trade_id, real_pnl, usdc_received)
        logger.info("[PNL-FIX] #%d corrected: formula→real P&L=$%+.2f (received=$%.2f - cost=$%.2f)",
                    trade_id, real_pnl, usdc_received, actual_cost)


def _cb_success():
    global _cb_failures
    with _cb_lock:
        _cb_failures = 0


def _cb_fail():
    global _cb_failures, _cb_open_until
    with _cb_lock:
        _cb_failures += 1
        if _cb_failures >= _CB_THRESHOLD:
            _cb_open_until = _time.time() + _CB_PAUSE_SECS
            _cb_failures = 0
            logger.warning("Circuit Breaker OPEN: %d Fehler hintereinander — %ds Pause",
                           _CB_THRESHOLD, _CB_PAUSE_SECS)


def _api_get(url, params=None, timeout=config.API_TIMEOUT, max_retries=config.API_MAX_RETRIES):
    """GET mit exponential Backoff (1s, 2s, 4s) und Circuit Breaker."""
    global _cb_open_until
    if _time.time() < _cb_open_until:
        remaining = int(_cb_open_until - _time.time())
        logger.warning("Circuit Breaker aktiv — noch %ds Pause", remaining)
        return None
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            _cb_success()
            return resp
        except Exception as e:
            wait = 2 ** attempt  # 1s, 2s, 4s
            _cb_fail()
            if attempt < max_retries - 1:
                logger.warning("API Fehler (Versuch %d/%d): %s — Retry in %ds",
                               attempt + 1, max_retries, e, wait)
                _time.sleep(wait)
            else:
                logger.warning("API Fehler (alle %d Versuche): %s", max_retries, e)
    return None


import re as _re

def _match_key(question: str) -> str:
    """Extract match key from market question for grouping related markets.

    'Valorant: Nuxeria vs F9 EICAR - Map 1 Winner' -> 'nuxeria vs f9 eicar'
    'Valorant: Nuxeria vs F9 EICAR (BO3)' -> 'nuxeria vs f9 eicar'
    'LoL: Fnatic vs G2 - Game 2 Winner' -> 'fnatic vs g2'
    """
    q = question.lower()
    # Remove prefix (game name + colon)
    q = _re.sub(r'^(counter-strike|valorant|dota ?2?|lol|league of legends)\s*:\s*', '', q)
    # Remove suffixes (Map X, Game X, BO3, spread, O/U etc)
    q = _re.split(r'\s*[-–(]\s*(map|game|bo[0-9]|spread|o/u|qualification|group)', q)[0]
    return q.strip()


def _get_current_balance() -> float:
    """Aktueller Kontostand (Startkapital + realisierte Gewinne)."""
    stats = db.get_copy_trade_stats()
    return STARTING_BALANCE + stats["total_pnl"]


# Per-trader maps — initial load, refreshed by _reload_maps() on each scan
_BET_SIZE_MAP = _parse_float_map(config.BET_SIZE_MAP, "BET_SIZE_MAP")
_TAKE_PROFIT_MAP = _parse_float_map(config.TAKE_PROFIT_MAP, "TAKE_PROFIT_MAP")
_STOP_LOSS_MAP = _parse_float_map(config.STOP_LOSS_MAP, "STOP_LOSS_MAP")
_MIN_ENTRY_PRICE_MAP = _parse_float_map(config.MIN_ENTRY_PRICE_MAP, "MIN_ENTRY_PRICE_MAP")
_MAX_ENTRY_PRICE_MAP = _parse_float_map(config.MAX_ENTRY_PRICE_MAP, "MAX_ENTRY_PRICE_MAP")
_AVG_TRADER_SIZE_MAP = _parse_float_map(config.AVG_TRADER_SIZE_MAP, "AVG_TRADER_SIZE_MAP")

_CATEGORY_BLACKLIST: dict[str, set[str]] = {}
for _cbl_entry in config.CATEGORY_BLACKLIST_MAP.split(","):
    _cbl_entry = _cbl_entry.strip()
    if ":" in _cbl_entry:
        _cbl_parts = _cbl_entry.split(":", 1)
        _cbl_name = _cbl_parts[0].strip().lower()
        _cbl_cats = {c.strip().lower() for c in _cbl_parts[1].split("|") if c.strip()}
        _CATEGORY_BLACKLIST[_cbl_name] = _cbl_cats

_MIN_CONVICTION_MAP = _parse_float_map(config.MIN_CONVICTION_RATIO_MAP, "MIN_CONVICTION_RATIO_MAP")

# --- Hot-Reload: re-read settings.env maps on each scan cycle ---
_last_settings_mtime = 0.0

def _reload_maps():
    """Re-read per-trader maps from settings.env if file changed since last check."""
    global _BET_SIZE_MAP, _TAKE_PROFIT_MAP, _STOP_LOSS_MAP, _MIN_ENTRY_PRICE_MAP
    global _MAX_ENTRY_PRICE_MAP, _AVG_TRADER_SIZE_MAP, _EXPOSURE_MAP, _MIN_TRADER_USD_MAP
    global _CATEGORY_BLACKLIST, _MIN_CONVICTION_MAP, _last_settings_mtime

    settings_path = _os.path.join(_BASE_DIR, "settings.env")
    try:
        mtime = _os.path.getmtime(settings_path)
    except OSError:
        return
    if mtime <= _last_settings_mtime:
        return  # file unchanged

    _last_settings_mtime = mtime
    # Re-read settings.env via dotenv
    from dotenv import dotenv_values
    vals = dotenv_values(settings_path)

    _BET_SIZE_MAP = _parse_float_map(vals.get("BET_SIZE_MAP", ""), "BET_SIZE_MAP")
    _TAKE_PROFIT_MAP = _parse_float_map(vals.get("TAKE_PROFIT_MAP", ""), "TAKE_PROFIT_MAP")
    _STOP_LOSS_MAP = _parse_float_map(vals.get("STOP_LOSS_MAP", ""), "STOP_LOSS_MAP")
    _MIN_ENTRY_PRICE_MAP = _parse_float_map(vals.get("MIN_ENTRY_PRICE_MAP", ""), "MIN_ENTRY_PRICE_MAP")
    _MAX_ENTRY_PRICE_MAP = _parse_float_map(vals.get("MAX_ENTRY_PRICE_MAP", ""), "MAX_ENTRY_PRICE_MAP")
    _AVG_TRADER_SIZE_MAP = _parse_float_map(vals.get("AVG_TRADER_SIZE_MAP", ""), "AVG_TRADER_SIZE_MAP")
    _EXPOSURE_MAP = _parse_float_map(vals.get("TRADER_EXPOSURE_MAP", ""), "TRADER_EXPOSURE_MAP")
    _MIN_TRADER_USD_MAP = _parse_float_map(vals.get("MIN_TRADER_USD_MAP", ""), "MIN_TRADER_USD_MAP")
    _MIN_CONVICTION_MAP = _parse_float_map(vals.get("MIN_CONVICTION_RATIO_MAP", ""), "MIN_CONVICTION_RATIO_MAP")

    # Reload category blacklist
    _CATEGORY_BLACKLIST.clear()
    for entry in (vals.get("CATEGORY_BLACKLIST_MAP", "") or "").split(","):
        entry = entry.strip()
        if ":" in entry:
            parts = entry.split(":", 1)
            name = parts[0].strip().lower()
            cats = {c.strip().lower() for c in parts[1].split("|") if c.strip()}
            _CATEGORY_BLACKLIST[name] = cats

    logger.info("[RELOAD] Settings maps refreshed (%d trader configs)", len(_BET_SIZE_MAP))

# Category keywords for market question detection
_CATEGORY_KEYWORDS = {
    "nba": ["nba", "lakers", "celtics", "warriors", "bulls", "bucks", "heat", "knicks", "76ers",
            "nets", "clippers", "mavericks", "nuggets", "suns", "grizzlies", "pelicans", "hawks",
            "cavaliers", "wizards", "hornets", "magic", "pacers", "pistons", "raptors", "kings",
            "spurs", "thunder", "timberwolves", "trail blazers", "jazz", "rockets",
            # Euroleague / international basketball (sovereign2013 trades these)
            "euroleague", "zalgiris", "fenerbahce", "hapoel", "maccabi", "olympiacos",
            "panathinaikos", "partizan", "red star", "bc dubai", "virtus bologna"],
    "mlb": ["mlb", "yankees", "red sox", "cubs", "dodgers", "mets", "astros", "braves", "phillies",
            "padres", "cardinals", "orioles", "rays", "guardians", "rangers", "twins", "mariners",
            "royals", "tigers", "white sox", "pirates", "reds", "brewers", "diamondbacks", "giants",
            "rockies", "marlins", "athletics", "angels", "nationals"],
    "nhl": ["nhl", "bruins", "maple leafs", "hurricanes", "devils",
            "islanders", "penguins", "flyers", "blue jackets", "red wings", "lightning",
            "senators", "canadiens", "sabres", "avalanche", "minnesota wild", "predators",
            "blackhawks", "flames", "oilers", "canucks", "kraken", "golden knights", "ducks", "sharks"],
    "nfl": ["nfl", "chiefs", "eagles", "49ers", "ravens", "cowboys", "bills", "dolphins",
            "lions", "packers", "texans", "bengals", "steelers", "broncos", "chargers", "rams",
            "seahawks", "bears", "vikings", "saints", "falcons", "buccaneers", "commanders",
            "cardinals", "colts", "jaguars", "titans", "raiders", "jets", "patriots", "panthers", "giants"],
    "tennis": ["tennis", "atp", "wta", "roland garros", "wimbledon", "us open tennis",
               "australian open", "monte carlo", "madrid open", "rome open", "indian wells",
               "miami open", "campinas", "sarasota", "monza", "challenger",
               "upper austria", "linz",  # WTA Linz tournament
               # Known tennis tournament cities (Challengers/ITF often just show city name)
               "mexico city:", "buenos aires:", "santiago:", "lima:", "bogota:",
               "pune:", "bengaluru:", "chennai:", "taipei:",
               # Known tennis player names (sovereign2013's frequent bets)
               "duckworth", "norrie", "de minaur", "monfils", "bublik", "sinner",
               "alcaraz", "djokovic", "medvedev", "rublev", "fritz", "ruud",
               "tsitsipas", "zverev", "berrettini", "tiafoe", "paul", "shelton",
               "volynets", "vekic", "badosa", "pliskova", "sasnovich", "grabher",
               "swiatek", "sabalenka", "gauff", "pegula", "keys", "rybakina",
               "ostapenko", "yastremska", "ruse", "danilina", "boulter",  # more WTA players
               "nardi", "landaluce", "gaston", "fery", "sachko", "rincon",  # ATP Challengers
               "pigato", "semenistaja", "kolar", "sakellaridis",  # more Challengers
               "basavareddy", "draxl", "moutet", "atmane", "popyrin", "cilic"],
    "soccer": ["soccer", "football", "premier league", "la liga", "bundesliga", "serie a",
               "ligue 1", "champions league", "ucl", "europa league", "mls",
               "bayern", "barcelona", "madrid", "arsenal", "liverpool", "manchester",
               "chelsea", "tottenham", "juventus", "inter milan", "ac milan", "psg",
               "freiburg", "dortmund", "southampton", "liga mx", "copa"],
    "cs": ["counter-strike", "cs2", "cs:", "csgo"],
    "lol": ["lol:", "league of legends"],
    "valorant": ["valorant", "val:"],
    "dota": ["dota 2", "dota:"],
    "geopolitics": ["trump", "iran", "tariff", "sanctions", "war", "election", "hormuz",
                    "china", "nato", "congress", "senate", "president", "minister"],
    "cricket": ["cricket", "t20", "ipl", "test match", "odi"],
}


def _detect_category(question: str) -> str:
    """Detect category from market question. Returns lowercase category name or empty string.
    Checks esports first (cs/lol/valorant/dota) to avoid false matches with generic sport keywords.
    """
    q = question.lower()
    # Check esports first — their prefixes are unambiguous
    for cat in ("cs", "lol", "valorant", "dota"):
        for kw in _CATEGORY_KEYWORDS[cat]:
            if kw in q:
                return cat
    # Then check all other categories
    for cat, keywords in _CATEGORY_KEYWORDS.items():
        if cat in ("cs", "lol", "valorant", "dota"):
            continue
        for kw in keywords:
            if kw in q:
                return cat
    return ""


def _is_category_blocked(trader_name: str, question: str) -> bool:
    """Check if this trade's category is blacklisted for this trader."""
    blocked = _CATEGORY_BLACKLIST.get(trader_name.lower())
    if not blocked:
        return False
    cat = _detect_category(question)
    return cat in blocked


def _calculate_position_size(entry_price: float, cash: float, trader_ratio: float = 1.0,
                             portfolio_value: float = 0, trader_name: str = "") -> float:
    """Bet-Sizing: X% vom Portfolio/Cash × Preis-Signal × proportionaler Trader-Multiplikator.

    BET_SIZE_BASIS controls whether sizing uses cash or portfolio value.
    BET_SIZE_MAP allows per-trader override of BET_SIZE_PCT.
    Result is always capped to available cash.
    """
    available = cash - CASH_RESERVE
    if available <= 0:
        return 0

    # Sizing basis: cash or portfolio (configurable)
    if config.BET_SIZE_BASIS == "portfolio" and portfolio_value > 0:
        sizing_base = portfolio_value
    else:
        sizing_base = cash

    # Per-trader bet size override
    bet_pct = _BET_SIZE_MAP.get(trader_name.lower(), BET_SIZE_PCT)

    # Basis: bet_pct vom Sizing-Base
    base = sizing_base * bet_pct

    # Preis-Signal Multiplikator
    edge = abs(entry_price - 0.50)
    if edge >= config.PRICE_EDGE_HIGH:
        price_mult = config.PRICE_MULT_HIGH
    elif edge >= config.PRICE_EDGE_MED:
        price_mult = config.PRICE_MULT_MED
    else:
        price_mult = config.PRICE_MULT_LOW

    # Proportionaler Trader-Multiplikator
    clamped_ratio = max(config.RATIO_MIN, min(config.RATIO_MAX, trader_ratio))

    size = base * price_mult * clamped_ratio
    size = min(size, MAX_POSITION_SIZE, available)  # never exceed cash
    return round(max(MIN_TRADE_SIZE, size), 2)


CASH_FLOOR = config.CASH_FLOOR
CASH_RECOVERY = config.CASH_RECOVERY
SAVE_POINT_STEP = config.SAVE_POINT_STEP

import os as _os

_BASE_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_DYNAMIC_FLOOR_PATH = _os.path.join(_BASE_DIR, "dynamic_floor.txt")
_SAVE_POINT_PATH = _os.path.join(_BASE_DIR, "save_point.txt")

# --- Persistenz: dynamic_floor.txt + save_point.txt (ueberlebt Neustarts) ---

def _load_dynamic_floor() -> float:
    """Aktuellen Floor laden (startet bei CASH_FLOOR, steigt pro Recovery)."""
    try:
        if _os.path.exists(_DYNAMIC_FLOOR_PATH):
            with open(_DYNAMIC_FLOOR_PATH, "r") as f:
                val = float(f.read().strip())
                if val >= CASH_FLOOR:
                    return val
    except Exception:
        pass
    return CASH_FLOOR

def _store_dynamic_floor(value: float):
    """Floor in Datei speichern."""
    try:
        with open(_DYNAMIC_FLOOR_PATH, "w") as f:
            f.write(str(value))
    except Exception as e:
        logger.error("Fehler beim Speichern dynamic_floor: %s", e)

def _load_save_point() -> float:
    """Recovery-Flag laden (>0 = Recovery-Modus aktiv)."""
    try:
        if _os.path.exists(_SAVE_POINT_PATH):
            with open(_SAVE_POINT_PATH, "r") as f:
                return float(f.read().strip())
    except Exception:
        pass
    return 0.0

def _store_save_point(value: float):
    """Recovery-Flag speichern."""
    try:
        with open(_SAVE_POINT_PATH, "w") as f:
            f.write(str(value))
    except Exception as e:
        logger.error("Fehler beim Speichern save_point: %s", e)


def _check_trade_limit():
    """Cash-Floor mit dynamischem Floor und Recovery.

    Beispiel-Ablauf:
      Floor=$20, Cash faellt auf $20 -> STOP
      Cash steigt auf $26 (Floor+$6) -> kaufen erlaubt, Floor wird $21
      Cash faellt auf $21 -> STOP
      Cash steigt auf $27 ($21+$6) -> kaufen erlaubt, Floor wird $22
      usw.
    """
    dynamic_floor = _load_dynamic_floor()
    in_recovery = _load_save_point() > 0

    # Echte Wallet-Balance
    try:
        from bot.order_executor import get_wallet_balance
        cash = get_wallet_balance()
    except Exception:
        logger.warning("Wallet balance check failed — skipping trade limit check")
        return True  # Lieber traden als wegen API-Fehler pausieren

    # Cash unter/gleich Floor -> STOP
    if cash <= dynamic_floor:
        if not in_recovery:
            _store_save_point(1.0)  # Recovery-Modus aktivieren
            logger.info("STOP: Cash $%.2f <= Floor $%.2f — warte auf +$%.2f Recovery.",
                        cash, dynamic_floor, CASH_RECOVERY)
        logger.info("PAUSE: Cash $%.2f <= Floor $%.2f", cash, dynamic_floor)
        return False

    # Recovery-Modus aktiv?
    if in_recovery:
        recovery_target = dynamic_floor + CASH_RECOVERY
        if cash < recovery_target:
            logger.info("PAUSE: Cash $%.2f < Recovery-Ziel $%.2f (Floor $%.2f + $%.2f)",
                        cash, recovery_target, dynamic_floor, CASH_RECOVERY)
            return False
        # Recovery erreicht! Kaufen erlaubt, Floor hochsetzen
        new_floor = dynamic_floor + SAVE_POINT_STEP
        logger.info("RECOVERY: Cash $%.2f >= $%.2f — kaufen erlaubt. Floor $%.2f -> $%.2f",
                    cash, recovery_target, dynamic_floor, new_floor)
        _store_dynamic_floor(new_floor)
        _store_save_point(0.0)  # Recovery-Modus beenden
        return True

    # Normalmodus: Cash ueber Floor, kein Recovery
    return True


def _parse_end_ts(end_date_str: str) -> float:
    """ISO-8601 end_date → Unix-Timestamp. Gibt 0.0 bei Fehler zurück.
    Bei reinem Datum (YYYY-MM-DD) → Ende des Tages 23:59:59 UTC."""
    if not end_date_str:
        return 0.0
    try:
        from datetime import datetime, timezone, timedelta
        dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        # Nur Datum ohne Zeit → Ende des Tages setzen
        if len(end_date_str) <= 10:
            dt = dt.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _process_pending_buys(balance: float, total_invested: float) -> int:
    """Prüft die Pending-Buy-Queue und feuert reife Trades ab.

    Ein Pending Buy feuert wenn:
      1. Preis jetzt >= BUY_THRESHOLD (Token hat sich bestätigt)
      2. Mindestwartezeit PENDING_BUY_MIN_SECS abgelaufen
    Nach PENDING_BUY_MAX_SECS wird er verworfen.
    """
    if not _pending_buys or BUY_THRESHOLD <= 0:
        return 0

    now = _time.time()
    fired = 0
    expired_keys = []

    for cid, entry in list(_pending_buys.items()):
        elapsed = now - entry["queued_at"]
        if elapsed > PENDING_BUY_MAX_SECS:
            expired_keys.append(cid)
            logger.info("[PENDING] Verworfen (timeout): %s", entry["trade_data"]["market_question"][:40])
            continue

        _extra = entry.get("extra_wait", 0)
        if elapsed < PENDING_BUY_MIN_SECS + _extra:
            continue  # Noch nicht reif

        # Aktuellen Preis prüfen
        trade_data = entry["trade_data"]
        live = price_tracker.get_price(cid, trade_data["side"]) if price_tracker.is_connected else None
        current = live if live is not None else trade_data["entry_price"]

        if current < BUY_THRESHOLD:
            continue  # Preis noch unter Threshold

        # Prüfe ob noch Kapital vorhanden
        size = _calculate_position_size(current, balance,
                                        trader_ratio=entry.get("trader_ratio", 1.0),
                                        trader_name=trade_data.get("wallet_username", ""))
        # Cash-Floor Check: genug Cash uebrig?
        cash_left = balance - total_invested - size
        if cash_left < _load_dynamic_floor():
            expired_keys.append(cid)
            logger.info("[PENDING] Kein Cash mehr: %s", trade_data["market_question"][:40])
            continue

        trade_data["entry_price"] = round(min(current + ENTRY_SLIPPAGE, 0.97), 4)
        trade_data["size"] = size
        # LIVE_MODE: echte Order platzieren BEVOR DB-Record erstellt wird
        if LIVE_MODE and cid:
            try:
                order_resp = buy_shares(cid, trade_data["side"], size, trade_data["entry_price"])
                if not order_resp:
                    logger.warning("[PENDING] Order failed, skipping: %s", trade_data["market_question"][:40])
                    expired_keys.append(cid)
                    continue
                _apply_fill_details(trade_data, order_resp, size, trade_data["entry_price"])
                size = trade_data["size"]
            except Exception as _pe:
                logger.warning("[PENDING] Buy error: %s", _pe)
                expired_keys.append(cid)
                continue
        trade_id = db.create_copy_trade(trade_data)
        if trade_id:
            fired += 1
            total_invested += size
            if cid:
                price_tracker.subscribe_condition(cid)
            logger.info("[PENDING] FIRED #%d: %s @ %.0fc", trade_id,
                        trade_data["market_question"][:40], current * 100)
        expired_keys.append(cid)

    for k in expired_keys:
        _pending_buys.pop(k, None)
    return fired


def _position_diff_scan(address: str, username: str, balance: float,
                        total_invested: float, portfolio_value: float = 0) -> int:
    """Position-Diff: findet neue Positionen die der Activity-Feed verpasst hat.

    Holt aktuelle Positionen des Traders und vergleicht mit unseren copy_trades.
    Jede Condition-ID die weder als 'open' noch als 'baseline' in unserer DB ist
    → neuer Trade der kopiert werden soll.

    Applies the SAME filters as the activity scan to prevent bypass.
    """
    try:
        positions = fetch_wallet_positions(address)
        if not positions:
            return 0

        # Alle bekannten condition_ids für diese Wallet (open + baseline)
        known = {t["condition_id"] for t in db.get_all_copy_trades_for_wallet(address) if t["condition_id"]}
        # Cache open trades once for this scan
        _diff_open = [dict(t) for t in db.get_open_copy_trades()]

        new_trades = 0
        for pos in positions:
            cid = pos.get("condition_id", "")
            if not cid or cid in known:
                continue
            if pos.get("redeemable", False) or pos.get("size", 0) < config.MIN_POSITION_SIZE_FILTER:
                continue

            entry_price_raw = pos.get("current_price", 0)
            if entry_price_raw <= 0 or entry_price_raw >= 1:
                continue

            # === SAME FILTERS AS ACTIVITY SCAN ===
            _q = pos["market_question"]
            _s = pos.get("side", "")
            # No-rebuy: don't re-enter a market we recently closed
            if cid and config.NO_REBUY_MINUTES > 0:
                try:
                    from database.db import get_connection as _gc_diff
                    with _gc_diff() as _rc_diff:
                        _was_closed_diff = _rc_diff.execute(
                            "SELECT id FROM copy_trades WHERE condition_id=? AND status='closed' "
                            "AND closed_at > datetime('now', '-' || ? || ' minutes', 'localtime')", (cid, str(config.NO_REBUY_MINUTES))
                        ).fetchone()
                        if _was_closed_diff:
                            _log_block(username, _q, cid, _s, entry_price_raw, "no_rebuy",
                                       "closed within %dmin" % config.NO_REBUY_MINUTES, "diff")
                            continue
                except Exception as _nre_diff:
                    logger.warning("[NO_REBUY] DB check failed, skipping conservatively: %s", _nre_diff)
                    continue

            # Category blacklist
            if _is_category_blocked(username, _q):
                _log_block(username, _q, cid, _s, entry_price_raw, "category_blacklist",
                           "category '%s' blocked" % _detect_category(_q), "diff")
                continue
            # Price range filter (per-trader override via MIN/MAX_ENTRY_PRICE_MAP)
            _min_price = _MIN_ENTRY_PRICE_MAP.get(username.lower(), config.MIN_ENTRY_PRICE)
            _max_price = _MAX_ENTRY_PRICE_MAP.get(username.lower(), config.MAX_ENTRY_PRICE)
            if entry_price_raw < _min_price or entry_price_raw > _max_price:
                _log_block(username, _q, cid, _s, entry_price_raw, "price_range",
                           "%.0fc outside %.0f-%.0fc" % (entry_price_raw*100, _min_price*100, _max_price*100), "diff")
                continue

            # Max copies per market
            if cid and db.count_copies_for_market(address, cid) >= config.MAX_COPIES_PER_MARKET:
                _log_block(username, _q, cid, _s, entry_price_raw, "max_copies",
                           "max %d copies reached" % config.MAX_COPIES_PER_MARKET, "diff")
                continue

            # Duplicate market check (another trader already has this market)
            if cid and db.is_market_already_open(cid, from_wallet=address):
                _log_block(username, _q, cid, _s, entry_price_raw, "cross_trader_dupe",
                           "market open from another trader", "diff")
                continue

            # Hedge check: don't buy opposite side of an existing position
            if cid:
                _existing = [x for x in _diff_open if x.get("condition_id") == cid and x.get("wallet_address") == address]
                if _existing:
                    _existing_sides = {x.get("side", "") for x in _existing}
                    if pos["side"] not in _existing_sides:
                        logger.info("[DIFF] Hedge blocked (%s open, skipping %s): %s",
                                    "/".join(_existing_sides), pos["side"], _q[:40])
                        _log_block(username, _q, cid, _s, entry_price_raw, "hedge_blocked",
                                   "%s open, skipping %s" % ("/".join(_existing_sides), _s), "diff")
                        continue

            # Max per event (DB query includes recently closed)
            _diff_evt_remaining = None
            if config.MAX_PER_EVENT > 0:
                _evt = pos.get("event_slug", "") or ""
                if _evt:
                    _evt_inv = db.get_invested_for_event(_evt)
                    _diff_evt_remaining = config.MAX_PER_EVENT - _evt_inv
                    if _diff_evt_remaining < config.MIN_TRADE_SIZE:
                        _log_block(username, _q, cid, _s, entry_price_raw, "event_full",
                                   "$%.0f/$%.0f invested" % (_evt_inv, config.MAX_PER_EVENT), "diff")
                        continue

            # Max per match (DB query includes recently closed)
            _diff_remaining = None
            if config.MAX_PER_MATCH > 0:
                _diff_mkey = _match_key(_q)
                if _diff_mkey and len(_diff_mkey) > 3:
                    _diff_match_inv = sum(
                        ot["size"] for ot in _diff_open
                        if _match_key(ot.get("market_question", "")) == _diff_mkey
                    )
                    _diff_evt_for_match = pos.get("event_slug", "") or ""
                    if _diff_evt_for_match:
                        _db_evt_inv = db.get_invested_for_event(_diff_evt_for_match)
                        _diff_match_inv = max(_diff_match_inv, _db_evt_inv)
                    _diff_remaining = config.MAX_PER_MATCH - _diff_match_inv
                    if _diff_remaining < config.MIN_TRADE_SIZE:
                        logger.info("[DIFF] Match full $%.0f/$%.0f, skipping: %s",
                                    _diff_match_inv, config.MAX_PER_MATCH, _q[:40])
                        _log_block(username, _q, cid, _s, entry_price_raw, "match_full",
                                   "$%.0f/$%.0f invested" % (_diff_match_inv, config.MAX_PER_MATCH), "diff")
                        continue
                    if _diff_evt_remaining is not None:
                        _diff_remaining = min(_diff_remaining, _diff_evt_remaining)
                    else:
                        _diff_remaining = _diff_remaining
                elif _diff_evt_remaining is not None:
                    _diff_remaining = _diff_evt_remaining

            # Max exposure per trader (use portfolio value like activity scan)
            _diff_portfolio = portfolio_value if portfolio_value > 0 else (balance + sum(t["size"] for t in _diff_open))
            _max_exp = _diff_portfolio * _EXPOSURE_MAP.get(username.lower(), config.MAX_EXPOSURE_PER_TRADER)
            _t_exp = db.get_trader_exposure(address)
            _diff_exp_remaining = _max_exp - _t_exp
            if _diff_exp_remaining < config.MIN_TRADE_SIZE:
                logger.info("[DIFF] Trader exposure $%.0f >= max $%.0f, skipping: %s",
                            _t_exp, _max_exp, _q[:40])
                _log_block(username, _q, cid, _s, entry_price_raw, "exposure_limit",
                           "$%.0f >= $%.0f max" % (_t_exp, _max_exp), "diff")
                continue

            # Market-close guard
            end_ts = _parse_end_ts(pos.get("end_date", ""))
            if end_ts and (_time.time() - end_ts) > 0:
                continue
            # Max market duration: skip if market resolves too far in future
            if config.MAX_MARKET_HOURS > 0 and end_ts:
                _hours_until_end = (end_ts - _time.time()) / 3600
                if _hours_until_end > config.MAX_MARKET_HOURS:
                    _log_block(username, _q, cid, _s, entry_price_raw, "market_too_long",
                               "resolves in %.0fh > %.0fh max" % (_hours_until_end, config.MAX_MARKET_HOURS), "diff")
                    continue

            # Event timing: skip if event > MAX_HOURS_BEFORE_EVENT away
            if config.MAX_HOURS_BEFORE_EVENT > 0:
                _diff_evt_slug = pos.get("event_slug", "") or pos.get("market_slug", "")
                if _diff_evt_slug:
                    try:
                        _diff_ev_r = requests.get("https://gamma-api.polymarket.com/events",
                                                  params={"slug": _diff_evt_slug.split("/")[-1]}, timeout=config.GAMMA_API_TIMEOUT)
                        if _diff_ev_r.ok and _diff_ev_r.json():
                            _diff_ev = _diff_ev_r.json()[0] if isinstance(_diff_ev_r.json(), list) else _diff_ev_r.json()
                            _diff_st = _diff_ev.get("startTime", "")
                            if _diff_st:
                                from datetime import datetime as _dt, timezone as _tz
                                _diff_start = _dt.fromisoformat(_diff_st.replace("Z", "+00:00"))
                                _diff_hours = (_diff_start - _dt.now(_tz.utc)).total_seconds() / 3600
                                if _diff_hours > config.MAX_HOURS_BEFORE_EVENT:
                                    logger.info("[DIFF] Event in %.1fh > %.1fh max, skipping: %s",
                                                _diff_hours, config.MAX_HOURS_BEFORE_EVENT, _q[:40])
                                    _log_block(username, _q, cid, _s, entry_price_raw, "event_timing",
                                               "event in %.1fh > %.1fh max" % (_diff_hours, config.MAX_HOURS_BEFORE_EVENT), "diff")
                                    continue
                    except Exception:
                        pass

            entry_price = round(min(entry_price_raw + ENTRY_SLIPPAGE, config.MAX_ENTRY_PRICE_CAP), 4)
            size = _calculate_position_size(entry_price, balance, trader_name=username)
            # Cap to match/event remaining budget
            if _diff_remaining is not None and size > _diff_remaining:
                size = round(_diff_remaining, 2)
                logger.info("[DIFF] Capped to event/match budget: $%.2f | %s", size, pos["market_question"][:35])
            # Cap to trader exposure remaining
            if _diff_exp_remaining > 0 and size > _diff_exp_remaining:
                size = round(_diff_exp_remaining, 2)
                logger.info("[DIFF] Capped to trader exposure: $%.2f | %s", size, pos["market_question"][:35])
            # Cap to MAX_POSITION_SIZE
            if cid:
                _diff_existing = sum(ot["size"] for ot in _diff_open if ot.get("condition_id") == cid)
                _diff_pos_remaining = MAX_POSITION_SIZE - _diff_existing
                if _diff_pos_remaining < config.MIN_TRADE_SIZE:
                    continue
                if size > _diff_pos_remaining:
                    size = round(_diff_pos_remaining, 2)
            if size < MIN_TRADE_SIZE:
                continue
            cash_left = balance - total_invested - size
            if cash_left < _load_dynamic_floor():
                break

            trade = {
                "wallet_address": address,
                "wallet_username": username,
                "market_question": pos["market_question"],
                "market_slug": pos.get("market_slug", ""),
                "event_slug": pos.get("event_slug", ""),
                "side": pos["side"],
                "entry_price": entry_price,
                "size": size,
                "end_date": pos.get("end_date", ""),
                "outcome_label": pos.get("outcome_label", ""),
                "condition_id": cid,
                "category": _detect_category(pos["market_question"]),
            }
            # === TRADE SCORER ===
            try:
                _score_result = score_trade(
                    trader_name=username,
                    condition_id=cid,
                    side=pos["side"],
                    entry_price=entry_price,
                    market_question=pos["market_question"],
                    category=trade["category"],
                    event_slug=pos.get("event_slug", ""),
                    trader_size_usd=pos.get("size", 0),
                    spread=0.03,
                    hours_until_event=12,
                )
            except Exception as _se:
                logger.warning("[SCORE] Scorer error, defaulting to EXECUTE: %s", _se)
                _score_result = {"action": "EXECUTE", "score": 50, "components": {}, "reason": "scorer_error"}
            if _score_result["action"] == "BLOCK":
                _log_block(username, pos["market_question"], cid, pos["side"], entry_price,
                           "score_block", _score_result["reason"], "diff")
                continue
            if _score_result["action"] == "QUEUE":
                _pending_buys[cid] = {
                    "trade_data": trade, "queued_at": _time.time(),
                    "extra_wait": int(PENDING_BUY_MIN_SECS * 0.5),
                    "score": _score_result["score"],
                }
                logger.info("[SCORE-QUEUE] %s queued (score=%d): %s",
                            username, _score_result["score"], pos["market_question"][:40])
                continue
            if _score_result["action"] == "BOOST":
                from bot.kelly import get_kelly_multiplier
                _kelly_m = get_kelly_multiplier(username)
                size = round(size * _kelly_m, 2)
                size = min(size, MAX_POSITION_SIZE, balance - total_invested - _load_dynamic_floor())
                if size < MIN_TRADE_SIZE:
                    continue
                trade["size"] = size
                logger.info("[SCORE-BOOST] %s boosted x%.2f (score=%d): %s",
                            username, _kelly_m, _score_result["score"], pos["market_question"][:40])
            # LIVE MODE: Echte Order platzieren
            with _buy_lock:
                if cid and db.count_copies_for_market(address, cid) >= config.MAX_COPIES_PER_MARKET:
                    continue
                if LIVE_MODE and cid:
                    try:
                        from bot.liquidity_check import check_liquidity
                        if not check_liquidity(cid, pos["side"], size):
                            continue
                    except Exception:
                        pass
                    order_resp = buy_shares(cid, pos["side"], size, entry_price)
                    if not order_resp:
                        logger.warning("[DIFF] Order fehlgeschlagen — ueberspringe: %s", pos["market_question"][:40])
                        continue
                    _apply_fill_details(trade, order_resp, size, entry_price)
                    size = trade["size"]

                trade_id = db.create_copy_trade(trade)
            if trade_id:
                new_trades += 1
                total_invested += size
                balance -= size
                price_tracker.subscribe_condition(cid)
                logger.info("[DIFF] Neuer Trade #%d (via Position-Diff): %s @ %.0fc (%s)",
                            trade_id, pos["market_question"][:40], entry_price * 100, pos["side"])
                db.log_activity("buy", "BUY", "Copied position from %s" % username,
                                "#%d %s @ %dc — $%.2f" % (trade_id, pos["market_question"][:40], entry_price * 100, size))
        return new_trades
    except Exception as e:
        logger.info("Position-diff error for %s: %s", address[:10], e)
        return 0


def _run_baseline(address: str, username: str):
    """Baseline fuer neu gefolgte Wallet: Snapshot + Timestamp, nichts kopieren."""
    positions = fetch_wallet_positions(address)
    if positions:
        logger.info("[BASELINE] %s — saving %d existing positions (not copying)", username, len(positions))
        for pos in positions:
            if pos["size"] < config.MIN_POSITION_SIZE_FILTER or pos.get("redeemable", False):
                continue
            cid = pos.get("condition_id", "")
            if cid and not db.is_trade_duplicate(address, pos["market_question"], cid):
                db.create_baseline_trade({
                    "wallet_address": address,
                    "wallet_username": username,
                    "market_question": pos["market_question"],
                    "market_slug": pos.get("market_slug", ""),
                    "event_slug": pos.get("event_slug", ""),
                    "side": pos["side"],
                    "entry_price": pos["current_price"],
                    "end_date": pos.get("end_date", ""),
                    "outcome_label": pos.get("outcome_label", ""),
                    "condition_id": cid,
                })
    # Echter letzter Trade-Timestamp aus API (nicht time.now) — damit Idle-Check korrekt arbeitet
    baseline_trades = fetch_wallet_recent_trades(address, limit=5)
    real_last_ts = max((t["timestamp"] for t in baseline_trades), default=int(_time.time()))
    db.set_last_trade_timestamp(address, real_last_ts)
    db.set_wallet_baselined(address)
    logger.info("[BASELINE] done: %s (letzter Trade vor %.0f min)",
                username, (int(_time.time()) - real_last_ts) / 60)


def _run_idle_check(followed: list):
    """Ersetzt einzelne Trader die > 20 Min inaktiv sind."""
    global _idle_replaced_at
    now = int(_time.time())
    idle_threshold = now - IDLE_TRIGGER_SECS
    REPLACE_COOLDOWN = config.IDLE_REPLACE_COOLDOWN
    idle_addresses = set()
    for w in followed:
        # Noch nicht baselined → timestamp ist 0 → wäre fälschlicherweise "idle"
        if not w["baseline_scanned"]:
            continue
        # Cooldown: wurde diese Wallet in den letzten 30 Min bereits ersetzt?
        last_replaced = _idle_replaced_at.get(w["address"], 0)
        if now - last_replaced < REPLACE_COOLDOWN:
            continue
        ts = (db.get_or_create_scan_config(w["address"]).get("last_trade_timestamp") or 0)
        if ts < idle_threshold:
            uname = w["username"] or w["address"][:12]
            logger.info("[IDLE] %s > 20 Min inaktiv — ersetze durch aktiveren Trader", uname)
            idle_addresses.add(w["address"])
    if idle_addresses:
        from bot.wallet_scanner import auto_follow_top_traders, fetch_wallet_recent_trades
        # Nur ersetzen wenn es einen Trader gibt der in letzten 20 Min aktiv war
        # Sonst Loop vermeiden: alle schlafen nachts → würde endlos ersetzen
        active_exists = False
        try:
            from bot.wallet_scanner import fetch_leaderboard_wallets, filter_wallets
            leaderboard = fetch_leaderboard_wallets(limit=50, time_period="DAY", order_by="PNL")
            twenty_min_ago = now - 20 * 60
            for cand in leaderboard[:20]:
                if cand["address"] in idle_addresses:
                    continue
                recent = fetch_wallet_recent_trades(cand["address"], limit=5)
                if any(t["timestamp"] > twenty_min_ago for t in recent):
                    active_exists = True
                    break
        except Exception:
            active_exists = True  # Im Zweifelsfall ersetzen

        if active_exists:
            for addr in idle_addresses:
                db.toggle_follow(addr, 0)
                _idle_replaced_at[addr] = now
            # Alle jemals ersetzten Adressen ausschliessen — verhindert Rotation der gleichen 3
            all_excluded = idle_addresses | set(_idle_replaced_at.keys())
            auto_follow_top_traders(count=config.AUTO_FOLLOW_COUNT, exclude=all_excluded, require_recent=True)
            # Neu-gefollte Wallets ebenfalls mit Cooldown markieren (verhindert sofortigen Re-Replace)
            for w in db.get_followed_wallets():
                addr = w["address"]
                if addr not in _idle_replaced_at:
                    _idle_replaced_at[addr] = now
        else:
            logger.info("[IDLE] Kein aktiver Ersatz gefunden (nachts?) — behalte aktuelle Trader.")


def copy_followed_wallets():
    """Scannt gefollte Wallets für NEUE Positionen via /trades Endpoint.

    LOGIC (wie polybot/ent0n29):
    1. Für jede Wallet: letzten gesehenen Trade-Timestamp aus DB
    2. Neueste Trades via /trades API holen (neuste zuerst)
    3. Nur BUY-Trades NACH dem letzten Timestamp → neue Positionen
    4. is_trade_duplicate() als Safety-Net gegen Baseline-Positionen
    5. Timestamp für nächsten Scan aktualisieren

    Vorteil: Kein falscher Alarm durch Positions-Snapshot-Vergleich (z.B. CemeterySun).
    """
    _reload_maps()  # Hot-reload settings if file changed
    followed = db.get_followed_wallets()
    if not followed:
        logger.info("Keine gefolgten Wallets. Erst Wallets folgen!")
        return 0

    # Baseline IMMER zuerst — auch wenn Trade-Limit erreicht (sonst Deadlock)
    for wallet in followed:
        address = wallet["address"]
        username = wallet["username"] or address[:12]
        if not db.is_wallet_baselined(address):
            _run_baseline(address, username)

    # Idle-Check (nur wenn via .env aktiviert)
    if config.IDLE_REPLACE_ENABLED:
        _run_idle_check(followed)

    if not _check_trade_limit():
        return 0

    # Max offene Positionen prüfen
    stats = db.get_copy_trade_stats()
    if stats["open_trades"] >= MAX_OPEN_POSITIONS:
        logger.info("[SKIP] Max offene Positionen erreicht (%d/%d)", stats["open_trades"], MAX_OPEN_POSITIONS)
        return 0

    # Max daily loss check
    if config.MAX_DAILY_LOSS > 0:
        daily_pnl = db.get_daily_copy_pnl()
        if daily_pnl <= -config.MAX_DAILY_LOSS:
            logger.info("[SKIP] Max daily loss reached ($%.2f <= -$%.0f)", daily_pnl, config.MAX_DAILY_LOSS)
            return 0

    # Max daily trades check
    if config.MAX_DAILY_TRADES > 0:
        from database.db import get_connection as _gc_daily
        with _gc_daily() as _dc:
            _today_ct = _dc.execute(
                "SELECT COUNT(*) as c FROM copy_trades WHERE status != 'baseline' AND created_at >= date('now','localtime')"
            ).fetchone()["c"]
        if _today_ct >= config.MAX_DAILY_TRADES:
            logger.info("[SKIP] Max daily trades reached (%d/%d)", _today_ct, config.MAX_DAILY_TRADES)
            return 0

    # WebSocket health check
    if not price_tracker.is_connected:
        logger.warning("[WS] Price tracker disconnected — live prices unavailable, using fallbacks")

    logger.info("[SCAN] Checking %d wallets for new positions...", len(followed))

    new_trades = 0
    try:
        from bot.order_executor import get_wallet_balance
        cash = get_wallet_balance()
    except Exception as _wb_err:
        logger.warning("[SCAN] Wallet balance error — skipping scan: %s", _wb_err)
        return 0
    balance = cash
    total_invested = 0
    # Cache open trades for this scan (convert Row→dict so .get() works everywhere)
    _cached_open_trades = [dict(t) for t in db.get_open_copy_trades()]
    # Portfolio value: wallet + active positions + redeemable (not dead 0c shares)
    _open_value = 0
    try:
        _pos_r = requests.get("https://data-api.polymarket.com/positions", params={
            "user": config.POLYMARKET_FUNDER, "limit": 500, "sizeThreshold": 0
        }, timeout=config.DATA_API_TIMEOUT)
        if _pos_r.ok:
            _open_value = sum(float(p.get("currentValue", 0) or 0) for p in _pos_r.json()
                              if float(p.get("curPrice", 0) or 0) > 0.01)
    except Exception:
        _open_value = sum(t["size"] for t in _cached_open_trades)  # fallback to DB
    portfolio_value = cash + _open_value
    logger.info("PORTFOLIO: Wallet=$%.2f | Positions=$%.2f | Total=$%.2f", cash, _open_value, portfolio_value)

    # Event-Wait-Queue: fire trades whose events are now within the time window
    if _event_wait_queue and config.MAX_HOURS_BEFORE_EVENT > 0:
        _ew_now = _time.time()
        _ew_expired = []
        for _ew_cid, _ew in list(_event_wait_queue.items()):
            hours_until = (_ew["event_start_ts"] - _ew_now) / 3600
            # Event within window → execute
            if 0 < hours_until <= config.MAX_HOURS_BEFORE_EVENT:
                td = _ew["trade_data"]
                _orig_price = td["entry_price"]

                # --- Queue Drift Filter ---
                # Get live price and check if it drifted too far from trader's original price
                _live_price = _orig_price
                try:
                    _lp = price_tracker.get_price(_ew_cid, td["side"]) if price_tracker.is_connected else None
                    if _lp and _lp > 0:
                        _live_price = _lp
                except Exception:
                    pass

                # Max allowed drift depends on price range (configurable)
                if _orig_price < 0.20:
                    _max_drift = config.QUEUE_DRIFT_LOTTERY
                elif _orig_price < 0.40:
                    _max_drift = config.QUEUE_DRIFT_UNDERDOG
                elif _orig_price < 0.60:
                    _max_drift = config.QUEUE_DRIFT_COINFLIP
                else:
                    _max_drift = config.QUEUE_DRIFT_FAVORITE

                _drift_pct = (_live_price - _orig_price) / _orig_price if _orig_price > 0 else 0
                _ew_qn = td["market_question"]
                _ew_sd = td.get("side", "")
                _ew_un = td.get("wallet_username", "")
                if _drift_pct > _max_drift:
                    logger.info("[EVENT-WAIT] SKIP drift %.0f%% > %.0f%% max (%.0fc->%.0fc): %s",
                                _drift_pct * 100, _max_drift * 100,
                                _orig_price * 100, _live_price * 100, _ew_qn[:40])
                    _log_block(_ew_un, _ew_qn, _ew_cid, _ew_sd, _live_price,
                               "queue_drift", "%.0f%% > %.0f%% max" % (_drift_pct*100, _max_drift*100), "event_wait")
                    _ew_expired.append(_ew_cid)
                    continue

                # Use live price for entry if available
                _entry_price = _live_price if _live_price != _orig_price else _orig_price
                td["entry_price"] = _entry_price

                # No-rebuy check
                if _ew_cid and config.NO_REBUY_MINUTES > 0:
                    try:
                        from database.db import get_connection as _gc_ew
                        with _gc_ew() as _rc_ew:
                            _was_closed_ew = _rc_ew.execute(
                                "SELECT id FROM copy_trades WHERE condition_id=? AND status='closed' "
                                "AND closed_at > datetime('now', '-' || ? || ' minutes', 'localtime')", (_ew_cid, str(config.NO_REBUY_MINUTES))
                            ).fetchone()
                            if _was_closed_ew:
                                _log_block(_ew_un, _ew_qn, _ew_cid, _ew_sd, _entry_price,
                                           "no_rebuy", "closed within %dmin" % config.NO_REBUY_MINUTES, "event_wait")
                                _ew_expired.append(_ew_cid)
                                continue
                    except Exception as _nre_ew:
                        logger.warning("[NO_REBUY] DB check failed, skipping conservatively: %s", _nre_ew)
                        _ew_expired.append(_ew_cid)
                        continue

                # Category blacklist
                if _is_category_blocked(td["wallet_username"], _ew_qn):
                    _log_block(_ew_un, _ew_qn, _ew_cid, _ew_sd, _entry_price,
                               "category_blacklist", "category '%s' blocked" % _detect_category(_ew_qn), "event_wait")
                    _ew_expired.append(_ew_cid)
                    continue

                # MAX_COPIES check
                if _ew_cid and db.count_copies_for_market(td["wallet_address"], _ew_cid) >= config.MAX_COPIES_PER_MARKET:
                    _log_block(_ew_un, _ew_qn, _ew_cid, _ew_sd, _entry_price,
                               "max_copies", "max %d copies reached" % config.MAX_COPIES_PER_MARKET, "event_wait")
                    _ew_expired.append(_ew_cid)
                    continue

                # Cross-trader duplicate check
                if _ew_cid and db.is_market_already_open(_ew_cid, from_wallet=td["wallet_address"]):
                    _log_block(_ew_un, _ew_qn, _ew_cid, _ew_sd, _entry_price,
                               "cross_trader_dupe", "market open from another trader", "event_wait")
                    _ew_expired.append(_ew_cid)
                    continue

                # MAX_PER_MATCH check (DB query includes recently closed)
                _ew_budget = None
                if config.MAX_PER_MATCH > 0:
                    _ew_evt_slug = td.get("event_slug", "") or ""
                    if _ew_evt_slug:
                        _ew_match_inv = db.get_invested_for_event(_ew_evt_slug)
                        _ew_budget = config.MAX_PER_MATCH - _ew_match_inv
                        if _ew_budget < MIN_TRADE_SIZE:
                            logger.info("[EVENT-WAIT] Match full $%.0f/$%.0f, skipping: %s",
                                        _ew_match_inv, config.MAX_PER_MATCH, _ew_qn[:40])
                            _log_block(_ew_un, _ew_qn, _ew_cid, _ew_sd, _entry_price,
                                       "match_full", "$%.0f/$%.0f invested" % (_ew_match_inv, config.MAX_PER_MATCH), "event_wait")
                            _ew_expired.append(_ew_cid)
                            continue

                # MAX_PER_EVENT check (DB query includes recently closed)
                if config.MAX_PER_EVENT > 0:
                    _ew_evt = td.get("event_slug", "") or ""
                    if _ew_evt:
                        _ew_evt_inv = db.get_invested_for_event(_ew_evt)
                        _ew_evt_rem = config.MAX_PER_EVENT - _ew_evt_inv
                        if _ew_evt_rem < MIN_TRADE_SIZE:
                            logger.info("[EVENT-WAIT] Event full $%.0f/$%.0f, skipping: %s",
                                        _ew_evt_inv, config.MAX_PER_EVENT, _ew_qn[:40])
                            _log_block(_ew_un, _ew_qn, _ew_cid, _ew_sd, _entry_price,
                                       "event_full", "$%.0f/$%.0f invested" % (_ew_evt_inv, config.MAX_PER_EVENT), "event_wait")
                            _ew_expired.append(_ew_cid)
                            continue
                        _ew_budget = min(_ew_budget, _ew_evt_rem) if _ew_budget is not None else _ew_evt_rem

                # Exposure check
                _ew_tpct = _EXPOSURE_MAP.get(td["wallet_username"].lower(), config.MAX_EXPOSURE_PER_TRADER)
                _ew_max_exp = portfolio_value * _ew_tpct
                _ew_t_inv = db.get_trader_exposure(td["wallet_address"])
                _ew_exp_rem = _ew_max_exp - _ew_t_inv
                if _ew_exp_rem < MIN_TRADE_SIZE:
                    logger.info("[EVENT-WAIT] Trader exposure $%.0f >= max $%.0f, skipping: %s",
                                _ew_t_inv, _ew_max_exp, _ew_qn[:40])
                    _log_block(_ew_un, _ew_qn, _ew_cid, _ew_sd, _entry_price,
                               "exposure_limit", "$%.0f >= $%.0f max" % (_ew_t_inv, _ew_max_exp), "event_wait")
                    _ew_expired.append(_ew_cid)
                    continue

                _ew_size = _calculate_position_size(_entry_price, balance,
                                                    trader_ratio=_ew.get("trader_ratio", 1.0),
                                                    portfolio_value=portfolio_value, trader_name=td["wallet_username"])
                # Apply all size caps
                if _ew_budget is not None and _ew_size > _ew_budget:
                    _ew_size = round(_ew_budget, 2)
                if _ew_size > _ew_exp_rem:
                    _ew_size = round(_ew_exp_rem, 2)
                if _ew_cid:
                    _ew_existing = sum(ot["size"] for ot in _cached_open_trades if ot.get("condition_id") == _ew_cid)
                    _ew_pos_rem = MAX_POSITION_SIZE - _ew_existing
                    if _ew_size > _ew_pos_rem:
                        _ew_size = round(_ew_pos_rem, 2)
                if _ew_size >= MIN_TRADE_SIZE and balance > _ew_size:
                    with _buy_lock:
                        if _ew_cid and db.count_copies_for_market(td["wallet_address"], _ew_cid) >= config.MAX_COPIES_PER_MARKET:
                            _ew_expired.append(_ew_cid)
                            continue
                        if LIVE_MODE and _ew_cid:
                            from bot.order_executor import get_wallet_balance as _gwb_ew
                            if _gwb_ew() < _ew_size:
                                continue
                            order_resp = buy_shares(_ew_cid, td["side"], _ew_size, _entry_price)
                            if not order_resp:
                                continue
                            _apply_fill_details(td, order_resp, _ew_size, _entry_price)
                            _ew_size = td["size"]
                        td["size"] = _ew_size
                        trade_id = db.create_copy_trade(td)
                    if trade_id:
                        new_trades += 1
                        balance -= _ew_size
                        _cached_open_trades.append(td)
                        _drift_info = " (drift %+.0f%%)" % (_drift_pct * 100) if abs(_drift_pct) > 0.01 else ""
                        logger.info("[EVENT-WAIT] Trade #%d fired (event in %.1fh): %s @ %dc | $%.2f%s",
                                    trade_id, hours_until, td["market_question"][:40],
                                    round(_entry_price * 100), _ew_size, _drift_info)
                        db.log_activity("buy", "BUY", "Copied position from %s (event wait)" % td["wallet_username"],
                                        "#%d %s @ %dc — $%.2f" % (trade_id, td["market_question"][:40],
                                        round(_entry_price * 100), _ew_size))
                _ew_expired.append(_ew_cid)
            # Event already started or passed → discard
            elif hours_until <= 0:
                _ew_expired.append(_ew_cid)
            # Queued too long (>24h) → discard
            elif _ew_now - _ew["queued_at"] > config.EVENT_WAIT_MAX_SECS:
                _ew_expired.append(_ew_cid)
        for _ek in _ew_expired:
            _event_wait_queue.pop(_ek, None)

    # Pending-Buy-Queue abarbeiten
    new_trades += _process_pending_buys(balance, total_invested)

    # Hedge-Wait Queue: fire trades that waited long enough without hedge
    if _hedge_queue:
        now_ts = _time.time()
        expired_keys = []
        for ekey, q in list(_hedge_queue.items()):
            # Per-trade wait time (stored in the trade data)
            wait = max(td.get("wait_secs", 60) for td in q["sides"].values())
            if now_ts - q["queued_at"] >= wait:
                # No hedge detected in time → this is a conviction trade, execute it
                for side, td in q["sides"].items():
                    logger.info("[HEDGE-WAIT] No hedge after %ds → executing: %s %s",
                                wait, side, td["question"][:40])
                    _orig_hw_price = td["entry_price"]
                    _hw_qn = td["question"]
                    _hw_sd = side
                    _hw_un = td["username"]
                    _hw_cid = td["cid"]
                    # Drift check: get live price and reject if moved too far
                    entry_price = _orig_hw_price
                    if _hw_cid and price_tracker.is_connected:
                        _hw_live = price_tracker.get_price(_hw_cid, side)
                        if _hw_live and _hw_live > 0:
                            entry_price = _hw_live
                    if _orig_hw_price > 0:
                        if _orig_hw_price < 0.20:
                            _hw_max_drift = config.QUEUE_DRIFT_LOTTERY
                        elif _orig_hw_price < 0.40:
                            _hw_max_drift = config.QUEUE_DRIFT_UNDERDOG
                        elif _orig_hw_price < 0.60:
                            _hw_max_drift = config.QUEUE_DRIFT_COINFLIP
                        else:
                            _hw_max_drift = config.QUEUE_DRIFT_FAVORITE
                        _hw_drift = (entry_price - _orig_hw_price) / _orig_hw_price
                        if _hw_drift > _hw_max_drift:
                            logger.info("[HEDGE-WAIT] SKIP drift %.0f%% > %.0f%% max (%.0fc->%.0fc): %s",
                                        _hw_drift * 100, _hw_max_drift * 100,
                                        _orig_hw_price * 100, entry_price * 100, _hw_qn[:40])
                            _log_block(_hw_un, _hw_qn, _hw_cid, _hw_sd, entry_price,
                                       "queue_drift", "%.0f%% > %.0f%% max" % (_hw_drift*100, _hw_max_drift*100), "hedge_wait")
                            continue
                    # No-rebuy check
                    if _hw_cid and config.NO_REBUY_MINUTES > 0:
                        try:
                            from database.db import get_connection as _gc_hw
                            with _gc_hw() as _rc_hw:
                                _was_closed_hw = _rc_hw.execute(
                                    "SELECT id FROM copy_trades WHERE condition_id=? AND status='closed' "
                                    "AND closed_at > datetime('now', '-' || ? || ' minutes', 'localtime')", (_hw_cid, str(config.NO_REBUY_MINUTES))
                                ).fetchone()
                                if _was_closed_hw:
                                    _log_block(_hw_un, _hw_qn, _hw_cid, _hw_sd, entry_price,
                                               "no_rebuy", "closed within %dmin" % config.NO_REBUY_MINUTES, "hedge_wait")
                                    continue
                        except Exception as _nre_hw:
                            logger.warning("[NO_REBUY] DB check failed, skipping conservatively: %s", _nre_hw)
                            continue
                    # Category blacklist
                    if _is_category_blocked(_hw_un, _hw_qn):
                        _log_block(_hw_un, _hw_qn, _hw_cid, _hw_sd, entry_price,
                                   "category_blacklist", "category '%s' blocked" % _detect_category(_hw_qn), "hedge_wait")
                        continue
                    # MAX_COPIES check: activity scan may have already copied this market
                    if _hw_cid and db.count_copies_for_market(td["address"], _hw_cid) >= config.MAX_COPIES_PER_MARKET:
                        logger.info("[HEDGE-WAIT] Already copied (activity scan was faster), skipping: %s", _hw_qn[:40])
                        _log_block(_hw_un, _hw_qn, _hw_cid, _hw_sd, entry_price,
                                   "max_copies", "already copied (activity faster)", "hedge_wait")
                        continue
                    # Cross-trader duplicate check
                    if _hw_cid and db.is_market_already_open(_hw_cid, from_wallet=td["address"]):
                        _log_block(_hw_un, _hw_qn, _hw_cid, _hw_sd, entry_price,
                                   "cross_trader_dupe", "market open from another trader", "hedge_wait")
                        continue
                    # MAX_PER_MATCH check (with size cap)
                    _hw_budget = None
                    if config.MAX_PER_MATCH > 0:
                        _hw_evt_slug = td["trade_data"].get("event_slug", "") or ""
                        if _hw_evt_slug:
                            _hw_match_inv = db.get_invested_for_event(_hw_evt_slug)
                            _hw_budget = config.MAX_PER_MATCH - _hw_match_inv
                            if _hw_budget < MIN_TRADE_SIZE:
                                _log_block(_hw_un, _hw_qn, _hw_cid, _hw_sd, entry_price,
                                           "match_full", "$%.0f/$%.0f invested" % (_hw_match_inv, config.MAX_PER_MATCH), "hedge_wait")
                                continue
                    # Check trader exposure limit (with size cap)
                    _max_t = portfolio_value * _EXPOSURE_MAP.get(_hw_un.lower(), config.MAX_EXPOSURE_PER_TRADER)
                    _t_inv = db.get_trader_exposure(td["address"])
                    _hw_exp_rem = _max_t - _t_inv
                    if _hw_exp_rem < MIN_TRADE_SIZE:
                        logger.info("[HEDGE-WAIT] Trader exposure $%.0f >= max $%.0f, skipping: %s", _t_inv, _max_t, _hw_qn[:40])
                        _log_block(_hw_un, _hw_qn, _hw_cid, _hw_sd, entry_price,
                                   "exposure_limit", "$%.0f >= $%.0f max" % (_t_inv, _max_t), "hedge_wait")
                        continue
                    # Max per event check (with size cap)
                    if config.MAX_PER_EVENT > 0:
                        _hw_evt = td["trade_data"].get("event_slug", "") or ""
                        if _hw_evt:
                            _hw_evt_inv = db.get_invested_for_event(_hw_evt)
                            _hw_evt_rem = config.MAX_PER_EVENT - _hw_evt_inv
                            if _hw_evt_rem < MIN_TRADE_SIZE:
                                logger.info("[HEDGE-WAIT] Event exposure $%.0f >= max $%.0f, skipping: %s",
                                            _hw_evt_inv, config.MAX_PER_EVENT, _hw_qn[:40])
                                _log_block(_hw_un, _hw_qn, _hw_cid, _hw_sd, entry_price,
                                           "event_full", "$%.0f/$%.0f invested" % (_hw_evt_inv, config.MAX_PER_EVENT), "hedge_wait")
                                continue
                            _hw_budget = min(_hw_budget, _hw_evt_rem) if _hw_budget is not None else _hw_evt_rem
                    # Event timing check: skip if event > MAX_HOURS away
                    if config.MAX_HOURS_BEFORE_EVENT > 0:
                        _hw_eslug = td["trade_data"].get("event_slug", "") or td["trade_data"].get("market_slug", "")
                        if _hw_eslug:
                            try:
                                _hw_ev_r = requests.get("https://gamma-api.polymarket.com/events",
                                                        params={"slug": _hw_eslug.split("/")[-1]}, timeout=config.GAMMA_API_TIMEOUT)
                                if _hw_ev_r.ok and _hw_ev_r.json():
                                    _hw_ev = _hw_ev_r.json()[0] if isinstance(_hw_ev_r.json(), list) else _hw_ev_r.json()
                                    _hw_st = _hw_ev.get("startTime", "")
                                    if _hw_st:
                                        from datetime import datetime as _dt, timezone as _tz
                                        _hw_start = _dt.fromisoformat(_hw_st.replace("Z", "+00:00"))
                                        _hw_hours = (_hw_start - _dt.now(_tz.utc)).total_seconds() / 3600
                                        if _hw_hours > config.MAX_HOURS_BEFORE_EVENT:
                                            logger.info("[HEDGE-WAIT] Event in %.1fh > %.1fh max, skipping: %s",
                                                        _hw_hours, config.MAX_HOURS_BEFORE_EVENT, _hw_qn[:40])
                                            _log_block(_hw_un, _hw_qn, _hw_cid, _hw_sd, entry_price,
                                                       "event_timing", "event in %.1fh > %.1fh max" % (_hw_hours, config.MAX_HOURS_BEFORE_EVENT), "hedge_wait")
                                            continue
                            except Exception:
                                pass
                    size = _calculate_position_size(entry_price, cash, td.get("trader_ratio", 1.0),
                                                    portfolio_value=portfolio_value, trader_name=td["username"])
                    # Apply all size caps
                    if _hw_budget is not None and size > _hw_budget:
                        size = round(_hw_budget, 2)
                    if size > _hw_exp_rem:
                        size = round(_hw_exp_rem, 2)
                    if td["cid"]:
                        _hw_existing = sum(ot["size"] for ot in _cached_open_trades if ot.get("condition_id") == td["cid"])
                        _hw_pos_rem = MAX_POSITION_SIZE - _hw_existing
                        if size > _hw_pos_rem:
                            size = round(_hw_pos_rem, 2)
                    if size < MIN_TRADE_SIZE or cash < size:
                        continue
                    trade = {
                        "wallet_address": td["address"],
                        "wallet_username": td["username"],
                        "market_question": td["question"],
                        "market_slug": td["trade_data"].get("market_slug", ""),
                        "event_slug": td["trade_data"].get("event_slug", ""),
                        "side": side,
                        "entry_price": round(entry_price, 4),
                        "size": size,
                        "end_date": td["trade_data"].get("end_date", ""),
                        "outcome_label": td["trade_data"].get("outcome_label", ""),
                        "condition_id": td["cid"],
                        "category": _detect_category(td["question"]),
                    }
                    with _buy_lock:
                        if td["cid"] and db.count_copies_for_market(td["address"], td["cid"]) >= config.MAX_COPIES_PER_MARKET:
                            continue
                        if LIVE_MODE and td["cid"]:
                            from bot.order_executor import get_wallet_balance as _gwb2
                            real_bal = _gwb2()
                            if real_bal < size:
                                continue
                            order_resp = buy_shares(td["cid"], side, size, entry_price)
                            if not order_resp:
                                continue
                            _apply_fill_details(trade, order_resp, size, entry_price)
                            size = trade["size"]
                        trade_id = db.create_copy_trade(trade)
                    if trade_id:
                        new_trades += 1
                        cash -= size
                        _cached_open_trades.append(trade)
                        db.log_activity("buy", "BUY", "Copied position from %s (conviction)" % td["username"],
                                        "#%d %s @ %dc — $%.2f" % (trade_id, td["question"][:40], entry_price*100, size))
                        logger.info("[HEDGE-WAIT] CONVICTION TRADE #%d: %s @ %dc | $%.2f",
                                    trade_id, td["question"][:40], entry_price*100, size)
                expired_keys.append(ekey)
        for k in expired_keys:
            _hedge_queue.pop(k, None)

    # Clean hedge queue: remove stale entries (>10min) and markets already copied
    _hq_now = _time.time()
    for _hk in list(_hedge_queue.keys()):
        if _hq_now - _hedge_queue[_hk]["queued_at"] > 600:
            logger.info("[HEDGE-WAIT] Removed stale entry (>10min): %s", _hk[:20])
            del _hedge_queue[_hk]
            continue
    for _hk in list(_hedge_queue.keys()):
        _hq_entry = _hedge_queue[_hk]
        _hq_first = list(_hq_entry["sides"].values())[0]
        _hq_addr = _hq_first.get("address", "")
        _hq_cid = _hq_first.get("cid", _hk)
        if _hq_cid and db.count_copies_for_market(_hq_addr, _hq_cid) >= config.MAX_COPIES_PER_MARKET:
            logger.info("[HEDGE-WAIT] Removed from queue (already copied): %s", _hq_first.get("question", "")[:40])
            del _hedge_queue[_hk]

    for wallet in followed:
        address = wallet["address"]
        username = wallet["username"] or address[:12]

        # Baseline schon oben erledigt — skip falls noch nicht fertig
        if not db.is_wallet_baselined(address):
            continue

        # Domain des Traders (gespeichert in strategy_type)
        trader_domain = (wallet["strategy_type"] or "General") if wallet["strategy_type"] else "General"

        # --- LIVE SCAN: /trades Endpoint → nur echte neue BUYs ---
        scan_cfg = db.get_or_create_scan_config(address)
        last_ts = scan_cfg.get("last_trade_timestamp", 0) or 0

        recent_trades = fetch_wallet_recent_trades(address, limit=config.RECENT_TRADES_LIMIT)
        if not recent_trades:
            logger.info("[SCAN] %s: keine Trades von API", username)
            continue

        # Store ALL trader activity (BUY + SELL) for historical analysis
        try:
            _activity_rows = [{
                "wallet_address": address,
                "trader": username,
                "condition_id": t.get("condition_id", ""),
                "asset": t.get("asset", ""),
                "trade_type": t.get("trade_type", ""),
                "side": t.get("side", ""),
                "price": t.get("price", 0),
                "usdc_size": t.get("usdc_size", 0),
                "market_question": t.get("market_question", ""),
                "market_slug": t.get("market_slug", ""),
                "event_slug": t.get("event_slug", ""),
                "category": _detect_category(t.get("market_question", "")),
                "timestamp": t.get("timestamp", 0),
            } for t in recent_trades if t.get("condition_id") and t.get("timestamp", 0) > 0]
            if _activity_rows:
                db.store_trader_activity(_activity_rows)
        except Exception:
            pass  # never let activity logging break the bot

        # Durchschnittliche Trade-Größe des Traders (für proportionales Sizing)
        # Per-trader override via AVG_TRADER_SIZE_MAP, else calculate from recent, else global default
        _ats_override = _AVG_TRADER_SIZE_MAP.get(username.lower())
        if _ats_override:
            avg_trader_size = _ats_override
        else:
            buy_sizes = [t.get("usdc_size", 0) for t in recent_trades if t["trade_type"] == "BUY" and t.get("usdc_size", 0) > 0]
            avg_trader_size = (sum(buy_sizes) / len(buy_sizes)) if buy_sizes else config.DEFAULT_AVG_TRADER_SIZE

        max_ts = max(t["timestamp"] for t in recent_trades)
        _last_processed_ts = last_ts  # tracks the last trade we actually processed

        # Only BUY trades that happened AFTER our last seen timestamp
        new_buy_trades = [
            t for t in recent_trades
            if t["trade_type"] == "BUY" and t["timestamp"] > last_ts
        ]

        all_buys = [t for t in recent_trades if t["trade_type"] == "BUY"]
        logger.info("[SCAN] %s: %d BUYs gesamt, %d neu (nach ts=%d)",
                    username, len(all_buys), len(new_buy_trades), last_ts)

        # === FAST SELL DETECTION: RN1 SELLs sofort erkennen (alle 5s) ===
        _already_sold_cids = set()  # prevent sell spam on same condition_id
        new_sells = [t for t in recent_trades if t["trade_type"] == "SELL" and t["timestamp"] > last_ts] if config.COPY_SELLS else []
        if new_sells:
            open_by_cid = {t["condition_id"]: t for t in _cached_open_trades if t["condition_id"] and t["wallet_address"] == address}
            for sell in new_sells:
                _last_processed_ts = max(_last_processed_ts, sell.get("timestamp", 0))
                sell_cid = sell.get("condition_id", "")
                if not sell_cid or sell_cid in _already_sold_cids:
                    continue
                if sell_cid in open_by_cid:
                    our_trade = open_by_cid[sell_cid]
                    sell_price = sell.get("price", 0)
                    if not sell_price:
                        sell_price = our_trade["current_price"] or _get_entry_price(our_trade)
                    pnl, shares = _calc_pnl(our_trade, sell_price)
                    # Sell FIRST, then close DB (prevents orphaned positions)
                    sell_resp = None
                    if LIVE_MODE and sell_cid:
                        sell_resp = sell_shares(sell_cid, our_trade["side"], sell_price)
                        if not sell_resp:
                            logger.warning("[FAST-SELL] Sell failed, keeping position open: %s", our_trade["market_question"][:40])
                            _already_sold_cids.add(sell_cid)
                            continue
                    # Atomic DB close — only if WE are the one closing (prevents double close)
                    if not db.close_copy_trade(our_trade["id"], pnl, close_price=sell_price):
                        logger.info("[FAST-SELL] Trade #%d already closed by another path", our_trade["id"])
                        _already_sold_cids.add(sell_cid)
                        continue
                    if sell_resp:
                        _correct_sell_pnl(our_trade, sell_resp, our_trade["id"])
                    logger.info("[FAST-SELL] #%d CLOSED (trader sold): PnL=$%.2f @ %.0fc | %s",
                                our_trade["id"], pnl, sell_price * 100, our_trade["market_question"][:40])
                    db.log_activity("sell", "WIN" if pnl > 0 else "LOSS",
                                    "Position closed — sold",
                                    "#%d %s — P&L $%+.2f" % (our_trade["id"], our_trade["market_question"][:40], pnl), pnl)
                    _already_sold_cids.add(sell_cid)
                    # Close ALL other open trades on same condition_id — sell each one too
                    _other_on_cid = [t for t in _cached_open_trades if t.get("condition_id") == sell_cid and t.get("id") != our_trade["id"]]
                    for _ot in _other_on_cid:
                        _ot_pnl, _ = _calc_pnl(_ot, sell_price)
                        # sell_shares already sold all shares for this token, just close DB
                        db.close_copy_trade(_ot["id"], _ot_pnl, close_price=sell_price)
                        logger.info("[FAST-SELL] #%d also closed (same market): PnL=$%.2f", _ot["id"], _ot_pnl)
                    try:
                        from dashboard.app import broadcast_event
                        broadcast_event("trade_closed", {
                            "id": our_trade["id"], "trader": username,
                            "market": our_trade["market_question"][:60],
                            "pnl": round(pnl, 2), "price": round(sell_price * 100),
                            "size": our_trade.get("size", 0),
                        })
                    except Exception:
                        pass

        # Position-Diff: Fallback für Trades die der Activity-Feed verpasst hat
        if config.POSITION_DIFF_ENABLED:
            new_trades += _position_diff_scan(address, username, balance, total_invested, portfolio_value=portfolio_value)

        for t in new_buy_trades:
            _last_processed_ts = max(_last_processed_ts, t["timestamp"])
            cid = t.get("condition_id", "")
            question = t["market_question"]
            logger.info("[NEW] %s: %s | $%.2f | %dc | cid=%s",
                        username, question[:40], t.get("usdc_size", 0),
                        round(t.get("price", 0) * 100), (cid or "?")[:16])

            if not question:
                logger.info("[SKIP] Empty question")
                continue

            # No-rebuy: don't re-enter a market we recently closed/sold
            if cid and config.NO_REBUY_MINUTES > 0:
                try:
                    from database.db import get_connection as _gc
                    with _gc() as _rc:
                        _was_closed = _rc.execute(
                            "SELECT id FROM copy_trades WHERE condition_id=? AND status='closed' "
                            "AND closed_at > datetime('now', '-' || ? || ' minutes', 'localtime')", (cid, str(config.NO_REBUY_MINUTES))
                        ).fetchone()
                        if _was_closed:
                            logger.info("[SKIP] Recently closed (no-rebuy %dmin): %s",
                                        config.NO_REBUY_MINUTES, question[:40])
                            _log_block(username, question, cid, t.get("side", ""), t.get("price", 0),
                                       "no_rebuy", "closed within %dmin" % config.NO_REBUY_MINUTES, "activity")
                            continue
                except Exception as _nre:
                    logger.warning("[NO_REBUY] DB check failed, skipping trade conservatively: %s | %s", _nre, question[:40])
                    continue

            # === RN1 SMART-FILTER ===
            # 0) Category blacklist: skip blocked categories for this trader
            if _is_category_blocked(username, question):
                _cat = _detect_category(question)
                logger.info("[FILTER] Category '%s' blocked for %s: %s", _cat, username, question[:40])
                _log_block(username, question, cid, t.get("side", ""), t.get("price", 0),
                           "category_blacklist", "category '%s' blocked" % _cat, "activity")
                continue

            # 1) Min Trader USD: per-trader override or global default
            dollar_value = t.get("usdc_size", 0)
            _min_usd = _MIN_TRADER_USD_MAP.get(username.lower(), config.MIN_TRADER_USD)
            if dollar_value < _min_usd:
                logger.info("[FILTER] Size $%.1f < $%.0f: %s",
                            dollar_value, _min_usd, question[:40])
                _log_block(username, question, cid, t.get("side", ""), t.get("price", 0),
                           "min_trader_usd", "$%.1f < $%.0f min" % (dollar_value, _min_usd), "activity")
                continue

            # 2) Conviction ratio: skip low-conviction trades (arb noise filter)
            _min_conv = _MIN_CONVICTION_MAP.get(username.lower(), config.MIN_CONVICTION_RATIO)
            if _min_conv > 0 and avg_trader_size > 0:
                _conv = dollar_value / avg_trader_size
                if _conv < _min_conv:
                    logger.info("[FILTER] Conviction %.1fx < %.1fx min for %s: %s",
                                _conv, _min_conv, username, question[:40])
                    _log_block(username, question, cid, t.get("side", ""), t.get("price", 0),
                               "conviction_ratio", "%.1fx < %.1fx min" % (_conv, _min_conv), "activity")
                    continue

            # 2b) Fee check: log fee info, skip if MAX_FEE_BPS is set and exceeded
            if cid:
                try:
                    from bot.order_executor import get_fee_rate
                    _fee = get_fee_rate(cid, t["side"])
                    t["_fee_bps"] = _fee  # store for later use in trade dict
                    if _fee > 0:
                        logger.info("[FEE] %dbps (%.1f%%) on: %s", _fee, _fee/100, question[:40])
                    if config.MAX_FEE_BPS > 0 and _fee > config.MAX_FEE_BPS:
                        logger.info("[FILTER] Fee %dbps > max %dbps, skipping: %s", _fee, config.MAX_FEE_BPS, question[:40])
                        _log_block(username, question, cid, t.get("side", ""), t.get("price", 0),
                                   "max_fee", "%dbps > %dbps max" % (_fee, config.MAX_FEE_BPS), "activity")
                        continue
                except Exception:
                    pass  # fee lookup failed, don't block trade

            # 3) Preis-Range-Filter: per-trader override via MIN/MAX_ENTRY_PRICE_MAP
            trader_price = t["price"]
            _min_price = _MIN_ENTRY_PRICE_MAP.get(username.lower(), config.MIN_ENTRY_PRICE)
            _max_price = _MAX_ENTRY_PRICE_MAP.get(username.lower(), config.MAX_ENTRY_PRICE)
            if trader_price < _min_price or trader_price > _max_price:
                logger.info("[FILTER] Preis %.0fc ausserhalb Range (%.0f-%.0fc): %s",
                            trader_price * 100, _min_price * 100,
                            _max_price * 100, question[:40])
                _log_block(username, question, cid, t.get("side", ""), trader_price,
                           "price_range", "%.0fc outside %.0f-%.0fc" % (trader_price*100, _min_price*100, _max_price*100), "activity")
                continue

            # 3) Max Kopien pro Markt: nicht X-mal denselben Markt kopieren
            if cid and db.count_copies_for_market(address, cid) >= config.MAX_COPIES_PER_MARKET:
                logger.info("[FILTER] Max copies (%d) for market: %s",
                            config.MAX_COPIES_PER_MARKET, question[:40])
                _log_block(username, question, cid, t.get("side", ""), t.get("price", 0),
                           "max_copies", "max %d copies reached" % config.MAX_COPIES_PER_MARKET, "activity")
                continue

            # === STANDARD-FILTER ===
            # Duplikat-Markt-Check: nicht denselben Markt von 2 Tradern kopieren
            if cid and db.is_market_already_open(cid, from_wallet=address):
                logger.info("[SKIP] Markt bereits offen (anderer Trader): %s", question[:40])
                _log_block(username, question, cid, t.get("side", ""), t.get("price", 0),
                           "cross_trader_dupe", "market open from another trader", "activity")
                continue

            # Hedge-Detection: wenn wir schon eine Seite offen haben, Gegenseite blocken
            if cid:
                existing = [x for x in _cached_open_trades
                            if x["condition_id"] == cid and x["wallet_address"] == address]
                if existing:
                    existing_sides = {x["side"] for x in existing}
                    if t["side"] not in existing_sides:
                        logger.info("[SKIP] Hedge blocked (%s already open, skipping %s): %s",
                                    "/".join(existing_sides), t["side"], question[:40])
                        _log_block(username, question, cid, t.get("side", ""), t.get("price", 0),
                                   "hedge_blocked", "%s open, skipping %s" % ("/".join(existing_sides), t["side"]), "activity")
                        continue

            # Hedge-Wait: hold trade and check if trader buys opposite side
            # Per-trader wait times from HEDGE_WAIT_TRADERS (e.g. "xsaghav:60,RN1:30")
            _hw_map = {}
            for entry in config.HEDGE_WAIT_TRADERS.split(","):
                entry = entry.strip()
                if ":" in entry:
                    parts = entry.split(":", 1)
                    _hw_map[parts[0].strip().lower()] = int(parts[1].strip())
                elif entry:
                    _hw_map[entry.lower()] = config.HEDGE_WAIT_SECS
            trader_name_lower = (wallet["username"] or "").lower()
            hedge_wait_secs = _hw_map.get(trader_name_lower, 0)

            if hedge_wait_secs > 0 and cid:
                # Key = condition_id (exact market, not event-level)
                hedge_key = cid
                if hedge_key in _hedge_queue:
                    q = _hedge_queue[hedge_key]
                    if t["side"] not in q["sides"]:
                        # Trader bought opposite side on SAME market → HEDGE → cancel both
                        logger.info("[HEDGE-WAIT] Hedge detected! %s bought %s + %s → skipping both: %s",
                                    username, list(q["sides"].keys())[0], t["side"], question[:40])
                        del _hedge_queue[hedge_key]
                        continue
                    else:
                        # Same side again (doubling down) → skip duplicate, keep original queued
                        logger.debug("[HEDGE-WAIT] Same side again, ignoring: %s %s", t["side"], question[:40])
                        continue
                else:
                    # First side → queue it, wait for potential hedge
                    _hedge_queue[hedge_key] = {
                        "sides": {t["side"]: {
                            "trade_data": t, "question": question, "cid": cid,
                            "entry_price": trader_price, "address": address, "username": username,
                            "wait_secs": hedge_wait_secs,
                            "trader_ratio": (dollar_value / avg_trader_size) if avg_trader_size > 0 else 1.0,
                        }},
                        "queued_at": _time.time(),
                    }
                    logger.info("[HEDGE-WAIT] Queued trade, waiting %ds for hedge check: %s %s",
                                hedge_wait_secs, t["side"], question[:40])
                    continue

            # Staleness Guard: Trade älter als ENTRY_TRADE_SEC → ignorieren
            trade_age = int(_time.time()) - t["timestamp"]
            if trade_age > ENTRY_TRADE_SEC:
                logger.info("[SKIP] Alter Trade (%ds > %ds): %s",
                            trade_age, ENTRY_TRADE_SEC, question[:40])
                _log_block(username, question, cid, t.get("side", ""), trader_price,
                           "stale_trade", "%ds > %ds max" % (trade_age, ENTRY_TRADE_SEC), "activity")
                continue

            if trader_price <= 0 or trader_price >= 1:
                logger.info("[SKIP] Ungültiger Preis %.4f: %s", trader_price, question[:40])
                continue
            # Live-Preis holen — realistischer als Trader-Preis wenn wir spaeter kopieren
            live_price = None
            if cid and price_tracker.is_connected:
                live_price = price_tracker.get_price(cid, t["side"])
            if live_price is None:
                event_slug_t = t.get("event_slug", "") or t.get("market_slug", "") or ""
                live_price = _fetch_live_price(event_slug_t, question, t["side"], cid)
            # Live-Preis nur nehmen wenn: >= LIVE_PRICE_MIN UND max X% Abweichung vom Trader-Preis
            if live_price is not None and live_price >= config.LIVE_PRICE_MIN and 0 < live_price < 1:
                diff_pct = abs(live_price - trader_price) / trader_price if trader_price > 0 else 0
                if diff_pct <= config.LIVE_PRICE_MAX_DEVIATION:
                    entry_price_raw = live_price
                    if abs(live_price - trader_price) > 0.005:
                        logger.info("[PRICE] Live=%.0fc vs Trader=%.0fc: %s",
                                    live_price * 100, trader_price * 100, question[:40])
                else:
                    entry_price_raw = trader_price
            else:
                entry_price_raw = trader_price

            # Market-Close Guard: kein Trade wenn Markt in < 2 Min schließt
            end_ts = _parse_end_ts(t.get("end_date", ""))
            if end_ts:
                secs_left = end_ts - _time.time()
                if secs_left < TRADE_SEC_FROM_RESOLVE:
                    logger.info("[SKIP] Markt schliesst in %.0fs: %s", secs_left, question[:40])
                    _log_block(username, question, cid, t.get("side", ""), trader_price,
                               "market_closing", "closes in %.0fs" % secs_left, "activity")
                    continue

            # Max market duration: skip if market resolves too far in future
            if config.MAX_MARKET_HOURS > 0 and end_ts:
                _hours_until_end = (end_ts - _time.time()) / 3600
                if _hours_until_end > config.MAX_MARKET_HOURS:
                    _log_block(username, question, cid, t.get("side", ""), trader_price, "market_too_long",
                               "resolves in %.0fh > %.0fh max" % (_hours_until_end, config.MAX_MARKET_HOURS), "activity")
                    continue


            # Spread-Filter: illiquide Märkte überspringen (WebSocket-Daten, falls verfügbar)
            if cid and price_tracker.is_connected:
                spread = price_tracker.get_spread(cid, t["side"])
                if spread is not None and spread > MAX_SPREAD:
                    logger.info("[SKIP] Spread zu gross (%.1f%% > %.0f%%): %s",
                                spread * 100, MAX_SPREAD * 100, question[:40])
                    _log_block(username, question, cid, t.get("side", ""), trader_price,
                               "spread", "%.1f%% > %.0f%% max" % (spread*100, MAX_SPREAD*100), "activity")
                    continue

            # Pending-Buy-Queue: wenn Preis unter BUY_THRESHOLD → warten statt sofort kaufen
            if BUY_THRESHOLD > 0 and entry_price_raw < BUY_THRESHOLD and cid and cid not in _pending_buys:
                _pending_buys[cid] = {
                    "queued_at": _time.time(),
                    "trader_ratio": (dollar_value / avg_trader_size) if avg_trader_size > 0 else 1.0,
                    "trade_data": {
                        "wallet_address": address,
                        "wallet_username": username,
                        "market_question": question,
                        "market_slug": t.get("market_slug", ""),
                        "event_slug": t.get("event_slug", ""),
                        "side": t["side"],
                        "entry_price": entry_price_raw,
                        "size": 0,  # wird beim Feuern berechnet
                        "end_date": t.get("end_date", ""),
                        "outcome_label": t.get("outcome_label", ""),
                        "condition_id": cid,
                        "category": _detect_category(question),
                    },
                }
                logger.info("[PENDING] Queued (%.0fc < BUY_THRESHOLD %.0fc): %s",
                            entry_price_raw * 100, BUY_THRESHOLD * 100, question[:40])
                continue

            # Event timing filter: only buy X hours before event starts
            if config.MAX_HOURS_BEFORE_EVENT > 0:
                _event_slug = t.get("event_slug", "") or t.get("market_slug", "")
                if _event_slug:
                    try:
                        _ev_r = requests.get("https://gamma-api.polymarket.com/events",
                                             params={"slug": _event_slug.split("/")[-1]}, timeout=config.GAMMA_API_TIMEOUT)
                        if _ev_r.ok and _ev_r.json():
                            _ev = _ev_r.json()[0] if isinstance(_ev_r.json(), list) else _ev_r.json()
                            _st = _ev.get("startTime", "")
                            if _st:
                                from datetime import datetime as _dt, timezone as _tz
                                _start = _dt.fromisoformat(_st.replace("Z", "+00:00"))
                                _now_utc = _dt.now(_tz.utc)
                                _hours_until = (_start - _now_utc).total_seconds() / 3600
                                if _hours_until > config.MAX_HOURS_BEFORE_EVENT:
                                    # Enough cash → buy now despite distant event
                                    if config.EVENT_WAIT_MIN_CASH > 0 and cash > config.EVENT_WAIT_MIN_CASH:
                                        logger.info("[EVENT-WAIT] Event in %.1fh but cash $%.0f > $%.0f — buying now: %s",
                                                    _hours_until, cash, config.EVENT_WAIT_MIN_CASH, question[:40])
                                    else:
                                        # Low cash (or always-queue mode) → queue for later
                                        if cid and cid not in _event_wait_queue:
                                            _event_wait_queue[cid] = {
                                                "trade_data": {
                                                    "wallet_address": address,
                                                    "wallet_username": username,
                                                    "market_question": question,
                                                    "market_slug": t.get("market_slug", ""),
                                                    "event_slug": t.get("event_slug", ""),
                                                    "side": t["side"],
                                                    "entry_price": trader_price,
                                                    "size": 0,
                                                    "end_date": t.get("end_date", ""),
                                                    "outcome_label": t.get("outcome_label", ""),
                                                    "condition_id": cid,
                                                    "category": _detect_category(question),
                                                },
                                                "event_start_ts": _start.timestamp(),
                                                "queued_at": _time.time(),
                                                "trader_ratio": (dollar_value / avg_trader_size) if avg_trader_size > 0 else 1.0,
                                            }
                                            logger.info("[EVENT-WAIT] Queued (event in %.1fh, cash $%.0f < $%.0f): %s",
                                                        _hours_until, cash, config.EVENT_WAIT_MIN_CASH, question[:40])
                                        continue
                    except Exception:
                        pass  # API fail → don't block, just skip check

            # Max $ per event (same game/match) — cap size to remaining budget
            # Uses DB query (includes recently closed) to prevent rapid re-entry loops
            _evt_remaining = None
            if config.MAX_PER_EVENT > 0:
                _evt = t.get("event_slug", "") or ""
                if _evt:
                    _evt_invested = db.get_invested_for_event(_evt)
                    _evt_remaining = config.MAX_PER_EVENT - _evt_invested
                    if _evt_remaining < MIN_TRADE_SIZE:
                        logger.info("[SKIP] Event full $%.0f/$%.0f: %s",
                                    _evt_invested, config.MAX_PER_EVENT, question[:40])
                        _log_block(username, question, cid, t.get("side", ""), trader_price,
                                   "event_full", "$%.0f/$%.0f invested" % (_evt_invested, config.MAX_PER_EVENT), "activity")
                        continue

            # Max $ per match (groups Map 1 + Map 2 + BO3 as one match)
            if config.MAX_PER_MATCH > 0:
                _mkey = _match_key(question)
                if _mkey and len(_mkey) > 3:
                    # Use _cached_open_trades for match key (needs Python regex, can't do in SQL)
                    # but also add recently closed from DB via event_slug as backup
                    _match_invested = sum(
                        ot["size"] for ot in _cached_open_trades
                        if _match_key(ot.get("market_question", "")) == _mkey
                    )
                    # Also check DB for recently closed trades on same event (catches rapid re-entry)
                    _evt_for_match = t.get("event_slug", "") or ""
                    if _evt_for_match:
                        _db_evt_inv = db.get_invested_for_event(_evt_for_match)
                        _match_invested = max(_match_invested, _db_evt_inv)
                    _match_remaining = config.MAX_PER_MATCH - _match_invested
                    if _match_remaining < MIN_TRADE_SIZE:
                        logger.info("[SKIP] Match full $%.0f/$%.0f: %s",
                                    _match_invested, config.MAX_PER_MATCH, question[:40])
                        _log_block(username, question, cid, t.get("side", ""), trader_price,
                                   "match_full", "$%.0f/$%.0f invested" % (_match_invested, config.MAX_PER_MATCH), "activity")
                        continue
                    if _evt_remaining is not None:
                        _evt_remaining = min(_evt_remaining, _match_remaining)
                    else:
                        _evt_remaining = _match_remaining

            # Apply realistic entry slippage (+1 tick) — simulates execution delay
            entry_price = round(min(entry_price_raw + ENTRY_SLIPPAGE, config.MAX_ENTRY_PRICE_CAP), 4)

            # Max exposure per trader (per-trader override or default)
            # Uses DB query to include recently closed trades (prevents rapid re-entry loops)
            _trader_pct = _EXPOSURE_MAP.get(username.lower(), config.MAX_EXPOSURE_PER_TRADER)
            max_per_trader = portfolio_value * _trader_pct
            trader_invested = db.get_trader_exposure(address)
            if trader_invested >= max_per_trader:
                logger.info("[SKIP] Trader exposure $%.0f >= max $%.0f (%.0f%%): %s",
                            trader_invested, max_per_trader, _trader_pct * 100, question[:40])
                _log_block(username, question, cid, t.get("side", ""), trader_price,
                           "exposure_limit", "$%.0f >= $%.0f max (%.0f%%)" % (trader_invested, max_per_trader, _trader_pct*100), "activity")
                continue

            # Proportionaler Trader-Multiplikator: dieser Trade vs. Trader-Durchschnitt
            trader_ratio = (dollar_value / avg_trader_size) if avg_trader_size > 0 else 1.0
            size = _calculate_position_size(entry_price, balance, trader_ratio=trader_ratio,
                                                portfolio_value=portfolio_value, trader_name=username)
            # Cap to MAX_POSITION_SIZE across all trades on same condition_id
            if cid:
                _existing_on_market = sum(
                    ot["size"] for ot in _cached_open_trades
                    if ot.get("condition_id", "") == cid
                )
                if _existing_on_market >= MAX_POSITION_SIZE:
                    logger.info("[SKIP] Position cap $%.0f >= max $%.0f: %s",
                                _existing_on_market, MAX_POSITION_SIZE, question[:40])
                    _log_block(username, question, cid, t.get("side", ""), trader_price,
                               "position_cap", "$%.0f >= $%.0f max" % (_existing_on_market, MAX_POSITION_SIZE), "activity")
                    continue
                _remaining_cap = MAX_POSITION_SIZE - _existing_on_market
                if size > _remaining_cap:
                    size = round(_remaining_cap, 2)
                    logger.info("[SIZE] Capped to position limit: $%.2f (existing $%.2f, max $%.0f) | %s",
                                size, _existing_on_market, MAX_POSITION_SIZE, question[:35])

            # Cap to event remaining budget
            if _evt_remaining is not None and size > _evt_remaining:
                size = round(_evt_remaining, 2)
                logger.info("[SIZE] Capped to event budget: $%.2f (remaining $%.2f of $%.0f) | %s",
                            size, _evt_remaining, config.MAX_PER_EVENT, question[:35])
            # Cap to trader exposure remaining budget
            _trader_remaining = max_per_trader - trader_invested
            if _trader_remaining > 0 and size > _trader_remaining:
                size = round(_trader_remaining, 2)
                logger.info("[SIZE] Capped to trader exposure: $%.2f (remaining $%.2f of $%.0f) | %s",
                            size, _trader_remaining, max_per_trader, question[:35])
            if size >= MIN_TRADE_SIZE:
                logger.info("[SIZE] %s: trader=$%.0f avg=$%.0f ratio=%.2f → our=$%.2f | %s",
                            username, dollar_value, avg_trader_size, trader_ratio, size, question[:35])

            cash_left = balance - total_invested - size
            if cash_left < _load_dynamic_floor():
                logger.info("[SKIP] Cash-Floor erreicht (Cash $%.2f < Floor) — ueberspringe: %s",
                            cash_left, question[:40])
                _log_block(username, question, cid, t.get("side", ""), trader_price,
                           "cash_floor", "cash $%.2f < floor" % cash_left, "activity")
                continue


            trade = {
                "wallet_address": address,
                "wallet_username": username,
                "market_question": question,
                "market_slug": t.get("market_slug", ""),
                "event_slug": t.get("event_slug", ""),
                "side": t["side"],
                "entry_price": entry_price,
                "size": size,
                "end_date": t.get("end_date", ""),
                "outcome_label": t.get("outcome_label", ""),
                "condition_id": cid,
                "category": _detect_category(question),
                "fee_bps": t.get("_fee_bps"),
            }

            # Domain-Check: warnen wenn Trade ausserhalb Trader-Spezialisierung
            if trader_domain not in ("General", "Mixed"):
                from bot.wallet_scanner import _detect_domain
                trade_domain = _detect_domain([question])
                if trade_domain not in ("General", "Mixed") and trade_domain != trader_domain:
                    logger.info("[DOMAIN] %s (%s) kopiert %s-Trade: %s",
                                username, trader_domain, trade_domain, question[:40])

            # === TRADE SCORER ===
            try:
                _score_result = score_trade(
                    trader_name=username,
                    condition_id=cid,
                    side=t["side"],
                    entry_price=entry_price,
                    market_question=question,
                    category=trade["category"],
                    event_slug=t.get("event_slug", ""),
                    trader_size_usd=t.get("usdc_size", 0),
                    spread=0.03,
                    hours_until_event=12,
                )
            except Exception as _se:
                logger.warning("[SCORE] Scorer error, defaulting to EXECUTE: %s", _se)
                _score_result = {"action": "EXECUTE", "score": 50, "components": {}, "reason": "scorer_error"}
            if _score_result["action"] == "BLOCK":
                _log_block(username, question, cid, t["side"], entry_price,
                           "score_block", _score_result["reason"], "activity")
                continue
            if _score_result["action"] == "QUEUE":
                _pending_buys[cid] = {
                    "trade_data": trade, "queued_at": _time.time(),
                    "extra_wait": int(PENDING_BUY_MIN_SECS * 0.5),
                    "score": _score_result["score"],
                }
                logger.info("[SCORE-QUEUE] %s queued (score=%d): %s",
                            username, _score_result["score"], question[:40])
                continue
            if _score_result["action"] == "BOOST":
                from bot.kelly import get_kelly_multiplier
                _kelly_m = get_kelly_multiplier(username)
                size = round(size * _kelly_m, 2)
                size = min(size, MAX_POSITION_SIZE, balance - total_invested - _load_dynamic_floor())
                if size < MIN_TRADE_SIZE:
                    continue
                trade["size"] = size
                logger.info("[SCORE-BOOST] %s boosted x%.2f (score=%d): %s",
                            username, _kelly_m, _score_result["score"], question[:40])
            # LIVE MODE: Echte Order auf Polymarket platzieren
            # Lock prevents race conditions between concurrent buy attempts
            with _buy_lock:
                # Re-check limits under lock (another thread may have bought in the meantime)
                if cid and db.count_copies_for_market(address, cid) >= config.MAX_COPIES_PER_MARKET:
                    logger.info("[LOCK] Max copies reached (concurrent): %s", question[:40])
                    continue

                if LIVE_MODE and cid:
                    # Echte USDC-Balance prüfen bevor wir ordern
                    from bot.order_executor import get_wallet_balance
                    real_balance = get_wallet_balance()
                    if real_balance < size:
                        logger.warning("[LIVE] Nicht genug USDC ($%.2f < $%.2f): %s",
                                       real_balance, size, question[:40])
                        continue
                    # Liquidity check before buying
                    try:
                        from bot.liquidity_check import check_liquidity
                        if not check_liquidity(cid, t["side"], size):
                            _log_block(username, question, cid, t["side"], entry_price,
                                       "low_liquidity", "Orderbook too thin for $%.2f" % size, "liquidity")
                            continue
                    except Exception:
                        pass  # dont block on liquidity check errors
                    order_resp = buy_shares(cid, t["side"], size, entry_price)
                    if not order_resp:
                        logger.warning("[LIVE] Order fehlgeschlagen — ueberspringe: %s", question[:40])
                        continue
                    # Apply verified fill details (actual price, size, shares)
                    _apply_fill_details(trade, order_resp, size, entry_price)
                    size = trade["size"]
                    logger.info("[LIVE] BUY FILLED: $%.2f (eff. %.0fc) | planned $%.2f @ %.0fc | %s",
                                trade["actual_size"], (trade.get("actual_entry_price") or entry_price) * 100,
                                size, entry_price * 100, question[:40])

                trade_id = db.create_copy_trade(trade)
            if trade_id:
                new_trades += 1
                total_invested += size
                balance -= size  # Update balance to prevent over-investment
                _cached_open_trades.append(trade)

                if cid:
                    price_tracker.subscribe_condition(cid)

                logger.info(
                    "COPY TRADE #%d: %s | %s @ %dc | $%.2f",
                    trade_id, username, question[:50],
                    round(entry_price * 100), size,
                )
                # Activity Log + Dashboard Notification
                try:
                    _cash_after = cash - total_invested
                    db.log_activity("buy", "BUY", "Copied position from %s" % username,
                                    "#%d %s @ %dc — $%.2f (Cash: $%.0f)" % (trade_id, question[:40], round(entry_price*100), size, _cash_after))
                    from dashboard.app import broadcast_event
                    broadcast_event("new_trade", {
                        "id": trade_id, "trader": username,
                        "market": question[:60], "side": t["side"],
                        "price": round(entry_price * 100), "size": round(size, 2),
                        "live": LIVE_MODE,
                    })
                except Exception:
                    pass
                if new_trades >= MAX_TRADES_PER_SCAN:
                    logger.info("[SCAN] MAX_TRADES_PER_SCAN (%d) erreicht — naechster Scan.", MAX_TRADES_PER_SCAN)
                    break  # inner loop break

        # Update timestamp to last processed trade (not max_ts!)
        # If MAX_TRADES_PER_SCAN cut off some trades, they'll reappear next scan
        if _last_processed_ts > last_ts:
            db.set_last_trade_timestamp(address, _last_processed_ts)

        if new_trades >= MAX_TRADES_PER_SCAN:
            break  # outer wallet loop break

    logger.info("[DONE] Scan complete. %d new trades copied.", new_trades)
    return new_trades


MISS_COUNT_TO_CLOSE = config.MISS_COUNT_TO_CLOSE


def _fetch_live_price(event_slug: str, market_question: str, side: str, condition_id: str = "") -> float | None:
    """Holt den aktuellen Live-Preis: zuerst WebSocket-Cache, dann Gamma API als Fallback."""
    import json

    # 1. WebSocket cache (instant, kein API-Call)
    if condition_id and price_tracker.is_connected:
        ws_price = price_tracker.get_price(condition_id, side)
        if ws_price is not None:
            return ws_price

    # 2. Fallback: Gamma REST API (mit Backoff + Circuit Breaker)
    try:
        resp = _api_get(
            "https://gamma-api.polymarket.com/events",
            params={"slug": event_slug},
        )
        if resp is None or not resp.json():
            return None

        event = resp.json()[0]
        for m in event.get("markets", []):
            mq = m.get("question", "")
            title = m.get("groupItemTitle", "")
            if mq == market_question or title == market_question:
                outcomes = m.get("outcomes", "[]")
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                prices = m.get("outcomePrices", "[]")
                if isinstance(prices, str):
                    prices = json.loads(prices)
                if not prices:
                    return None

                if side in outcomes:
                    # Exakter Match — z.B. "Lakers" in ["Lakers", "Celtics"]
                    return float(prices[outcomes.index(side)])
                elif side == "YES":
                    return float(prices[0])
                elif side == "NO":
                    return float(prices[1]) if len(prices) > 1 else 1 - float(prices[0])
                else:
                    # Case-insensitive Fallback — z.B. "lakers" vs "Lakers"
                    side_lower = side.lower()
                    for i, o in enumerate(outcomes):
                        if o.lower() == side_lower and i < len(prices):
                            return float(prices[i])
                    # Nicht gefunden — None zurueckgeben damit Fallback greift
                    return None
    except Exception as e:
        logger.debug("Gamma API error for '%s': %s", market_question[:30], e)
    return None


def update_copy_positions():
    """Update prices and close copy trades by comparing open AND closed positions.

    LOGIC PER WALLET (grouped to minimize API calls):
    1. Fetch current open positions for wallet
    2. Fetch closed positions with limit = last_closed_count + 100
    3. For each copy trade:
       a. Found in closed positions → close immediately (trader closed it)
       b. Found in open positions + redeemable → close immediately (market resolved)
       c. Found in open positions → update price
       d. Not found anywhere → increment miss counter (fallback after 20 misses)
    """
    from collections import defaultdict

    open_trades = [dict(t) for t in db.get_open_copy_trades()]
    if not open_trades:
        return

    # Subscribe all open trades to WebSocket price feed (no-op if already subscribed)
    for t in open_trades:
        if t["condition_id"]:
            price_tracker.subscribe_condition(t["condition_id"])

    logger.debug("Updating %d open copy trades...", len(open_trades))

    # Group trades by wallet to fetch positions once per wallet
    trades_by_wallet = defaultdict(list)
    for trade in open_trades:
        trades_by_wallet[trade["wallet_address"]].append(trade)

    for wallet_address, wallet_trades in trades_by_wallet.items():
        try:
            # Get dynamic closed-position fetch limit: last_known + 100
            scan_cfg = db.get_or_create_scan_config(wallet_address)
            closed_limit = scan_cfg.get("last_closed_count", 0) + 100

            # Fetch open and closed positions ONCE per wallet
            open_positions = fetch_wallet_positions(wallet_address)
            closed_positions = fetch_wallet_closed_positions(wallet_address, limit=closed_limit)

            # Update the stored closed count for next cycle
            db.update_closed_count(wallet_address, len(closed_positions))

            # Save closed positions for matching
            if closed_positions:
                db.save_closed_positions(wallet_address, closed_positions)

            # Build fast-lookup sets
            open_by_cid = {p.get("condition_id", ""): p for p in open_positions if p.get("condition_id")}
            closed_cids = {p.get("condition_id", "") for p in closed_positions if p.get("condition_id")}

            for trade in wallet_trades:
                try:
                    trade_cid = trade["condition_id"] or ""

                    # --- PRIMARY: Check if still open (hat Vorrang vor closed!) ---
                    open_pos = open_by_cid.get(trade_cid) if trade_cid else None

                    # Fallback match by market_question for old trades without condition_id
                    if open_pos is None and not trade_cid:
                        open_pos = next((p for p in open_positions if p["market_question"] == trade["market_question"]), None)

                    if open_pos is not None:
                        logger.debug("Trade #%d matched in trader wallet: price=%.4f", trade["id"], open_pos.get("current_price", 0))
                        # Polymarket API gibt immer den YES-Token-Preis zurueck.
                        # Fuer NO-Trades muss der Preis invertiert werden (1 - yes_price = no_price).
                        raw_price = open_pos["current_price"]
                        current_price = (1.0 - raw_price) if trade["side"] == "NO" else raw_price
                        is_resolved = open_pos.get("redeemable", False)

                        # Update missing metadata
                        if not trade["end_date"] and open_pos.get("end_date"):
                            db.update_copy_trade_end_date(trade["id"], open_pos["end_date"])
                        if not trade["outcome_label"] and open_pos.get("outcome_label"):
                            db.update_copy_trade_outcome_label(trade["id"], open_pos["outcome_label"])
                        if not trade_cid and open_pos.get("condition_id"):
                            db.update_copy_trade_condition_id(trade["id"], open_pos["condition_id"])

                        # Close if market resolved
                        if is_resolved:
                            # redeemable=True = Markt resolved.
                            resolve_price = trade["current_price"] if trade["current_price"] else current_price
                            if resolve_price >= 0.50:
                                close_price = 1.0
                            else:
                                close_price = 0.0
                            pnl, shares = _calc_pnl(trade, close_price)
                            if not db.close_copy_trade(trade["id"], pnl):
                                continue  # already closed by another path
                            # Store usdc_received for resolved trades (no exit fee on redemption)
                            _resolve_received = round(shares * close_price, 4)
                            _resolve_cost = _get_size(trade)
                            _resolve_real_pnl = round(_resolve_received - _resolve_cost, 2)
                            db.update_closed_trade_pnl(trade["id"], _resolve_real_pnl, _resolve_received)
                            status = "[+]" if pnl > 0 else "[-]"
                            logger.info("%s Copy trade #%d CLOSED (resolved @ %.0fc): P&L=$%.2f | %s (%s)",
                                       status, trade["id"], close_price * 100, pnl,
                                       trade["market_question"][:40], trade["side"])
                            db.log_activity("resolved", "WIN" if pnl > 0 else "LOSS",
                                            "Position %s" % ("won" if pnl > 0 else "lost"),
                                            "#%d %s — P&L $%+.2f" % (trade["id"], trade["market_question"][:40], pnl), pnl)
                            continue

                        # Still open → update price (WebSocket first, then Gamma REST)
                        event_slug = trade["event_slug"] or trade["market_slug"] or ""
                        live_price = _fetch_live_price(event_slug, trade["market_question"], trade["side"], trade_cid)
                        best_price = live_price if live_price is not None else current_price
                        # Positions-API-Preis als Untergrenze: WS kann leicht abweichen (Rounding)
                        effective_price = max(best_price, current_price) if (best_price and current_price) else (best_price or current_price)
                        # Always update price (even 0c) so dashboard shows current state
                        if effective_price is None:
                            effective_price = current_price or 0
                        if effective_price is not None:
                            pnl, shares = _calc_pnl(trade, effective_price)

                            db.update_copy_trade_price(trade["id"], effective_price, pnl)
                            logger.debug("Trade #%d: %.0f%c | P&L=$%.2f", trade["id"], effective_price * 100, 0xa2, pnl)

                            # Stop-Loss: auto-sell if loss exceeds threshold (per-trader override)
                            _ep = _get_entry_price(trade)
                            _sl_trader = (trade.get("wallet_username") or "").lower()
                            _sl_pct = _STOP_LOSS_MAP.get(_sl_trader, config.STOP_LOSS_PCT)
                            if _sl_pct > 0 and _ep > 0:
                                loss_pct = (_ep - effective_price) / _ep
                                if loss_pct >= _sl_pct:
                                    # Sell FIRST, then close DB (prevents orphaned positions)
                                    _sl_resp = None
                                    if LIVE_MODE and trade_cid:
                                        _sl_resp = sell_shares(trade_cid, trade["side"], effective_price)
                                        if not _sl_resp:
                                            logger.warning("[STOP-LOSS] Sell failed, keeping position open: %s", trade["market_question"][:40])
                                            continue
                                    if not db.close_copy_trade(trade["id"], pnl):
                                        continue  # already closed by another path
                                    if _sl_resp:
                                        _correct_sell_pnl(trade, _sl_resp, trade["id"])
                                    logger.info("[STOP-LOSS] #%d closed at %.0f%% loss: $%.2f | %s",
                                                trade["id"], loss_pct * 100, pnl, trade["market_question"][:40])
                                    db.log_activity("sell", "LOSS", "Stop-loss triggered",
                                                    "#%d %s — P&L $%+.2f" % (trade["id"], trade["market_question"][:35], pnl), round(pnl, 2))
                                    continue

                            # Trailing Stop: once position was 20%+ up, trail sell point below peak
                            # Sell point = peak - MARGIN (follows peak upward, never downward)
                            # Only activates after position was genuinely 20%+ above entry
                            if config.TRAILING_STOP_ENABLED and _ep > 0:
                                _peak = trade.get("peak_price") if trade.get("peak_price") not in (None, 0) else effective_price
                                _peak_gain = (_peak - _ep) / _ep
                                _sell_at = _peak - config.TRAILING_STOP_MARGIN
                                # Ensure sell point is at least at entry (never sell at a loss via trailing)
                                _sell_at = max(_sell_at, _ep)
                                if _peak_gain >= config.TRAILING_STOP_ACTIVATE and effective_price <= _sell_at:
                                    _ts_resp = None
                                    if LIVE_MODE and trade_cid:
                                        _ts_resp = sell_shares(trade_cid, trade["side"], effective_price)
                                        if not _ts_resp:
                                            logger.warning("[TRAILING-STOP] Sell failed, keeping position open: %s", trade["market_question"][:40])
                                            continue
                                    if not db.close_copy_trade(trade["id"], pnl):
                                        continue
                                    if _ts_resp:
                                        _correct_sell_pnl(trade, _ts_resp, trade["id"])
                                    logger.info("[TRAILING-STOP] #%d closed — peak was %.0fc (+%.0f%%), now %.0fc: P&L=$%.2f | %s",
                                                trade["id"], _peak * 100, _peak_gain * 100, effective_price * 100, pnl, trade["market_question"][:40])
                                    db.log_activity("sell", "WIN" if pnl >= 0 else "LOSS", "Trailing stop triggered",
                                                    "#%d %s — peak %.0fc, sold %.0fc, P&L $%+.2f" % (trade["id"], trade["market_question"][:35], _peak * 100, effective_price * 100, pnl), round(pnl, 2))
                                    continue

                            # Take-Profit: per-trader override via TAKE_PROFIT_MAP
                            # Custom value fully replaces global (0=disabled for that trader)
                            # AUTO_SELL_PRICE (96c) catches everything regardless of TP
                            _tp_trader = (trade.get("wallet_username") or "").lower()
                            _tp_pct = _TAKE_PROFIT_MAP.get(_tp_trader, config.TAKE_PROFIT_PCT)
                            if _tp_pct > 0 and _ep > 0:
                                gain_pct = (effective_price - _ep) / _ep
                                if gain_pct >= _tp_pct:
                                    # Sell FIRST, then close DB (prevents orphaned positions)
                                    _tp_resp = None
                                    if LIVE_MODE and trade_cid:
                                        _tp_resp = sell_shares(trade_cid, trade["side"], effective_price)
                                        if not _tp_resp:
                                            logger.warning("[TAKE-PROFIT] Sell failed, keeping position open: %s", trade["market_question"][:40])
                                            continue
                                    if not db.close_copy_trade(trade["id"], pnl):
                                        continue  # already closed by another path
                                    if _tp_resp:
                                        _correct_sell_pnl(trade, _tp_resp, trade["id"])
                                    logger.info("[TAKE-PROFIT] #%d closed at %.0f%% gain: $%.2f | %s",
                                                trade["id"], gain_pct * 100, pnl, trade["market_question"][:40])
                                    db.log_activity("sell", "WIN", "Take-profit triggered",
                                                    "#%d %s — P&L $%+.2f" % (trade["id"], trade["market_question"][:35], pnl), round(pnl, 2))
                                    continue



                    else:
                        # --- NOT IN OPEN: Check if trader closed it ---
                        if trade_cid and trade_cid in closed_cids:
                            closed_pos = next((p for p in closed_positions if p.get("condition_id") == trade_cid), None)
                            trader_closed_at = (closed_pos or {}).get("closed_at", "") if closed_pos else ""
                            trade_created_at = trade["created_at"] or ""

                            if not trader_closed_at or not trade_created_at:
                                pass  # Kann nicht verifizieren — offen lassen
                            elif trader_closed_at < trade_created_at:
                                pass  # Alte historische Close — ignorieren
                            else:
                                # Bester Close-Preis: closed_pos price, dann aktueller Preis, dann entry
                                raw_close = None
                                if closed_pos and closed_pos.get("closed_price"):
                                    raw_close = closed_pos["closed_price"]
                                if raw_close is None or raw_close <= 0:
                                    raw_close = trade["current_price"] or _get_entry_price(trade)
                                # Resolved-Logik: >= 0.50 = gewonnen ($1), < 0.50 = verloren ($0)
                                if raw_close >= 0.95:
                                    close_price = 1.0
                                elif raw_close < 0.05:
                                    close_price = 0.0
                                else:
                                    close_price = raw_close
                                pnl, shares = _calc_pnl(trade, close_price)
                                # Sell FIRST, then close DB (prevents orphaned positions)
                                sell_resp = None
                                if LIVE_MODE and trade_cid:
                                    sell_resp = sell_shares(trade_cid, trade["side"], close_price)
                                    if not sell_resp:
                                        logger.warning("[LIVE] SELL failed, keeping open: %s", trade["market_question"][:40])
                                        continue
                                if not db.close_copy_trade(trade["id"], pnl):
                                    logger.info("[SKIP] Trade #%d already closed by another path", trade["id"])
                                    continue
                                if sell_resp:
                                    _correct_sell_pnl(trade, sell_resp, trade["id"])
                                status = "[+]" if pnl > 0 else "[-]"
                                logger.info("%s Copy trade #%d CLOSED (trader closed): P&L=$%.2f @ %.0fc (%s) | %s",
                                           status, trade["id"], pnl, close_price * 100, trade["side"],
                                           trade["market_question"][:40])
                                db.log_activity("sell", "WIN" if pnl > 0 else "LOSS",
                                                "Trader closed position — sold",
                                                "#%d %s — P&L $%+.2f" % (trade["id"], trade["market_question"][:40], pnl), pnl)
                                continue

                        # --- FALLBACK: Check Gamma API if market is resolved ---
                        if trade_cid:
                            try:
                                import json as _json
                                gr = _api_get("https://gamma-api.polymarket.com/markets",
                                              params={"conditionId": trade_cid})
                                if gr and gr.json():
                                    gm = gr.json()[0]
                                    if gm.get("closed") or gm.get("resolved"):
                                        outcomes = gm.get("outcomes", "[]")
                                        prices = gm.get("outcomePrices", "[]")
                                        if isinstance(outcomes, str): outcomes = _json.loads(outcomes)
                                        if isinstance(prices, str): prices = _json.loads(prices)
                                        resolve_p = None
                                        s_upper = trade["side"].upper()
                                        s_lower = trade["side"].lower()
                                        for i, o in enumerate(outcomes):
                                            if o.lower() == s_lower and i < len(prices):
                                                resolve_p = float(prices[i])
                                                break
                                        if resolve_p is None and s_upper == "YES" and prices:
                                            resolve_p = float(prices[0])
                                        elif resolve_p is None and s_upper == "NO" and len(prices) > 1:
                                            resolve_p = float(prices[1])
                                        if resolve_p is not None:
                                            final = 1.0 if resolve_p >= 0.50 else 0.0
                                            pnl, shares = _calc_pnl(trade, final)
                                            if not db.close_copy_trade(trade["id"], pnl):
                                                continue  # already closed
                                            # Store usdc_received for resolved trades
                                            _gamma_received = round(shares * final, 4)
                                            _gamma_cost = _get_size(trade)
                                            db.update_closed_trade_pnl(trade["id"], round(_gamma_received - _gamma_cost, 2), _gamma_received)
                                            st = "[+]" if pnl > 0 else "[-]"
                                            logger.info("%s Trade #%d AUTO-CLOSED (Gamma resolved): PnL=$%.2f | %s",
                                                        st, trade["id"], pnl, trade["market_question"][:40])
                                            db.log_activity("resolved", "WIN" if pnl > 0 else "LOSS",
                                                            "Position %s" % ("won" if pnl > 0 else "lost"),
                                                            "#%d %s — P&L $%+.2f" % (trade["id"], trade["market_question"][:40], pnl), pnl)
                                            try:
                                                from dashboard.app import broadcast_event
                                                broadcast_event("trade_closed", {
                                                    "id": trade["id"], "trader": trade["wallet_username"],
                                                    "market": trade["market_question"][:60],
                                                    "pnl": round(pnl, 2), "price": round(final * 100),
                                                    "size": trade.get("size", 0),
                                                })
                                            except Exception:
                                                pass
                                            continue
                            except Exception:
                                pass

                        # Trade not in open or closed positions — update price + increment miss counter
                        event_slug = trade["event_slug"] or trade["market_slug"] or ""
                        live_price = price_tracker.get_price(trade_cid, trade["side"]) if (trade_cid and price_tracker.is_connected) else None
                        if live_price is None:
                            live_price = _fetch_live_price(event_slug, trade["market_question"], trade["side"], trade_cid)
                        if live_price is not None:
                            _miss_pnl, _ = _calc_pnl(trade, live_price)
                            db.update_copy_trade_price(trade["id"], live_price, _miss_pnl)

                        # Miss count: position vanished from trader's wallet — after N misses, auto-close
                        miss = db.increment_miss_count(trade["id"])
                        if MISS_COUNT_TO_CLOSE > 0 and miss >= MISS_COUNT_TO_CLOSE:
                            _close_price = live_price if (live_price is not None and live_price > 0) else (trade.get("current_price") or 0)
                            if _close_price <= 0 or _close_price >= 1:
                                logger.debug("Trade #%d: invalid close price %.4f, skipping miss-close", trade["id"], _close_price)
                                continue
                            _pnl, _ = _calc_pnl(trade, _close_price)
                            # Sell first, then close DB
                            _miss_resp = None
                            if LIVE_MODE and trade_cid:
                                _miss_resp = sell_shares(trade_cid, trade["side"], _close_price)
                            if db.close_copy_trade(trade["id"], _pnl, close_price=_close_price):
                                if _miss_resp:
                                    _correct_sell_pnl(trade, _miss_resp, trade["id"])
                                logger.info("[MISS-CLOSE] #%d closed after %d misses: PnL=$%.2f @ %.0fc | %s",
                                            trade["id"], miss, _pnl, _close_price * 100, trade["market_question"][:40])
                                db.log_activity("sell", "WIN" if _pnl > 0 else "LOSS",
                                                "Position closed (stale)",
                                                "#%d %s — P&L $%+.2f" % (trade["id"], trade["market_question"][:35], _pnl), _pnl)
                        else:
                            logger.debug("Trade #%d: not in positions, miss %d/%d", trade["id"], miss, MISS_COUNT_TO_CLOSE)

                except Exception as e:
                    logger.warning("Error updating trade #%d: %s", trade["id"], e)

        except Exception as e:
            logger.warning("Error processing wallet %s: %s", wallet_address[:10], e)


def get_copy_portfolio_summary():
    """Get portfolio summary for copy trading."""
    stats = db.get_copy_trade_stats()
    open_trades = db.get_open_copy_trades()

    total_invested = sum(t["size"] for t in open_trades)
    total_unrealized = sum(t["pnl_unrealized"] or 0 for t in open_trades)

    # Calculate potential profit if all open trades win
    max_profit = 0
    for t in open_trades:
        entry = t["entry_price"]
        if entry > 0 and entry < 1:
            max_profit += t["size"] * (1.0 - entry) / entry

    # Use real wallet balance in LIVE mode
    if LIVE_MODE:
        try:
            from bot.order_executor import get_wallet_balance as _gwb
            cash = _gwb()
        except Exception:
            cash = STARTING_BALANCE + stats["total_pnl"] - total_invested
    else:
        cash = STARTING_BALANCE + stats["total_pnl"] - total_invested
    daily_realized = db.get_daily_copy_pnl()

    return {
        "starting_balance": STARTING_BALANCE,
        "cash_balance": round(cash, 2),
        "total_invested": round(total_invested, 2),
        "total_value": round(cash + total_invested + total_unrealized, 2),
        "total_pnl": round(cash + total_invested + total_unrealized - STARTING_BALANCE, 2),
        "realized_pnl": round(stats["total_pnl"], 2),
        "unrealized_pnl": round(total_unrealized, 2),
        "daily_pnl": round(daily_realized, 2),
        "open_trades": stats["open_trades"],
        "closed_trades": stats["closed_trades"],
        "total_trades": stats["total_trades"],
        "wins": stats["wins"],
        "win_rate": stats["win_rate"],
        "max_profit_if_win": round(max_profit, 2),
        "max_total_if_win": round(cash + total_invested + max_profit, 2),
    }
