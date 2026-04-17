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
import config

logger = logging.getLogger(__name__)

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'settings.env')


# Tier-Definitionen — baked-in defaults. Overridable via settings.env (TIER_* keys).
# Classification thresholds (pnl_7d, wr_7d) stay hardcoded in _classify_trader().
_TIER_DEFAULTS = {
    'star': {        # PnL > +10% portfolio, WR > 55%
        'bet_size': 0.07, 'exposure': 0.40, 'conviction': 0,
        'min_entry': 0.30, 'max_entry': 0.90, 'min_trader_usd': 3,
        'take_profit': 3.0, 'stop_loss': 0.60, 'max_copies': 3, 'hedge_wait': 30,
    },
    'solid': {       # PnL > +2% portfolio, WR > 45%
        'bet_size': 0.05, 'exposure': 0.25, 'conviction': 0,
        'min_entry': 0.35, 'max_entry': 0.90, 'min_trader_usd': 3,
        'take_profit': 2.5, 'stop_loss': 0.50, 'max_copies': 2, 'hedge_wait': 45,
    },
    'neutral': {     # PnL > -8% portfolio, WR > 35%
        'bet_size': 0.04, 'exposure': 0.15, 'conviction': 0,
        'min_entry': 0.35, 'max_entry': 0.90, 'min_trader_usd': 5,
        'take_profit': 2.0, 'stop_loss': 0.40, 'max_copies': 1, 'hedge_wait': 60,
    },
    'weak': {        # PnL > -15% portfolio
        'bet_size': 0.03, 'exposure': 0.08, 'conviction': 0.3,
        'min_entry': 0.38, 'max_entry': 0.85, 'min_trader_usd': 5,
        'take_profit': 1.5, 'stop_loss': 0.30, 'max_copies': 1, 'hedge_wait': 90,
    },
    'terrible': {    # PnL <= -15% portfolio
        'bet_size': 0.015, 'exposure': 0.03, 'conviction': 1.5,
        'min_entry': 0.42, 'max_entry': 0.75, 'min_trader_usd': 8,
        'take_profit': 1.0, 'stop_loss': 0.25, 'max_copies': 1, 'hedge_wait': 120,
    },
}

# Mapping field-name -> settings.env key. Each setting is a tier:value,tier:value string.
_TIER_FIELD_TO_ENV = {
    'bet_size':       'TIER_BET_SIZE',
    'exposure':       'TIER_EXPOSURE',
    'conviction':     'TIER_CONVICTION',
    'min_entry':      'TIER_MIN_ENTRY',
    'max_entry':      'TIER_MAX_ENTRY',
    'min_trader_usd': 'TIER_MIN_TRADER_USD',
    'take_profit':    'TIER_TAKE_PROFIT',
    'stop_loss':      'TIER_STOP_LOSS',
    'max_copies':     'TIER_MAX_COPIES',
    'hedge_wait':     'TIER_HEDGE_WAIT',
}

# Classification thresholds — percentage of portfolio for PnL, absolute for WR.
# All configurable via settings.env TIER_PNL_*/TIER_WR_* keys.
_CLASSIFY_DEFAULTS = {
    'pnl_star': 0.10,    # 7d PnL > +10% of portfolio
    'wr_star': 55,
    'pnl_solid': 0.02,   # 7d PnL > +2% of portfolio
    'wr_solid': 45,
    'pnl_neutral': -0.08, # 7d PnL > -8% of portfolio
    'wr_neutral': 35,
    'pnl_weak': -0.15,   # 7d PnL > -15% of portfolio
    # TERRIBLE = everything below WEAK
}

def _load_classify_thresholds():
    """Load classification thresholds from settings.env, fall back to _CLASSIFY_DEFAULTS."""
    thresholds = dict(_CLASSIFY_DEFAULTS)
    try:
        from bot.settings_lock import read_settings
        content = read_settings()
        for line in content.split('\n'):
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            k = k.strip().upper()
            v = v.strip()
            mapping = {
                'TIER_PNL_STAR': 'pnl_star', 'TIER_WR_STAR': 'wr_star',
                'TIER_PNL_SOLID': 'pnl_solid', 'TIER_WR_SOLID': 'wr_solid',
                'TIER_PNL_NEUTRAL': 'pnl_neutral', 'TIER_WR_NEUTRAL': 'wr_neutral',
                'TIER_PNL_WEAK': 'pnl_weak',
            }
            if k in mapping:
                try:
                    thresholds[mapping[k]] = float(v)
                except ValueError:
                    pass
    except Exception:
        pass
    return thresholds


def _parse_tier_map(raw: str) -> dict:
    """Parse 'star:0.30,solid:0.35,...' into {tier_name: float}. Invalid entries skipped."""
    result = {}
    if not raw:
        return result
    for entry in raw.split(','):
        entry = entry.strip()
        if ':' not in entry:
            continue
        k, v = entry.split(':', 1)
        try:
            result[k.strip().lower()] = float(v.strip())
        except ValueError:
            logger.warning("Auto-tuner: invalid tier value in '%s' — skipping", entry)
    return result


def _load_tiers() -> dict:
    """Build TIERS dict: start from _TIER_DEFAULTS, override with values from settings.env.

    Re-read on every auto_tune() call so changes to settings.env take effect without restart.
    """
    # Read current settings file
    try:
        from bot.settings_lock import read_settings
        content = read_settings()
    except Exception as e:
        logger.debug("auto_tuner: could not read settings (%s), using hardcoded defaults", e)
        return {k: dict(v) for k, v in _TIER_DEFAULTS.items()}

    # Extract TIER_* values from content
    env_vals = {}
    for line in content.split('\n'):
        line = line.strip()
        if '=' not in line or line.startswith('#'):
            continue
        k, _, v = line.partition('=')
        env_vals[k.strip()] = v.strip()

    tiers = {k: dict(v) for k, v in _TIER_DEFAULTS.items()}
    for field, env_key in _TIER_FIELD_TO_ENV.items():
        override_map = _parse_tier_map(env_vals.get(env_key, ''))
        for tier_name, value in override_map.items():
            if tier_name in tiers:
                tiers[tier_name][field] = value
    return tiers


def _classify_trader(pnl_7d, winrate_7d, trades_7d, pnl_30d, winrate_30d, portfolio_value=100):
    """Trader in Tier einordnen — Schwellen relativ zum Portfolio-Wert.

    Alle Schwellen konfigurierbar via settings.env TIER_PNL_*/TIER_WR_* Keys.
    PnL-Schwellen sind Prozent vom Portfolio (z.B. -0.08 = -8% = -$6.16 bei $77).
    """
    th = _load_classify_thresholds()
    pv = max(portfolio_value, 10)  # floor at $10 to avoid division issues

    if trades_7d < 3:
        # Wenig 7d-Daten: nutze 30d mit 2x breiteren Schwellen
        if pnl_30d > pv * th['pnl_solid'] * 2 and winrate_30d > th['wr_solid']:
            return 'solid'
        elif pnl_30d < pv * th['pnl_weak'] * 2:
            return 'weak'
        return 'neutral'

    if pnl_7d > pv * th['pnl_star'] and winrate_7d > th['wr_star']:
        return 'star'
    if pnl_7d > pv * th['pnl_solid'] and winrate_7d > th['wr_solid']:
        return 'solid'
    if pnl_7d > pv * th['pnl_neutral'] and winrate_7d > th['wr_neutral']:
        return 'neutral'
    if pnl_7d > pv * th['pnl_weak']:
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

    # PATCH-037b: Blacklist wenn Trader in Kategorie PnL < -$10 hat (keine WR/Trade-Anzahl Bedingung)
    blacklist = []
    for cat, data in by_cat.items():
        if data["cnt"] >= 1:
            wr = data["wins"] / data["cnt"] * 100
            if data["pnl"] < -10:
                blacklist.append(cat)

    return blacklist


def _read_settings():
    from bot.settings_lock import read_settings
    return read_settings()


def _update_map_setting(content, key, new_map):
    """Ein MAP-Setting updaten. Behaelt Kommentare."""
    map_str = ','.join('%s:%s' % (k, v) for k, v in sorted(new_map.items()))
    pattern = r'^(' + re.escape(key) + r'=)[^\n#]*'
    if re.search(pattern, content, re.MULTILINE):
        content = re.sub(pattern, r'\g<1>' + map_str, content, flags=re.MULTILINE)
    return content


def _update_blacklist_setting(content, new_map):
    """CATEGORY_BLACKLIST_MAP updaten — merge with existing entries."""
    # Parse existing blacklist from content
    existing_map = {}
    for line in content.split("\n"):
        if line.startswith("CATEGORY_BLACKLIST_MAP="):
            val = line.split("=", 1)[1].strip()
            if val:
                for part in val.split(","):
                    part = part.strip()
                    if ":" in part:
                        trader, cats_str = part.split(":", 1)
                        existing_map[trader.strip()] = set(c.strip() for c in cats_str.split("|") if c.strip())
            break
    # Merge: keep existing entries, add/extend with new_map
    merged = {}
    for trader in set(list(existing_map.keys()) + list(new_map.keys())):
        cats = set()
        if trader in existing_map:
            cats.update(existing_map[trader])
        if trader in new_map and new_map[trader]:
            cats.update(new_map[trader])
        if cats:
            merged[trader] = cats
    parts = []
    for trader, cats in sorted(merged.items()):
        if cats:
            parts.append('%s:%s' % (trader, '|'.join(sorted(cats))))
    map_str = ','.join(parts)
    pattern = r'^(CATEGORY_BLACKLIST_MAP=)[^\n]*'
    content = re.sub(pattern, r'\g<1>' + map_str, content, flags=re.MULTILINE)
    return content


def auto_tune():
    """Analysiere alle Trader und passe ALLE Settings an.

    Respects AUTO_TUNER_MODE:
      disabled — return immediately
      readonly — compute + log recommendations to brain_decisions, no write
      active   — compute + write to settings.env
    """
    mode = getattr(config, "AUTO_TUNER_MODE", "disabled").strip().lower()
    if mode == "disabled":
        return

    with db.get_connection() as conn:
        traders = conn.execute(
            "SELECT DISTINCT wallet_username FROM copy_trades "
            "WHERE wallet_username != '' AND wallet_username != 'imported'"
        ).fetchall()

    trader_names = list(set(t["wallet_username"] for t in traders if t["wallet_username"]))
    if not trader_names:
        return

    # PATCH-037: Get portfolio value for percentage-based tier thresholds
    portfolio_value = config.STARTING_BALANCE  # fallback
    try:
        from bot.order_executor import get_wallet_balance
        import requests
        cash = get_wallet_balance()
        r = requests.get("https://data-api.polymarket.com/positions", params={
            "user": config.POLYMARKET_FUNDER, "limit": 500, "sizeThreshold": 0
        }, timeout=15)
        pos_val = sum(float(p.get("currentValue", 0) or 0) for p in (r.json() if r.ok else []))
        portfolio_value = cash + pos_val
        logger.info("[TUNER] Portfolio value: $%.2f (cash $%.2f + positions $%.2f)", portfolio_value, cash, pos_val)
    except Exception as e:
        logger.warning("[TUNER] Portfolio fetch failed, using STARTING_BALANCE $%.0f: %s", portfolio_value, e)

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

        tier = _classify_trader(pnl7, wr7, cnt7, pnl30, wr30, portfolio_value=portfolio_value)
        blacklist = _get_category_blacklist(name)

        classifications[name] = {
            "tier": tier, "pnl_7d": pnl7, "wr_7d": wr7, "trades_7d": cnt7,
            "pnl_30d": pnl30, "wr_30d": wr30, "trades_30d": cnt30,
            "blacklist": blacklist,
        }

    # Load tier values fresh from settings.env (with hardcoded fallback)
    tiers = _load_tiers()

    # Pre-read existing MIN/MAX_ENTRY_PRICE_MAP values BEFORE building
    # the tier-default maps, so manual overrides in settings.env are
    # honored as the starting point. This prevents the auto-tuner from
    # silently resetting a hand-tuned value (e.g. xsaghav:0.30-0.85
    # from Option A) back to tier default just because the calibrator
    # has insufficient data to recommend a replacement.
    _existing_content_for_prices = _read_settings()
    _pre_existing_min: dict[str, float] = {}
    _pre_existing_max: dict[str, float] = {}
    for _line in _existing_content_for_prices.split("\n"):
        if _line.startswith("MIN_ENTRY_PRICE_MAP="):
            for _part in _line.split("=", 1)[1].split(","):
                if ":" in _part:
                    _k, _v = _part.split(":", 1)
                    try: _pre_existing_min[_k.strip()] = float(_v.strip())
                    except: pass
        if _line.startswith("MAX_ENTRY_PRICE_MAP="):
            for _part in _line.split("=", 1)[1].split(","):
                if ":" in _part:
                    _k, _v = _part.split(":", 1)
                    try: _pre_existing_max[_k.strip()] = float(_v.strip())
                    except: pass

    # Build ALL setting maps
    bet_map = {}
    exposure_map = {}
    conviction_map = {}
    min_entry_map = {}
    max_entry_map = {}
    min_usd_map = {}
    tp_map = {}
    sl_map = {}
    # If global STOP_LOSS_PCT is 0, user disabled stop-loss entirely — don't override per-trader
    _global_sl_off = config.STOP_LOSS_PCT <= 0
    copies_map = {}
    hedge_map = {}
    blacklist_map = {}

    for name, data in classifications.items():
        tier = data["tier"]
        s = tiers[tier]
        bet_map[name] = s["bet_size"]
        exposure_map[name] = s["exposure"]
        if s["conviction"] > 0:
            conviction_map[name] = s["conviction"]
        # Seed from existing manual value if present, else tier default.
        # Calibrator below may override with verified-PnL compute.
        min_entry_map[name] = _pre_existing_min.get(name, s["min_entry"])
        max_entry_map[name] = _pre_existing_max.get(name, s["max_entry"])
        min_usd_map[name] = s["min_trader_usd"]
        tp_map[name] = s["take_profit"]
        sl_map[name] = s["stop_loss"]
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

    # 2026-04-14 magnitude-aware price range compute, staged rollout.
    # Replaces the old tier-based WR heuristic (which clipped profitable
    # tails like xsaghav's +$41.65 30-40c bucket) with per-bucket
    # verified PnL. Only applies to traders with >= PRICE_CALIBRATOR_
    # MIN_TRADES verified samples — conservative gate to avoid 2-sample
    # false-positives on $106 equity where worst-case blast radius is
    # -$75/cycle if a tiny sample paints a wrong window.
    #
    # STAGED ROLLOUT PLAN (as of 2026-04-14):
    #   Today    threshold=100 → only sovereign2013 (n=130) auto-updates
    #   Week 2   lower to 50 if sovereign stable → xsaghav (73) + KING (~60) qualify
    #   Week 4   lower to 30 → fsavhlc (~25) + Jargs (~20) qualify
    #   Week 6+  lower to 20 → full autopilot
    # Each step is a 1-line edit (PRICE_CALIBRATOR_MIN_TRADES below).
    # If any step shows verified regression, raise the number back up.
    PRICE_CALIBRATOR_MIN_TRADES = 100
    try:
        from bot.price_range_calibrator import compute_verified_price_range
        from database import db as _db_pr
        for name in list(min_entry_map.keys()):
            try:
                computed = compute_verified_price_range(
                    _db_pr, name,
                    min_total_trades=PRICE_CALIBRATOR_MIN_TRADES,
                )
            except Exception as _e:
                logger.warning("[TUNER] %s price range compute failed: %s", name, _e)
                continue
            if computed is None:
                logger.info(
                    "[TUNER] %s price range: <%d verified trades, keeping tier default %.2f-%.2f",
                    name, PRICE_CALIBRATOR_MIN_TRADES, min_entry_map[name], max_entry_map[name],
                )
                continue
            old_min, old_max = min_entry_map[name], max_entry_map[name]
            min_entry_map[name], max_entry_map[name] = computed
            logger.info(
                "[TUNER] %s price range: tier=%.2f-%.2f -> verified=%.2f-%.2f (calibrator, n>=%d)",
                name, old_min, old_max, computed[0], computed[1], PRICE_CALIBRATOR_MIN_TRADES,
            )
    except Exception as _e:
        logger.warning("[TUNER] price range calibrator unavailable: %s", _e)

    # Preserve any existing non-followed entries (discovered wallets
    # like 0x3e5b23e9f7, aenews2) so auto_tuner output doesn't erase
    # them. The calibrator only covers classified/followed traders.
    existing_min: dict[str, float] = {}
    existing_max: dict[str, float] = {}
    for line in content.split("\n"):
        if line.startswith("MIN_ENTRY_PRICE_MAP="):
            for part in line.split("=", 1)[1].split(","):
                if ":" in part:
                    k, v = part.split(":", 1)
                    try: existing_min[k.strip()] = float(v.strip())
                    except: pass
        if line.startswith("MAX_ENTRY_PRICE_MAP="):
            for part in line.split("=", 1)[1].split(","):
                if ":" in part:
                    k, v = part.split(":", 1)
                    try: existing_max[k.strip()] = float(v.strip())
                    except: pass
    for k, v in existing_min.items():
        if k not in min_entry_map:
            min_entry_map[k] = v
    for k, v in existing_max.items():
        if k not in max_entry_map:
            max_entry_map[k] = v

    content = _update_map_setting(content, "MIN_ENTRY_PRICE_MAP", min_entry_map)
    content = _update_map_setting(content, "MAX_ENTRY_PRICE_MAP", max_entry_map)
    content = _update_map_setting(content, "MIN_TRADER_USD_MAP", min_usd_map)
    content = _update_map_setting(content, "TAKE_PROFIT_MAP", tp_map)
    content = _update_map_setting(content, "STOP_LOSS_MAP", sl_map)
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

    # CATEGORY_BLACKLIST_MAP — 2026-04-14: DISABLED auto-write, matches
    # piff-philosophy for auto-pause/throttle/kick. The auto_tuner's
    # blacklist computation reads full-history pnl_realized which still
    # contains pre-backfill formula-based losses, so it re-adds entries
    # for categories that are actually profitable on the verified subset
    # (e.g. xsaghav:cs +$70 verified was re-blacklisted every cycle). The
    # computed recommendations are still LOGGED above for visibility,
    # just not written to settings.env. Manual CATEGORY_BLACKLIST_MAP
    # edits stick now.
    if blacklist_map:
        logger.info("[TUNER] Would blacklist (DISABLED, manual): %s",
                    dict(sorted(blacklist_map.items())))

    # Build summary for both modes
    changes = []
    for name, data in sorted(classifications.items(), key=lambda x: x[1]["pnl_7d"], reverse=True):
        changes.append("%s=%s($%.0f)" % (name, data["tier"].upper(), data["pnl_7d"]))
    summary = " | ".join(changes)

    if mode == "readonly":
        for name, data in classifications.items():
            tier = data["tier"]
            detail = "tier=%s bet=%.2f exp=%.2f | 7d: %dt %.1f%%WR $%.2f" % (
                tier, bet_map.get(name, 0), exposure_map.get(name, 0),
                data["trades_7d"], data["wr_7d"], data["pnl_7d"])
            db.log_brain_decision(
                "TUNER_RECOMMENDATION", name, detail, "", summary)
        logger.info("[TUNER] READONLY — logged %d recommendations: %s",
                    len(classifications), summary)
        return

    if content != old_content:
        from bot.settings_lock import write_settings
        write_settings(content)
        logger.info("[TUNER] Settings updated: %s", summary)
        logger.info("[TUNER] Settings written — copy_trader will reload on next scan")
    else:
        logger.info("[TUNER] No changes needed")
