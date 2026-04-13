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
PAUSE_COOLDOWN_HOURS = 48  # After pause expires, cooldown before re-pause allowed
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
    # Guard: skip if trader is already PAUSED (prevents pause_until overwrite loop)
    existing_lc = db.get_lifecycle_trader(address)
    if existing_lc and existing_lc.get("status") == "PAUSED":
        logger.debug("[LIFECYCLE] %s already PAUSED, skipping re-pause", trader_name)
        return
    # Guard: skip if trader was recently unpaused (cooldown prevents ping-pong)
    if existing_lc and existing_lc.get("pause_until"):
        try:
            _last_pause_end = datetime.fromisoformat(existing_lc["pause_until"])
            _hours_since = (datetime.now() - _last_pause_end).total_seconds() / 3600
            if 0 < _hours_since < PAUSE_COOLDOWN_HOURS:
                logger.info("[LIFECYCLE] %s in post-pause cooldown (%.0fh left), skipping",
                            trader_name, PAUSE_COOLDOWN_HOURS - _hours_since)
                return
        except (ValueError, TypeError):
            pass
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
    # _remove_followed_trader(address, trader_name)  # DISABLED: settings managed manually
    logger.info("[LIFECYCLE] PAUSED %s for %dh (DB only, not removed from settings): %s", trader_name, hours, reason)
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
    try:
        _auto_promote = getattr(config, "AUTO_DISCOVERY_AUTO_PROMOTE", False)
    except Exception:
        _auto_promote = False
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
            if not _auto_promote:
                # Mirror the pause_trader policy at line 63-64: settings.env is
                # managed manually. Skip both the status flip and the
                # settings.env mutation. Log a recommendation so the user can
                # review manually (dashboard/logs).
                logger.info(
                    "[LIFECYCLE] %s meets paper criteria (%d trades, %.1f%% WR, $%.2f PnL) "
                    "but AUTO_DISCOVERY_AUTO_PROMOTE=false — review manually",
                    t.get("username", t["address"][:12]),
                    paper_trades, paper_wr, paper_pnl)
                db.log_brain_decision(
                    "PROMOTE_RECOMMENDED", t.get("username", t["address"][:12]),
                    "Paper criteria met but auto-promote disabled",
                    json.dumps({"paper_trades": paper_trades, "paper_wr": paper_wr,
                                "paper_pnl": paper_pnl}),
                    "Manual review required — set AUTO_DISCOVERY_AUTO_PROMOTE=true to enable")
                continue
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
    # Defense-in-depth gate: respect AUTO_DISCOVERY_AUTO_PROMOTE at the
    # function level so ANY call site is covered, not just the ones we
    # remember to gate. Mirrors pause_trader's "settings managed manually"
    # policy (line 63-64) from the opposite direction — brain may not
    # auto-add to FOLLOWED_TRADERS either.
    try:
        if not getattr(config, "AUTO_DISCOVERY_AUTO_PROMOTE", False):
            logger.info(
                "[LIFECYCLE] Auto-add blocked — AUTO_DISCOVERY_AUTO_PROMOTE=false: %s",
                username or address[:12])
            return
    except Exception:
        return
    content = _read_settings()
    match = re.search(r'^FOLLOWED_TRADERS=(.*)$', content, re.MULTILINE)
    current = match.group(1).strip() if match else ""
    entry = "%s:%s" % (username, address) if username else address
    if address in current:
        return
    new_val = ("%s,%s" % (current, entry)).strip(",")
    pattern = r'^(FOLLOWED_TRADERS=).*$'
    content = re.sub(pattern, r'\g<1>' + new_val, content, flags=re.MULTILINE)

    # PATCH-024: Use _seed_tier_defaults for complete NEUTRAL tier seeding
    name = username or address[:12]
    content = _seed_tier_defaults(content, name)
    # Also seed AVG_TRADER_SIZE_MAP (not in _NEUTRAL_DEFAULTS)
    avg_m = re.search(r'^(AVG_TRADER_SIZE_MAP=)(.*)$', content, re.MULTILINE)
    if avg_m and name.lower() not in avg_m.group(2).lower():
        new_avg = ("%s,%s:20" % (avg_m.group(2).strip(), name)).strip(",")
        content = re.sub(r'^(AVG_TRADER_SIZE_MAP=).*$', r'\g<1>' + new_avg, content, flags=re.MULTILINE)

    _write_settings(content)
    logger.info("[LIFECYCLE] Added %s to FOLLOWED_TRADERS + seeded NEUTRAL tier defaults",
                username or address[:12])


def _remove_followed_trader(address: str, username: str):
    logger.info("[LIFECYCLE] Auto-remove disabled — settings managed manually: %s", username or address[:12])
    return  # DISABLED: settings managed manually
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


def ensure_followed_traders_seeded():
    """Upsert a LIVE_FOLLOW lifecycle row for every trader in FOLLOWED_TRADERS.

    Called at startup and at the start of each brain cycle so that primary
    followed traders actually appear in the lifecycle table. Without this,
    they only got lifecycle rows when brain.pause_trader paused them —
    meaning paper stats and lifecycle transitions never worked for them.
    """
    content = _read_settings()
    match = re.search(r'^FOLLOWED_TRADERS=(.*)$', content, re.MULTILINE)
    if not match:
        return 0
    raw = match.group(1).strip()
    if not raw:
        return 0
    seeded = 0
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" not in entry:
            # Legacy format (no address) — skip, nothing to upsert.
            continue
        username, address = entry.split(":", 1)
        username = username.strip()
        address = address.strip()
        if not address:
            continue
        existing = db.get_lifecycle_trader(address)
        if existing is None:
            db.upsert_lifecycle_trader(address, username, "LIVE_FOLLOW", "bootstrap")
            seeded += 1
            logger.info("[LIFECYCLE] Seeded %s (%s) as LIVE_FOLLOW", username, address[:12])
    return seeded
