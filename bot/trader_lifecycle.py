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

SETTINGS_PATH = '/root/polymarket-copy-bot/settings.env'

PAPER_MIN_TRADES = 25
PAPER_MIN_WR = 58.0
PAPER_REHAB_MIN_TRADES = 25
PAPER_REHAB_MIN_WR = 58.0
MAX_PAUSE_COUNT = 2
KICK_30D_PNL = -30.0
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
    if pnl_7d < -20:
        hours = PAUSE_DURATIONS["pnl_20"]
    elif pnl_7d < -10:
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
    _write_settings(content)
    logger.info("[LIFECYCLE] Added %s to FOLLOWED_TRADERS", username or address[:12])


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
