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

# Feste Werte (selten geändert)
CASH_RESERVE = 0
ENTRY_SLIPPAGE = 0.0
TRADE_SEC_FROM_RESOLVE = 120
IDLE_TRIGGER_SECS = 20 * 60
MAX_CATEGORY_PCT = 0.30
BUY_THRESHOLD = 0.0
PENDING_BUY_MIN_SECS = 210
PENDING_BUY_MAX_SECS = 900
MAX_TRADES_PER_SCAN = 3

# Per-trader exposure map (parsed once at module load)
_EXPOSURE_MAP: dict[str, float] = {}
for _entry in config.TRADER_EXPOSURE_MAP.split(","):
    _entry = _entry.strip()
    if ":" in _entry:
        _parts = _entry.split(":", 1)
        _EXPOSURE_MAP[_parts[0].strip().lower()] = float(_parts[1].strip())

# Pending Buy Queue (in-memory: condition_id → {trade_data, queued_at})
_pending_buys: dict = {}

# Idle-Replace Cooldown: verhindert Loop (address → letzter Replace-Zeitpunkt)
_idle_replaced_at: dict = {}

# Hedge-Detection Queue: holds trades for 120s to check if trader buys opposite side
# Key: event_slug or market group → {sides: {side: trade_data}, queued_at: timestamp}
_hedge_queue: dict = {}  # event_slug → {sides: {side: trade_data}, queued_at: ts, address: addr}

# Circuit Breaker: nach N aufeinanderfolgenden API-Fehlern → X Sekunden Pause
_CB_THRESHOLD = 8
_CB_PAUSE_SECS = 60
_cb_failures = 0
_cb_open_until = 0.0
_cb_lock = __import__("threading").Lock()


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


def _api_get(url, params=None, timeout=10, max_retries=3):
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


def _get_current_balance() -> float:
    """Aktueller Kontostand (Startkapital + realisierte Gewinne)."""
    stats = db.get_copy_trade_stats()
    return STARTING_BALANCE + stats["total_pnl"]


def _calculate_position_size(entry_price: float, balance: float, trader_ratio: float = 1.0) -> float:
    """Bet-Sizing: 2% vom Portfolio × Preis-Signal × proportionaler Trader-Multiplikator.

    trader_ratio = trader_trade_size / trader_median_trade_size
        → wenn Trader 2x seinen Durchschnitt setzt, setzen wir auch 2x

    Preis-Multiplikator:
        0-20¢ / 80-100¢  → ×1.5  (sehr starkes Signal)
        20-35¢ / 65-80¢  → ×1.0  (normales Signal)
        35-50¢           → ×0.60 (schwaches Signal)

    trader_ratio wird auf [0.5, 3.0] begrenzt um Ausreisser abzufangen.
    """
    available = balance - CASH_RESERVE
    if available <= 0:
        return 0

    # Basis: BET_SIZE_PCT vom Portfolio (default 2%)
    base = balance * BET_SIZE_PCT

    # Preis-Signal Multiplikator
    edge = abs(entry_price - 0.50)
    if edge >= 0.30:
        price_mult = 1.50
    elif edge >= 0.15:
        price_mult = 1.00
    else:
        price_mult = 0.60

    # Proportionaler Trader-Multiplikator
    clamped_ratio = max(config.RATIO_MIN, min(config.RATIO_MAX, trader_ratio))

    size = base * price_mult * clamped_ratio
    size = min(size, MAX_POSITION_SIZE, available)
    return round(max(MIN_TRADE_SIZE, size), 2)


CASH_FLOOR = config.CASH_FLOOR
CASH_RECOVERY = config.CASH_RECOVERY
SAVE_POINT_STEP = 1.0 # Floor steigt pro Recovery-Zyklus um $1

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

        if elapsed < PENDING_BUY_MIN_SECS:
            continue  # Noch nicht reif

        # Aktuellen Preis prüfen
        trade_data = entry["trade_data"]
        live = price_tracker.get_price(cid, trade_data["side"]) if price_tracker.is_connected else None
        current = live if live is not None else trade_data["entry_price"]

        if current < BUY_THRESHOLD:
            continue  # Preis noch unter Threshold

        # Prüfe ob noch Kapital vorhanden
        size = _calculate_position_size(current, balance,
                                        trader_ratio=entry.get("trader_ratio", 1.0))
        # Cash-Floor Check: genug Cash uebrig?
        cash_left = balance - total_invested - size
        if cash_left < _load_dynamic_floor():
            expired_keys.append(cid)
            logger.info("[PENDING] Kein Cash mehr: %s", trade_data["market_question"][:40])
            continue

        trade_data["entry_price"] = round(min(current + ENTRY_SLIPPAGE, 0.97), 4)
        trade_data["size"] = size
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
                        total_invested: float) -> int:
    """Position-Diff: findet neue Positionen die der Activity-Feed verpasst hat.

    Holt aktuelle Positionen des Traders und vergleicht mit unseren copy_trades.
    Jede Condition-ID die weder als 'open' noch als 'baseline' in unserer DB ist
    → neuer Trade der kopiert werden soll.
    """
    try:
        positions = fetch_wallet_positions(address)
        if not positions:
            return 0

        # Alle bekannten condition_ids für diese Wallet (open + baseline)
        known = {t["condition_id"] for t in db.get_all_copy_trades_for_wallet(address) if t["condition_id"]}

        new_trades = 0
        for pos in positions:
            cid = pos.get("condition_id", "")
            if not cid or cid in known:
                continue
            if pos.get("redeemable", False) or pos.get("size", 0) < 0.5:
                continue

            entry_price_raw = pos.get("current_price", 0)
            if entry_price_raw <= 0 or entry_price_raw >= 1:
                continue

            # Max exposure per trader (per-trader or default)
            _max_exp = (balance + sum(t["size"] for t in db.get_open_copy_trades())) * _EXPOSURE_MAP.get(username.lower(), config.MAX_EXPOSURE_PER_TRADER)
            _t_exp = sum(t["size"] for t in db.get_open_copy_trades() if t["wallet_address"] == address)
            if _t_exp >= _max_exp:
                logger.info("[DIFF] Trader exposure $%.0f >= max $%.0f, skipping: %s",
                            _t_exp, _max_exp, pos["market_question"][:40])
                continue

            # Market-close guard
            end_ts = _parse_end_ts(pos.get("end_date", ""))
            if end_ts and (_time.time() - end_ts) > 0:
                continue  # Markt bereits vorbei

            entry_price = round(min(entry_price_raw + ENTRY_SLIPPAGE, 0.97), 4)
            size = _calculate_position_size(entry_price, balance)
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
            }
            # LIVE MODE: Echte Order platzieren
            if LIVE_MODE and cid:
                order_resp = buy_shares(cid, pos["side"], size, entry_price)
                if not order_resp:
                    logger.warning("[DIFF] Order fehlgeschlagen — ueberspringe: %s", pos["market_question"][:40])
                    continue

            trade_id = db.create_copy_trade(trade)
            if trade_id:
                new_trades += 1
                total_invested += size
                price_tracker.subscribe_condition(cid)
                logger.info("[DIFF] Neuer Trade #%d (via Position-Diff): %s @ %.0fc (%s)",
                            trade_id, pos["market_question"][:40], entry_price * 100, pos["side"])
                db.log_activity("buy", "BUY", "Copied position from %s" % username,
                                "#%d %s @ %dc — $%.2f" % (trade_id, pos["market_question"][:40], entry_price * 100, size))
        return new_trades
    except Exception as e:
        logger.debug("Position-diff error for %s: %s", address[:10], e)
        return 0


def _run_baseline(address: str, username: str):
    """Baseline fuer neu gefolgte Wallet: Snapshot + Timestamp, nichts kopieren."""
    positions = fetch_wallet_positions(address)
    if positions:
        logger.info("[BASELINE] %s — saving %d existing positions (not copying)", username, len(positions))
        for pos in positions:
            if pos["size"] < 0.50 or pos.get("redeemable", False):
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
    REPLACE_COOLDOWN = 30 * 60  # 30 Min Cooldown nach Replace — verhindert Loop
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

    # Idle-Check deaktiviert — nur manuell gefolgte Trader
    # _run_idle_check(followed)

    if not _check_trade_limit():
        return 0

    # Max offene Positionen prüfen
    stats = db.get_copy_trade_stats()
    if stats["open_trades"] >= MAX_OPEN_POSITIONS:
        logger.info("[SKIP] Max offene Positionen erreicht (%d/%d)", stats["open_trades"], MAX_OPEN_POSITIONS)
        return 0

    logger.info("[SCAN] Checking %d wallets for new positions...", len(followed))

    new_trades = 0
    try:
        from bot.order_executor import get_wallet_balance
        cash = get_wallet_balance()
    except Exception:
        cash = 0
    balance = cash
    total_invested = 0
    # Cache open trades for this scan (avoid repeated DB queries in loops)
    _cached_open_trades = list(db.get_open_copy_trades())
    # Portfolio value from Polymarket API (real value, not DB sizes)
    _open_value = 0
    try:
        _pos_r = requests.get("https://data-api.polymarket.com/positions", params={
            "user": config.POLYMARKET_FUNDER, "limit": 500, "sizeThreshold": 0
        }, timeout=10)
        if _pos_r.ok:
            _open_value = sum(float(p.get("currentValue", 0) or 0) for p in _pos_r.json())
    except Exception:
        _open_value = sum(t["size"] for t in _cached_open_trades)  # fallback to DB
    portfolio_value = cash + _open_value
    logger.info("PORTFOLIO: Wallet=$%.2f | Positions=$%.2f | Total=$%.2f", cash, _open_value, portfolio_value)

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
                    # Re-inject into the activity feed by creating the trade directly
                    entry_price = td["entry_price"]
                    # Check trader exposure limit (per-trader or default)
                    _max_t = portfolio_value * _EXPOSURE_MAP.get(td["username"].lower(), config.MAX_EXPOSURE_PER_TRADER)
                    _t_inv = sum(x["size"] for x in _cached_open_trades if x["wallet_address"] == td["address"])
                    if _t_inv >= _max_t:
                        logger.info("[HEDGE-WAIT] Trader exposure $%.0f >= max $%.0f, skipping: %s", _t_inv, _max_t, td["question"][:40])
                        continue
                    # Max per event check
                    if config.MAX_PER_EVENT > 0:
                        _hw_evt = td["trade_data"].get("event_slug", "") or ""
                        if _hw_evt:
                            _hw_evt_inv = sum(x["size"] for x in _cached_open_trades if x.get("event_slug", "") == _hw_evt)
                            if _hw_evt_inv >= config.MAX_PER_EVENT:
                                logger.info("[HEDGE-WAIT] Event exposure $%.0f >= max $%.0f, skipping: %s",
                                            _hw_evt_inv, config.MAX_PER_EVENT, td["question"][:40])
                                continue
                    size = _calculate_position_size(entry_price, cash, 1.0)
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
                    }
                    if LIVE_MODE and td["cid"]:
                        from bot.order_executor import get_wallet_balance as _gwb2
                        real_bal = _gwb2()
                        if real_bal < size:
                            continue
                        order_resp = buy_shares(td["cid"], side, size, entry_price)
                        if not order_resp:
                            continue
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

        recent_trades = fetch_wallet_recent_trades(address, limit=50)
        if not recent_trades:
            logger.info("[SCAN] %s: keine Trades von API", username)
            continue

        # Durchschnittliche Trade-Größe des Traders (für proportionales Sizing)
        buy_sizes = [t.get("usdc_size", 0) for t in recent_trades if t["trade_type"] == "BUY" and t.get("usdc_size", 0) > 0]
        avg_trader_size = (sum(buy_sizes) / len(buy_sizes)) if buy_sizes else 10.0

        # Update timestamp to newest trade seen (for next scan)
        max_ts = max(t["timestamp"] for t in recent_trades)
        logger.debug("[SCAN] %s: last_ts=%d, max_ts=%d, delta=%ds",
                     username, last_ts, max_ts, max_ts - last_ts)
        if max_ts > last_ts:
            db.set_last_trade_timestamp(address, max_ts)

        # Only BUY trades that happened AFTER our last seen timestamp
        new_buy_trades = [
            t for t in recent_trades
            if t["trade_type"] == "BUY" and t["timestamp"] > last_ts
        ]

        all_buys = [t for t in recent_trades if t["trade_type"] == "BUY"]
        logger.info("[SCAN] %s: %d BUYs gesamt, %d neu (nach ts=%d)",
                    username, len(all_buys), len(new_buy_trades), last_ts)

        # === FAST SELL DETECTION: RN1 SELLs sofort erkennen (alle 5s) ===
        new_sells = [t for t in recent_trades if t["trade_type"] == "SELL" and t["timestamp"] > last_ts]
        if new_sells:
            open_by_cid = {t["condition_id"]: t for t in _cached_open_trades if t["condition_id"] and t["wallet_address"] == address}
            for sell in new_sells:
                sell_cid = sell.get("condition_id", "")
                if sell_cid and sell_cid in open_by_cid:
                    our_trade = open_by_cid[sell_cid]
                    sell_price = sell.get("price", 0)
                    if not sell_price:
                        sell_price = our_trade["current_price"] or our_trade["entry_price"]
                    # LIVE: echte Sell Order
                    if LIVE_MODE and sell_cid:
                        sell_resp = sell_shares(sell_cid, our_trade["side"], sell_price)
                        if sell_resp:
                            logger.info("[FAST-SELL] Order OK: %s @ %.0fc", our_trade["market_question"][:40], sell_price * 100)
                        else:
                            logger.warning("[FAST-SELL] Order fehlgeschlagen: %s", our_trade["market_question"][:40])
                    shares = our_trade["size"] / our_trade["entry_price"] if our_trade["entry_price"] > 0 else 0
                    pnl = (sell_price - our_trade["entry_price"]) * shares
                    db.close_copy_trade(our_trade["id"], round(pnl, 2))
                    logger.info("[FAST-SELL] #%d CLOSED (trader sold): PnL=$%.2f @ %.0fc | %s",
                                our_trade["id"], pnl, sell_price * 100, our_trade["market_question"][:40])
                    db.log_activity("sell", "WIN" if pnl > 0 else "LOSS",
                                    "Position closed — sold",
                                    "#%d %s — P&L $%+.2f" % (our_trade["id"], our_trade["market_question"][:40], pnl), pnl)
                    try:
                        from dashboard.app import broadcast_event
                        broadcast_event("trade_closed", {
                            "id": our_trade["id"], "trader": username,
                            "market": our_trade["market_question"][:60],
                            "pnl": round(pnl, 2), "price": round(sell_price * 100),
                        })
                    except Exception:
                        pass

        # Position-Diff: Fallback für Trades die der Activity-Feed verpasst hat
        new_trades += _position_diff_scan(address, username, balance, total_invested)

        for t in new_buy_trades:
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
                            "AND closed_at > datetime('now', '-%d minutes')" % config.NO_REBUY_MINUTES, (cid,)
                        ).fetchone()
                        if _was_closed:
                            logger.info("[SKIP] Recently closed (no-rebuy %dmin): %s",
                                        config.NO_REBUY_MINUTES, question[:40])
                            continue
                except Exception:
                    pass

            # === RN1 SMART-FILTER ===
            # 1) Min Trader USD: Nur echte Conviction-Trades kopieren, Noise ignorieren
            dollar_value = t.get("usdc_size", 0)
            if dollar_value < config.MIN_TRADER_USD:
                logger.info("[FILTER] Size $%.1f < $%.0f: %s",
                            dollar_value, config.MIN_TRADER_USD, question[:40])
                continue

            # 2) Preis-Range-Filter: Trash-Farming (1-3c) und Hedges (95-99c) ausfiltern
            trader_price = t["price"]
            if trader_price < config.MIN_ENTRY_PRICE or trader_price > config.MAX_ENTRY_PRICE:
                logger.info("[FILTER] Preis %.0fc ausserhalb Range (%.0f-%.0fc): %s",
                            trader_price * 100, config.MIN_ENTRY_PRICE * 100,
                            config.MAX_ENTRY_PRICE * 100, question[:40])
                continue

            # 3) Max Kopien pro Markt: nicht X-mal denselben Markt kopieren
            if cid and db.count_copies_for_market(address, cid) >= config.MAX_COPIES_PER_MARKET:
                logger.info("[FILTER] Max copies (%d) for market: %s",
                            config.MAX_COPIES_PER_MARKET, question[:40])
                continue

            # === STANDARD-FILTER ===
            # Duplikat-Markt-Check: nicht denselben Markt von 2 Tradern kopieren
            if cid and db.is_market_already_open(cid, from_wallet=address):
                logger.info("[SKIP] Markt bereits offen (anderer Trader): %s", question[:40])
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
            # Live-Preis nur nehmen wenn: >= 5 Cent UND max 50% Abweichung vom Trader-Preis
            if live_price is not None and live_price >= 0.05 and 0 < live_price < 1:
                diff_pct = abs(live_price - trader_price) / trader_price
                if diff_pct <= 0.50:
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
                    continue


            # Spread-Filter: illiquide Märkte überspringen (WebSocket-Daten, falls verfügbar)
            if cid and price_tracker.is_connected:
                spread = price_tracker.get_spread(cid, t["side"])
                if spread is not None and spread > MAX_SPREAD:
                    logger.info("[SKIP] Spread zu gross (%.1f%% > %.0f%%): %s",
                                spread * 100, MAX_SPREAD * 100, question[:40])
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
                                             params={"slug": _event_slug.split("/")[-1]}, timeout=5)
                        if _ev_r.ok and _ev_r.json():
                            _ev = _ev_r.json()[0] if isinstance(_ev_r.json(), list) else _ev_r.json()
                            _st = _ev.get("startTime", "")
                            if _st:
                                from datetime import datetime as _dt, timezone as _tz
                                _start = _dt.fromisoformat(_st.replace("Z", "+00:00"))
                                _now_utc = _dt.now(_tz.utc)
                                _hours_until = (_start - _now_utc).total_seconds() / 3600
                                if _hours_until > config.MAX_HOURS_BEFORE_EVENT:
                                    logger.info("[SKIP] Event in %.1fh (max %.1fh): %s",
                                                _hours_until, config.MAX_HOURS_BEFORE_EVENT, question[:40])
                                    continue
                    except Exception:
                        pass  # API fail → don't block, just skip check

            # Max $ per event (same game/match) — cap size to remaining budget
            _evt_remaining = None
            if config.MAX_PER_EVENT > 0:
                _evt = t.get("event_slug", "") or ""
                if _evt:
                    _evt_invested = sum(
                        ot["size"] for ot in _cached_open_trades
                        if ot.get("event_slug", "") == _evt
                    )
                    _evt_remaining = config.MAX_PER_EVENT - _evt_invested
                    if _evt_remaining < MIN_TRADE_SIZE:
                        logger.info("[SKIP] Event full $%.0f/$%.0f: %s",
                                    _evt_invested, config.MAX_PER_EVENT, question[:40])
                        continue

            # Apply realistic entry slippage (+1 tick) — simulates execution delay
            entry_price = round(min(entry_price_raw + ENTRY_SLIPPAGE, 0.97), 4)

            # Max exposure per trader (per-trader override or default)
            _trader_pct = _EXPOSURE_MAP.get(username.lower(), config.MAX_EXPOSURE_PER_TRADER)
            max_per_trader = portfolio_value * _trader_pct
            trader_invested = sum(
                ot["size"] for ot in _cached_open_trades
                if ot["wallet_address"] == address
            )
            if trader_invested >= max_per_trader:
                logger.info("[SKIP] Trader exposure $%.0f >= max $%.0f (%.0f%%): %s",
                            trader_invested, max_per_trader, _trader_pct * 100, question[:40])
                continue

            # Proportionaler Trader-Multiplikator: dieser Trade vs. Trader-Durchschnitt
            trader_ratio = (dollar_value / avg_trader_size) if avg_trader_size > 0 else 1.0
            size = _calculate_position_size(entry_price, balance, trader_ratio=trader_ratio)
            # Cap to event remaining budget
            if _evt_remaining is not None and size > _evt_remaining:
                size = round(_evt_remaining, 2)
                logger.info("[SIZE] Capped to event budget: $%.2f (remaining $%.2f of $%.0f) | %s",
                            size, _evt_remaining, config.MAX_PER_EVENT, question[:35])
            else:
                logger.info("[SIZE] %s: trader=$%.0f avg=$%.0f ratio=%.2f → our=$%.2f | %s",
                            username, dollar_value, avg_trader_size, trader_ratio, size, question[:35])

            cash_left = balance - total_invested - size
            if cash_left < _load_dynamic_floor():
                logger.info("[SKIP] Cash-Floor erreicht (Cash $%.2f < Floor) — ueberspringe: %s",
                            cash_left, question[:40])
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
            }

            # Domain-Check: warnen wenn Trade ausserhalb Trader-Spezialisierung
            if trader_domain not in ("General", "Mixed"):
                from bot.wallet_scanner import _detect_domain
                trade_domain = _detect_domain([question])
                if trade_domain not in ("General", "Mixed") and trade_domain != trader_domain:
                    logger.info("[DOMAIN] %s (%s) kopiert %s-Trade: %s",
                                username, trader_domain, trade_domain, question[:40])

            # LIVE MODE: Echte Order auf Polymarket platzieren
            if LIVE_MODE and cid:
                # Echte USDC-Balance prüfen bevor wir ordern
                from bot.order_executor import get_wallet_balance
                real_balance = get_wallet_balance()
                if real_balance < size:
                    logger.warning("[LIVE] Nicht genug USDC ($%.2f < $%.2f): %s",
                                   real_balance, size, question[:40])
                    continue
                # Wallet-Balance VOR Order merken
                bal_before = real_balance
                order_resp = buy_shares(cid, t["side"], size, entry_price)
                if not order_resp:
                    logger.warning("[LIVE] Order fehlgeschlagen — ueberspringe: %s", question[:40])
                    continue
                # Echten Fill-Betrag bestimmen: Differenz der Wallet-Balance
                try:
                    bal_after = get_wallet_balance()
                    real_fill = round(bal_before - bal_after, 2)
                    if real_fill > 0.10:
                        planned_size = size
                        trade["size"] = real_fill
                        size = real_fill
                        logger.info("[LIVE] BUY FILLED: $%.2f echt (geplant $%.2f) @ %.0fc | %s",
                                    real_fill, planned_size, entry_price * 100, question[:40])
                    else:
                        logger.info("[LIVE] BUY Order OK: $%.2f @ %.0fc | %s", size, entry_price * 100, question[:40])
                except Exception:
                    logger.info("[LIVE] BUY Order OK: $%.2f @ %.0fc | %s", size, entry_price * 100, question[:40])

            trade_id = db.create_copy_trade(trade)
            if trade_id:
                new_trades += 1
                total_invested += size
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
                    db.log_activity("buy", "BUY", "Copied position from %s" % username,
                                    "#%d %s @ %dc — $%.2f" % (trade_id, question[:45], round(entry_price*100), size))
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

        if new_trades >= MAX_TRADES_PER_SCAN:
            break  # outer wallet loop break

    logger.info("[DONE] Scan complete. %d new trades copied.", new_trades)
    return new_trades


MISS_COUNT_TO_CLOSE = 180  # Position muss 180x hintereinander fehlen (= 30 Min bei 10s-Intervall)

# Track which positions already got a loss warning (avoid log spam)
_loss_warned: set = set()  # trade_id set


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

    open_trades = db.get_open_copy_trades()
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
                            # DB-Preis (trade["current_price"]) ist zuverlaessiger als API-Preis
                            # weil er bereits korrekt fuer unsere Seite berechnet wurde.
                            resolve_price = trade["current_price"] if trade["current_price"] else current_price
                            if resolve_price >= 0.50:
                                close_price = 1.0
                            else:
                                close_price = 0.0
                            shares = trade["size"] / trade["entry_price"] if trade["entry_price"] > 0 else 0
                            pnl = (close_price - trade["entry_price"]) * shares
                            db.close_copy_trade(trade["id"], round(pnl, 2))
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
                        if effective_price:
                            shares = trade["size"] / trade["entry_price"] if trade["entry_price"] > 0 else 0
                            pnl = (effective_price - trade["entry_price"]) * shares

                            # Kein Stop-Loss, kein Profit-Cap — nur schliessen wenn Markt resolved oder Trader verkauft
                            db.update_copy_trade_price(trade["id"], effective_price, round(pnl, 2))
                            logger.debug("Trade #%d: %.0f%c | P&L=$%.2f", trade["id"], effective_price * 100, 0xa2, pnl)

                            # Log significant unrealized losses (once per position)
                            if pnl <= -3.0 and trade["id"] not in _loss_warned:
                                _loss_warned.add(trade["id"])
                                db.log_activity("warning", "DROP",
                                                "Position dropping — %s" % trade["wallet_username"],
                                                "#%d %s — now %dc (entry %dc), P&L $%.2f" % (
                                                    trade["id"], (trade["market_question"] or "")[:35],
                                                    round(effective_price * 100), round(trade["entry_price"] * 100), round(pnl, 2)),
                                                round(pnl, 2))

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
                                    raw_close = trade["current_price"] or trade["entry_price"]
                                # Resolved-Logik: >= 0.50 = gewonnen ($1), < 0.50 = verloren ($0)
                                if raw_close >= 0.95:
                                    close_price = 1.0
                                elif raw_close < 0.05:
                                    close_price = 0.0
                                else:
                                    close_price = raw_close
                                # LIVE MODE: Sell Order platzieren
                                if LIVE_MODE and trade_cid:
                                    sell_resp = sell_shares(trade_cid, trade["side"], close_price)
                                    if sell_resp:
                                        logger.info("[LIVE] SELL Order OK: %s @ %.0fc",
                                                   trade["market_question"][:40], close_price * 100)
                                    else:
                                        logger.warning("[LIVE] SELL fehlgeschlagen: %s", trade["market_question"][:40])

                                shares = trade["size"] / trade["entry_price"] if trade["entry_price"] > 0 else 0
                                pnl = (close_price - trade["entry_price"]) * shares
                                db.close_copy_trade(trade["id"], round(pnl, 2))
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
                                            shares = trade["size"] / trade["entry_price"] if trade["entry_price"] > 0 else 0
                                            pnl = (final - trade["entry_price"]) * shares
                                            db.close_copy_trade(trade["id"], round(pnl, 2))
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
                                                })
                                            except Exception:
                                                pass
                                            continue
                            except Exception:
                                pass

                        # Nur Preis updaten, NICHT schliessen
                        event_slug = trade["event_slug"] or trade["market_slug"] or ""
                        live_price = price_tracker.get_price(trade_cid, trade["side"]) if (trade_cid and price_tracker.is_connected) else None
                        if live_price is None:
                            live_price = _fetch_live_price(event_slug, trade["market_question"], trade["side"], trade_cid)
                        if live_price is not None:
                            shares = trade["size"] / trade["entry_price"] if trade["entry_price"] > 0 else 0
                            pnl = (live_price - trade["entry_price"]) * shares
                            db.update_copy_trade_price(trade["id"], live_price, round(pnl, 2))
                        logger.debug("Trade #%d: not in positions, keeping open (price update only)", trade["id"])

                except Exception as e:
                    logger.debug("Error updating trade #%d: %s", trade["id"], e)

        except Exception as e:
            logger.debug("Error processing wallet %s: %s", wallet_address[:10], e)


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
