"""
Auto-Tuner — passt ALLE Settings pro Trader automatisch an.
Laeuft alle 2 Stunden. Aendert settings.env direkt.

Angepasste Settings:
- BET_SIZE_MAP (Einsatzgroesse)
- TRADER_EXPOSURE_MAP (Max % vom Portfolio)
- MIN_CONVICTION_RATIO_MAP (Min Ueberzeugung)
- MIN_ENTRY_PRICE_MAP (Min Kaufpreis)
- MAX_ENTRY_PRICE_MAP (Max Kaufpreis)
- MIN_TRADER_USD_MAP (Min Trade-Groesse des Traders)
- TAKE_PROFIT_MAP (Take Profit %)
- CATEGORY_BLACKLIST_MAP (Kategorie-Sperren datenbasiert)
"""
import logging
import os
import re
from datetime import datetime, timedelta

from database import db

logger = logging.getLogger(__name__)

SETTINGS_PATH = '/root/polymarket-copy-bot/settings.env'


# Tier-Definitionen
TIERS = {
    'star': {        # 7d P&L > +$5, WR > 55%
        'bet_size': 0.07,
        'exposure': 0.40,
        'conviction': 0,
        'min_entry': 0.30,
        'max_entry': 0.85,
        'min_trader_usd': 3,
        'take_profit': 3.0, 'max_copies': 3, 'hedge_wait': 30,
    },
    'solid': {       # 7d P&L > $0, WR > 50%
        'bet_size': 0.05,
        'exposure': 0.25,
        'conviction': 0,
        'min_entry': 0.35,
        'max_entry': 0.80,
        'min_trader_usd': 5,
        'take_profit': 2.5, 'max_copies': 2, 'hedge_wait': 45,
    },
    'neutral': {
        'bet_size': 0.03, 'exposure': 0.10, 'conviction': 0,
        'min_entry': 0.38, 'max_entry': 0.75, 'min_trader_usd': 5,
        'take_profit': 2.0, 'max_copies': 1, 'hedge_wait': 60,
    },
    'weak': {
        'bet_size': 0.02, 'exposure': 0.03, 'conviction': 0.5,
        'min_entry': 0.42, 'max_entry': 0.70, 'min_trader_usd': 8,
        'take_profit': 1.5, 'max_copies': 1, 'hedge_wait': 90,
    },
    'terrible': {    # 7d P&L < -$10
        'bet_size': 0.01,
        'exposure': 0.005,
        'conviction': 3.0,
        'min_entry': 0.45,
        'max_entry': 0.65,
        'min_trader_usd': 10,
        'take_profit': 1.0, 'max_copies': 1, 'hedge_wait': 120,
    },
}


def _classify_trader(pnl_7d, winrate_7d, trades_7d, pnl_30d, winrate_30d):
    """Trader in Tier einordnen basierend auf 7d + 30d Daten."""
    if trades_7d < 3:
        # Wenig 7d-Daten: nutze 30d als Fallback
        if pnl_30d > 10 and winrate_30d > 52:
            return 'solid'
        elif pnl_30d < -20:
            return 'weak'
        return 'neutral'

    if pnl_7d > 5 and winrate_7d > 55:
        return 'star'
    if pnl_7d > 0 and winrate_7d > 50:
        return 'solid'
    if pnl_7d > -5 and winrate_7d > 45:
        return 'neutral'
    if pnl_7d > -10:
        return 'weak'
    return 'terrible'


def _get_category_blacklist(trader_name):
    """Berechne Kategorie-Blacklist basierend auf Daten."""
    from bot.copy_trader import _detect_category

    with db.get_connection() as conn:
        trades = conn.execute(
            "SELECT market_question, pnl_realized FROM copy_trades "
            "WHERE wallet_username = ? AND status = 'closed' AND pnl_realized IS NOT NULL",
            (trader_name,)
        ).fetchall()

    # P&L pro Kategorie
    by_cat = {}
    for t in trades:
        cat = _detect_category(t["market_question"] or "")
        if not cat:
            continue
        if cat not in by_cat:
            by_cat[cat] = {"pnl": 0, "cnt": 0, "wins": 0}
        by_cat[cat]["pnl"] += (t["pnl_realized"] or 0)
        by_cat[cat]["cnt"] += 1
        if (t["pnl_realized"] or 0) > 0:
            by_cat[cat]["wins"] += 1

    # Blacklist: Kategorien mit >5 Trades UND negativem P&L UND <40% WR
    blacklist = []
    for cat, data in by_cat.items():
        if data["cnt"] >= 5:
            wr = data["wins"] / data["cnt"] * 100
            if data["pnl"] < -3 and wr < 40:
                blacklist.append(cat)

    return blacklist


def _read_settings():
    try:
        with open(SETTINGS_PATH) as f:
            return f.read()
    except (FileNotFoundError, PermissionError) as e:
        logger.error('[TUNER] Cannot read settings: %s', e)
        return ''


def _update_map_setting(content, key, new_map):
    """Ein MAP-Setting updaten. Behaelt Kommentare."""
    map_str = ','.join('%s:%s' % (k, v) for k, v in sorted(new_map.items()))
    pattern = r'^(' + re.escape(key) + r'=)[^\n#]*'
    if re.search(pattern, content, re.MULTILINE):
        content = re.sub(pattern, r'\g<1>' + map_str, content, flags=re.MULTILINE)
    return content


def _update_blacklist_setting(content, new_map):
    """CATEGORY_BLACKLIST_MAP updaten."""
    parts = []
    for trader, cats in sorted(new_map.items()):
        if cats:
            parts.append('%s:%s' % (trader, '|'.join(sorted(cats))))
    map_str = ','.join(parts)
    pattern = r'^(CATEGORY_BLACKLIST_MAP=)[^\n]*'
    content = re.sub(pattern, r'\g<1>' + map_str, content, flags=re.MULTILINE)
    return content


def auto_tune():
    """Analysiere alle Trader und passe ALLE Settings an."""
    with db.get_connection() as conn:
        traders = conn.execute(
            "SELECT DISTINCT wallet_username FROM copy_trades "
            "WHERE wallet_username != '' AND wallet_username != 'imported'"
        ).fetchall()

    trader_names = list(set(t["wallet_username"] for t in traders if t["wallet_username"]))
    if not trader_names:
        return

    classifications = {}
    for name in trader_names:
        s7 = db.get_trader_rolling_pnl(name, 7)
        s30 = db.get_trader_rolling_pnl(name, 30)
        cnt7 = s7.get("cnt", 0) or 0
        wins7 = s7.get("wins", 0) or 0
        pnl7 = s7.get("total_pnl", 0) or 0
        wr7 = round(wins7 / cnt7 * 100, 1) if cnt7 > 0 else 50
        cnt30 = s30.get("cnt", 0) or 0
        wins30 = s30.get("wins", 0) or 0
        pnl30 = s30.get("total_pnl", 0) or 0
        wr30 = round(wins30 / cnt30 * 100, 1) if cnt30 > 0 else 50

        tier = _classify_trader(pnl7, wr7, cnt7, pnl30, wr30)
        blacklist = _get_category_blacklist(name)

        classifications[name] = {
            "tier": tier, "pnl_7d": pnl7, "wr_7d": wr7, "trades_7d": cnt7,
            "pnl_30d": pnl30, "wr_30d": wr30, "trades_30d": cnt30,
            "blacklist": blacklist,
        }

    # Build ALL setting maps
    bet_map = {}
    exposure_map = {}
    conviction_map = {}
    min_entry_map = {}
    max_entry_map = {}
    min_usd_map = {}
    tp_map = {}
    copies_map = {}
    hedge_map = {}
    blacklist_map = {}

    for name, data in classifications.items():
        tier = data["tier"]
        s = TIERS[tier]
        bet_map[name] = s["bet_size"]
        exposure_map[name] = s["exposure"]
        if s["conviction"] > 0:
            conviction_map[name] = s["conviction"]
        min_entry_map[name] = s["min_entry"]
        max_entry_map[name] = s["max_entry"]
        min_usd_map[name] = s["min_trader_usd"]
        if s["take_profit"] != 2.0:
            tp_map[name] = s["take_profit"]
        copies_map[name] = s["max_copies"]
        hedge_map[name] = s["hedge_wait"]
        if data["blacklist"]:
            blacklist_map[name] = data["blacklist"]

        logger.info(
            "[TUNER] %s: %s | 7d: %dt %.1f%%WR $%.2f | 30d: %dt %.1f%%WR $%.2f | bl: %s",
            name, tier.upper(), data["trades_7d"], data["wr_7d"], data["pnl_7d"],
            data["trades_30d"], data["wr_30d"], data["pnl_30d"],
            data["blacklist"] or "none")

    # Update settings.env
    content = _read_settings()
    old_content = content

    content = _update_map_setting(content, "BET_SIZE_MAP", bet_map)
    content = _update_map_setting(content, "TRADER_EXPOSURE_MAP", exposure_map)
    content = _update_map_setting(content, "MIN_CONVICTION_RATIO_MAP", conviction_map)
    content = _update_map_setting(content, "MIN_ENTRY_PRICE_MAP", min_entry_map)
    content = _update_map_setting(content, "MAX_ENTRY_PRICE_MAP", max_entry_map)
    content = _update_map_setting(content, "MIN_TRADER_USD_MAP", min_usd_map)
    content = _update_map_setting(content, "TAKE_PROFIT_MAP", tp_map)
    content = _update_map_setting(content, "MAX_COPIES_PER_MARKET_MAP", copies_map)

    # HEDGE_WAIT_TRADERS: simple line replace
    hedge_str = ",".join("%s:%s" % (k, v) for k, v in sorted(hedge_map.items()))
    new_lines = []
    for line in content.split("\n"):
        if line.startswith("HEDGE_WAIT_TRADERS="):
            new_lines.append("HEDGE_WAIT_TRADERS=" + hedge_str)
        else:
            new_lines.append(line)
    content = "\n".join(new_lines)

    # CATEGORY_BLACKLIST_MAP
    content = _update_blacklist_setting(content, blacklist_map)

    if content != old_content:
        _tmp = SETTINGS_PATH + '.tmp'
        with open(_tmp, 'w') as f:
            f.write(content)
        os.replace(_tmp, SETTINGS_PATH)  # atomic rename
        changes = []
        for name, data in sorted(classifications.items(), key=lambda x: x[1]["pnl_7d"], reverse=True):
            changes.append("%s=%s($%.0f)" % (name, data["tier"].upper(), data["pnl_7d"]))
        summary = " | ".join(changes)
        logger.info("[TUNER] Settings updated: %s", summary)
        # Activity logged to journal only, not dashboard
        pass

        # Auto-Restart disabled for safety (was killing running orders/DB writes)
        logger.warning("[TUNER] Settings changed — restart recommended to apply new values")
    else:
        logger.info("[TUNER] No changes needed")
