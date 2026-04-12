"""
Trader Lifecycle Manager — automatisiert den kompletten Trader-Lebenszyklus.
DISCOVERED -> OBSERVING (48h) -> PAPER_FOLLOW (7-14d) -> LIVE_FOLLOW -> PAUSED -> KICKED
"""
import logging
import json
import os
import re
from datetime import datetime, timedelta

from database import db
import config

logger = logging.getLogger(__name__)

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'settings.env')

PAPER_MIN_TRADES = 25
PAPER_MIN_WR = 58.0
PAPER_REHAB_MIN_TRADES = 25
PAPER_REHAB_MIN_WR = 58.0
MAX_PAUSE_COUNT = 2
KICK_30D_PNL = -50.0
OBSERVE_HOURS = 24
PAUSE_DURATIONS = {"streak": 24, "pnl_10": 48, "pnl_20": 72}
REHAB_DAYS = 3
PAPER_MAX_TRADES = 500  # Nach 500 Paper-Trades ohne Erfolg -> KICK


def check_transitions():
    _check_observing_to_paper()
    _check_paper_to_live()
    _check_paused_to_rehab()
    _check_kick_criteria()
    logger.info("[LIFECYCLE] Transition check complete")


def pause_trader(trader_name: str, reason: str):
    with db.get_connection() as conn:
        wallet = conn.execute(
            "SELECT DISTINCT wallet_address FROM copy_trades "
            "WHERE wallet_username = ? AND wallet_address != '' LIMIT 1",
            (trader_name,)
        ).fetchone()
    if not wallet:
        logger.warning("[LIFECYCLE] Cannot find address for trader %s", trader_name)
        return
    address = wallet["wallet_address"]
    stats = db.get_trader_rolling_pnl(trader_name, 7)
    pnl_7d = stats.get("total_pnl", 0) or 0
    if pnl_7d < -30:
        hours = PAUSE_DURATIONS["pnl_20"]
    elif pnl_7d < -20:
        hours = PAUSE_DURATIONS["pnl_10"]
    else:
        hours = PAUSE_DURATIONS["streak"]
    pause_until = (datetime.now() + timedelta(hours=hours)).isoformat()
    existing = db.get_lifecycle_trader(address)
    if not existing:
        db.upsert_lifecycle_trader(address, trader_name, "LIVE_FOLLOW", "manual")
    db.update_lifecycle_status(address, "PAUSED", reason)
    db.set_lifecycle_pause_until(address, pause_until)
    _remove_followed_trader(address, trader_name)
    logger.info("[LIFECYCLE] PAUSED %s for %dh: %s", trader_name, hours, reason)
    try:
        from dashboard.app import broadcast_event
        broadcast_event("brain_decision", {
            "action": "PAUSE_TRADER", "target": trader_name,
            "reason": reason, "duration": "%dh" % hours,
        })
    except Exception:
        pass


def _check_observing_to_paper():
    traders = db.get_lifecycle_traders_by_status("OBSERVING")
    now = datetime.now()
    for t in traders:
        changed = datetime.fromisoformat(t["status_changed_at"]) if t["status_changed_at"] else now
        age_hours = (now - changed).total_seconds() / 3600
        if age_hours >= OBSERVE_HOURS:
            db.update_lifecycle_status(t["address"], "PAPER_FOLLOW",
                                      "Auto-promoted after %.0fh observation" % age_hours)
            logger.info("[LIFECYCLE] %s: OBSERVING -> PAPER_FOLLOW",
                        t.get("username", t["address"][:12]))


def _check_paper_to_live():
    traders = db.get_lifecycle_traders_by_status("PAPER_FOLLOW")
    for t in traders:
        paper_trades = t.get("paper_trades", 0) or 0
        paper_wr = t.get("paper_wr", 0) or 0
        paper_pnl = t.get("paper_pnl", 0) or 0
        pause_count = t.get("pause_count", 0) or 0
        if pause_count > 0:
            min_trades = PAPER_REHAB_MIN_TRADES
            min_wr = PAPER_REHAB_MIN_WR
        else:
            min_trades = PAPER_MIN_TRADES
            min_wr = PAPER_MIN_WR
        if paper_trades >= min_trades and paper_wr >= min_wr and paper_pnl > 0:
            db.update_lifecycle_status(t["address"], "LIVE_FOLLOW",
                                      "Paper criteria met: %d trades, %.1f%% WR, $%.2f PnL" % (
                                          paper_trades, paper_wr, paper_pnl))
            _add_followed_trader(t["address"], t.get("username", ""))
            logger.info("[LIFECYCLE] %s: PAPER -> LIVE", t.get("username", t["address"][:12]))
            db.log_brain_decision("PROMOTE_TRADER", t.get("username", t["address"][:12]),
                                  "Paper criteria met",
                                  json.dumps({"paper_trades": paper_trades, "paper_wr": paper_wr,
                                              "paper_pnl": paper_pnl}),
                                  "New live trader added")
        elif paper_trades >= PAPER_MAX_TRADES:
            db.update_lifecycle_status(t["address"], "KICKED",
                                      "Paper failed after %d trades: %.1f%% WR, $%.2f PnL" % (
                                          paper_trades, paper_wr, paper_pnl))
            logger.info("[LIFECYCLE] %s: KICKED (paper failed after %d trades, %.1f%% WR)",
                        t.get("username", t["address"][:12]), paper_trades, paper_wr)
            db.log_brain_decision("KICK_TRADER", t.get("username", t["address"][:12]),
                                  "Paper failed after %d trades" % paper_trades,
                                  json.dumps({"paper_trades": paper_trades, "paper_wr": paper_wr,
                                              "paper_pnl": paper_pnl}),
                                  "Removed: could not meet criteria in 500 trades")


def _check_paused_to_rehab():
    traders = db.get_lifecycle_traders_by_status("PAUSED")
    now = datetime.now()
    for t in traders:
        pause_until = t.get("pause_until", "")
        if not pause_until:
            changed = datetime.fromisoformat(t["status_changed_at"]) if t["status_changed_at"] else now
            if (now - changed).days >= REHAB_DAYS:
                _start_rehab(t)
        else:
            try:
                until = datetime.fromisoformat(pause_until)
                if now >= until:
                    _start_rehab(t)
            except ValueError:
                pass


def _start_rehab(trader: dict):
    pause_count = trader.get("pause_count", 0) or 0
    if pause_count >= MAX_PAUSE_COUNT:
        db.update_lifecycle_status(trader["address"], "KICKED",
                                   "Kicked: paused %d times" % pause_count)
        logger.info("[LIFECYCLE] %s: KICKED (paused %dx)",
                    trader.get("username", trader["address"][:12]), pause_count)
        db.log_brain_decision("KICK_TRADER", trader.get("username", ""),
                              "Paused %d times" % pause_count, "", "Permanently removed")
        return
    db.update_lifecycle_paper_stats(trader["address"], 0, 0, 0)
    db.update_lifecycle_status(trader["address"], "PAPER_FOLLOW",
                               "Rehabilitation started after pause")
    logger.info("[LIFECYCLE] %s: PAUSED -> PAPER_FOLLOW (rehab)",
                trader.get("username", trader["address"][:12]))


def _check_kick_criteria():
    for status in ["LIVE_FOLLOW", "PAPER_FOLLOW"]:
        traders = db.get_lifecycle_traders_by_status(status)
        for t in traders:
            username = t.get("username", "")
            if not username:
                continue
            stats_30d = db.get_trader_rolling_pnl(username, 30)
            pnl_30d = stats_30d.get("total_pnl", 0) or 0
            if pnl_30d < KICK_30D_PNL:
                if status == "LIVE_FOLLOW":
                    _remove_followed_trader(t["address"], username)
                db.update_lifecycle_status(t["address"], "KICKED",
                                           "30d PnL $%.2f" % pnl_30d)
                logger.info("[LIFECYCLE] %s: KICKED (30d PnL=$%.2f)", username, pnl_30d)
                db.log_brain_decision("KICK_TRADER", username,
                                      "30d PnL $%.2f" % pnl_30d, "", "Permanently removed")


# NEUTRAL tier defaults — used to seed all per-trader maps when a new
# trader gets promoted PAPER->LIVE. Without these, the new trader would
# fall back to global defaults until auto_tuner first sees a trade from
# them (~2h delay + restart). With these, they start at NEUTRAL tier
# and the auto_tuner can adjust upward/downward as data accumulates.
_NEUTRAL_DEFAULTS = {
    "BET_SIZE_MAP": "0.03",
    "TRADER_EXPOSURE_MAP": "0.10",
    "MIN_ENTRY_PRICE_MAP": "0.38",
    "MAX_ENTRY_PRICE_MAP": "0.75",
    "MIN_TRADER_USD_MAP": "5",
    "TAKE_PROFIT_MAP": "2.0",
    "MAX_COPIES_PER_MARKET_MAP": "1",
    "HEDGE_WAIT_TRADERS": "60",
}


def _seed_tier_defaults(content: str, name: str) -> str:
    """For each per-trader map, add 'name:value' if name not already present.
    Returns updated content. Does not write to disk.
    """
    for map_key, default_val in _NEUTRAL_DEFAULTS.items():
        m = re.search(r'^(' + re.escape(map_key) + r'=)(.*)$', content, re.MULTILINE)
        if not m:
            continue
        current = m.group(2).strip()
        # Already in map? skip
        if re.search(r'(^|,)\s*' + re.escape(name) + r'\s*:', current):
            continue
        new_entry = "%s:%s" % (name, default_val)
        if current:
            new_val = current + "," + new_entry
        else:
            new_val = new_entry
        content = re.sub(r'^(' + re.escape(map_key) + r'=).*$',
                         r'\g<1>' + new_val, content, flags=re.MULTILINE)
    return content


def _add_followed_trader(address: str, username: str):
    content = _read_settings()
    match = re.search(r'^FOLLOWED_TRADERS=(.*)$', content, re.MULTILINE)
    current = match.group(1).strip() if match else ""
    entry = "%s:%s" % (username, address) if username else address
    if address in current:
        return
    new_val = ("%s,%s" % (current, entry)).strip(",")
    pattern = r'^(FOLLOWED_TRADERS=).*$'
    content = re.sub(pattern, r'\g<1>' + new_val, content, flags=re.MULTILINE)

    # Auto-add starter settings for new trader
    name = username or address[:12]
    _maps = {
        "BET_SIZE_MAP": (name, "0.02"),
        "TRADER_EXPOSURE_MAP": (name, "0.05"),
        "AVG_TRADER_SIZE_MAP": (name, "20"),
    }
    for map_key, (trader_id, default_val) in _maps.items():
        m = re.search(r'^(%s=)(.*)$' % map_key, content, re.MULTILINE)
        if m:
            current_map = m.group(2).strip()
            if trader_id.lower() not in current_map.lower():
                new_map = ("%s,%s:%s" % (current_map, trader_id, default_val)).strip(",")
                content = re.sub(r'^(%s=).*$' % map_key, r'\g<1>' + new_map, content, flags=re.MULTILINE)
                logger.info("[LIFECYCLE] Auto-settings: %s +%s:%s", map_key, trader_id, default_val)

    _write_settings(content)
    logger.info("[LIFECYCLE] Added %s to FOLLOWED_TRADERS + seeded NEUTRAL tier defaults",
                username or address[:12])


def _remove_followed_trader(address: str, username: str):
    content = _read_settings()
    match = re.search(r'^FOLLOWED_TRADERS=(.*)$', content, re.MULTILINE)
    if not match:
        return
    current = match.group(1).strip()
    entries = [e.strip() for e in current.split(",") if e.strip()]
    entries = [e for e in entries if address not in e]
    new_val = ",".join(entries)
    pattern = r'^(FOLLOWED_TRADERS=).*$'
    content = re.sub(pattern, r'\g<1>' + new_val, content, flags=re.MULTILINE)
    _write_settings(content)
    logger.info("[LIFECYCLE] Removed %s from FOLLOWED_TRADERS", username or address[:12])


def _read_settings() -> str:
    from bot.settings_lock import read_settings
    return read_settings()


def _write_settings(content: str):
    from bot.settings_lock import write_settings
    write_settings(content)
