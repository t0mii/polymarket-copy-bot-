"""
Brain Engine — Zentrales Intelligenz-Modul.
Laeuft alle 2 Stunden. Analysiert Verluste, passt Filter an,
managed Trader Lifecycle, bewertet Autonomous Performance.
"""
import logging
import json
import os
import re
import time
from datetime import datetime, timedelta

from database import db
import config

logger = logging.getLogger(__name__)

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'settings.env')
MIN_LIVE_TRADERS = 2

# Per-cycle mutex: traders that got TIGHTEN_FILTER this cycle cannot also
# receive RELAX_FILTER from _revert_obsolete_tightens. Reset at the top of
# every run_brain() call. Prevents the intra-cycle oscillation observed on
# 2026-04-13 09:28:57 where KING7777777 was simultaneously tightened (12
# BAD_PRICE losses) and relaxed (tier=neutral) 1 second apart.
_tightened_this_cycle: set = set()


def run_brain():
    logger.info("[BRAIN] === Brain Engine starting ===")
    _tightened_this_cycle.clear()
    try:
        _classify_losses()
        _check_trader_health()
        _optimize_score_weights()
        _check_autonomous_performance()
        try:
            from bot.trader_lifecycle import check_transitions
            check_transitions()
        except Exception as e:
            logger.warning("[BRAIN] Lifecycle error: %s", e)
        try:
            _revert_obsolete_blacklists()
            _revert_obsolete_tightens()
        except Exception as e:
            logger.warning("[BRAIN] Revert helpers error: %s", e)
        # PATCH-025: Auto-tuner AFTER reverts, so relaxations are not immediately undone
        try:
            from bot.auto_tuner import auto_tune
            auto_tune()
        except Exception as e:
            logger.warning("[BRAIN] Auto-tuner error: %s", e)
        logger.info("[BRAIN] === Brain Engine complete ===")
    except Exception as e:
        logger.exception("[BRAIN] Fatal error: %s", e)


def _classify_losses():
    # PERFORMANCE_SINCE gate: never classify losses from before the regime
    # change. Without this, brain re-applies old BAD_CATEGORY / BAD_PRICE
    # blocks every cycle based on stale pre-regime trades.
    performance_since = db.get_performance_since()
    with db.get_connection() as conn:
        if performance_since:
            losses = conn.execute(
                "SELECT * FROM copy_trades WHERE status = 'closed' AND pnl_realized < 0 "
                "AND closed_at >= datetime('now', '-7 days', 'localtime') "
                "AND closed_at >= ?",
                (performance_since,)
            ).fetchall()
        else:
            losses = conn.execute(
                "SELECT * FROM copy_trades WHERE status = 'closed' AND pnl_realized < 0 "
                "AND closed_at >= datetime('now', '-7 days', 'localtime')"
            ).fetchall()
    if not losses:
        logger.info("[BRAIN] No losses in last 7d")
        return
    losses = [dict(l) for l in losses]
    classifications = {"BAD_TRADER": [], "BAD_CATEGORY": [], "UNCLASSIFIED": [],
                       "BAD_PRICE": [], "BAD_SIZING": []}
    for loss in losses:
        trader = loss.get("wallet_username", "")
        category = loss.get("category", "")
        entry = loss.get("actual_entry_price") or loss.get("entry_price") or 0
        stats_7d = db.get_trader_rolling_pnl(trader, 7)
        trader_pnl_7d = stats_7d.get("total_pnl", 0) or 0
        if trader_pnl_7d < 0:
            classifications["BAD_TRADER"].append(loss)
            continue
        if category:
            with db.get_connection() as conn:
                if performance_since:
                    cat_row = conn.execute(
                        "SELECT COUNT(*) as cnt, "
                        "SUM(CASE WHEN pnl_realized > 0 THEN 1 ELSE 0 END) as wins "
                        "FROM copy_trades WHERE wallet_username = ? AND category = ? AND status = 'closed' "
                        "AND closed_at >= ?",
                        (trader, category, performance_since)
                    ).fetchone()
                else:
                    cat_row = conn.execute(
                        "SELECT COUNT(*) as cnt, "
                        "SUM(CASE WHEN pnl_realized > 0 THEN 1 ELSE 0 END) as wins "
                        "FROM copy_trades WHERE wallet_username = ? AND category = ? AND status = 'closed'",
                        (trader, category)
                    ).fetchone()
            cat_cnt = cat_row["cnt"] or 0
            cat_wins = cat_row["wins"] or 0
            if cat_cnt >= 5 and (cat_wins / cat_cnt * 100) < 40:
                classifications["BAD_CATEGORY"].append(loss)
                continue
        if entry < 0.25 or entry > 0.80:
            classifications["BAD_PRICE"].append(loss)
            continue
        classifications["UNCLASSIFIED"].append(loss)

    total_loss = sum(l.get("pnl_realized", 0) for l in losses)
    impacts = {}
    for cause, trades in classifications.items():
        if trades:
            cause_loss = sum(t.get("pnl_realized", 0) for t in trades)
            impacts[cause] = {
                "count": len(trades),
                "loss": round(cause_loss, 2),
                "pct_of_total": round(cause_loss / total_loss * 100, 1) if total_loss != 0 else 0,
            }
    for cause, impact in sorted(impacts.items(), key=lambda x: x[1]["loss"]):
        logger.info("[BRAIN] %s: %d trades, $%.2f loss (%.0f%%)",
                    cause, impact["count"], impact["loss"], impact["pct_of_total"])
    _execute_loss_actions(classifications, impacts)


def _execute_loss_actions(classifications: dict, impacts: dict):
    # BAD_CATEGORY: collapse to unique (trader, category) pairs so we write
    # one brain_decisions row and one settings update per UNIQUE rule, not
    # one per affected loss. Previously 5 identical losses wrote 5 rows.
    cat_pairs = set()
    for loss in classifications.get("BAD_CATEGORY", []):
        trader = loss.get("wallet_username", "")
        category = loss.get("category", "")
        if trader and category:
            cat_pairs.add((trader, category))
    for trader, category in sorted(cat_pairs):
        _add_category_blacklist(trader, category,
                               "Brain: %s WR < 40%% in %s" % (trader, category))

    # BAD_PRICE: still needs at least 3 losses to trigger, but tighten each
    # trader only once per cycle regardless of how many losses they had.
    price_by_trader = {}
    for loss in classifications.get("BAD_PRICE", []):
        trader = loss.get("wallet_username", "")
        if trader:
            price_by_trader.setdefault(trader, []).append(loss)
    tightened = set()
    for trader, trader_losses in price_by_trader.items():
        if len(trader_losses) >= 3 and trader not in tightened:
            _tighten_price_range(trader,
                                "Brain: %d BAD_PRICE losses for %s" % (len(trader_losses), trader))
            tightened.add(trader)
            _tightened_this_cycle.add(trader)


def _check_trader_health():
    # Keep lifecycle table in sync with FOLLOWED_TRADERS (picks up any
    # new traders added since startup via settings reload).
    try:
        from bot.trader_lifecycle import ensure_followed_traders_seeded
        ensure_followed_traders_seeded()
    except Exception as e:
        logger.debug("[BRAIN] lifecycle seed sync failed: %s", e)

    with db.get_connection() as conn:
        traders = conn.execute(
            "SELECT DISTINCT wallet_username FROM copy_trades "
            "WHERE wallet_username != '' AND status IN ('open', 'closed') "
            "AND created_at >= datetime('now', '-30 days', 'localtime')"
        ).fetchall()
    active_traders = [t["wallet_username"] for t in traders if t["wallet_username"]]

    def _current_live_count() -> int:
        """Re-read FOLLOWED_TRADERS from disk so pauses within this loop
        are reflected immediately. Prevents dropping below MIN_LIVE_TRADERS
        under a multi-pause cycle."""
        _content = _read_settings()
        _ft_match = re.search(r'^FOLLOWED_TRADERS=(.*)$', _content, re.MULTILINE)
        raw = _ft_match.group(1).strip() if _ft_match else ""
        if not raw:
            return 0
        return len([x for x in raw.split(",") if x.strip()])

    for trader in active_traders:
        stats_7d = db.get_trader_rolling_pnl(trader, 7)
        pnl_7d = stats_7d.get("total_pnl", 0) or 0
        cnt_7d = stats_7d.get("cnt", 0) or 0
        wins_7d = stats_7d.get("wins", 0) or 0
        with db.get_connection() as conn:
            recent = conn.execute(
                "SELECT pnl_realized FROM copy_trades "
                "WHERE wallet_username = ? AND status = 'closed' "
                "ORDER BY closed_at DESC LIMIT 5",
                (trader,)
            ).fetchall()
        streak = 0
        for r in recent:
            if (r["pnl_realized"] or 0) < 0:
                streak += 1
            else:
                break
        should_pause = False
        reason = ""
        if pnl_7d < -20:
            should_pause = True
            reason = "7d PnL $%.2f < -$20" % pnl_7d
        elif streak >= 5:
            should_pause = True
            reason = "%d consecutive losses" % streak
        if should_pause and _current_live_count() > MIN_LIVE_TRADERS:
            logger.info("[BRAIN] Would pause %s: %s (DISABLED — settings managed manually)", trader, reason)
            db.log_brain_decision("PAUSE_TRADER", trader, reason,
                                  json.dumps({"pnl_7d": pnl_7d, "streak": streak}),
                                  "Logged only — auto-pause disabled")
            # DISABLED: settings managed manually
            # try:
            #     from bot.trader_lifecycle import pause_trader
            #     pause_trader(trader, reason)
            # except Exception as e:
            #     logger.warning("[BRAIN] Failed to pause %s: %s", trader, e)
        elif pnl_7d > 5 and cnt_7d >= 5:
            wr = wins_7d / cnt_7d * 100 if cnt_7d > 0 else 0
            if wr > 60:
                logger.info("[BRAIN] BOOST %s: 7d PnL=$%.2f, WR=%.0f%%", trader, pnl_7d, wr)
                db.log_brain_decision("BOOST_TRADER", trader,
                                      "7d PnL=$%.2f, WR=%.0f%%" % (pnl_7d, wr),
                                      json.dumps({"pnl_7d": pnl_7d, "wr_7d": wr}),
                                      "Increase bet size for consistent winner")


def _optimize_score_weights():
    perf = db.get_score_range_performance()
    if not perf:
        return
    logger.info("[BRAIN] Score range performance:")
    for p in perf:
        total = p.get("total", 0)
        wins = p.get("wins", 0) or 0
        wr = round(wins / total * 100, 1) if total > 0 else 0
        logger.info("[BRAIN]   %s: %d trades, %d wins, %.1f%% WR, $%.2f PnL",
                    p["score_range"], total, wins, wr, p.get("total_pnl", 0))
    scores = db.get_trade_scores_with_outcomes(7)
    blocked_scores = [s for s in scores if s["action"] == "BLOCK" and s.get("outcome_pnl") is not None]
    if blocked_scores:
        blocked_would_win = sum(1 for s in blocked_scores if (s["outcome_pnl"] or 0) > 0)
        blocked_total = len(blocked_scores)
        blocked_wr = blocked_would_win / blocked_total * 100 if blocked_total > 0 else 0
        logger.info("[BRAIN] Blocked trades: %d total, %d would-have-won (%.1f%%)",
                    blocked_total, blocked_would_win, blocked_wr)
        if blocked_wr > 60 and blocked_total >= 5:
            from bot.trade_scorer import _load_weights, save_weights
            weights, thresholds = _load_weights()
            old_block = thresholds["block"]
            thresholds["block"] = max(25, thresholds["block"] - 5)
            save_weights(weights, thresholds)
            db.log_brain_decision("ADJUST_SCORE_THRESHOLD", "scorer",
                                  "Blocked trades had %.0f%% WR — lowering block threshold %d->%d" % (
                                      blocked_wr, old_block, thresholds["block"]),
                                  json.dumps({"blocked_wr": blocked_wr, "blocked_count": blocked_total}),
                                  "Capture more winning trades")
        elif blocked_wr < 30 and blocked_total >= 10:
            from bot.trade_scorer import _load_weights, save_weights
            weights, thresholds = _load_weights()
            old_block = thresholds["block"]
            thresholds["block"] = min(55, thresholds["block"] + 5)
            save_weights(weights, thresholds)
            db.log_brain_decision("ADJUST_SCORE_THRESHOLD", "scorer",
                                  "Blocked trades had only %.0f%% WR — raising block threshold %d->%d" % (
                                      blocked_wr, old_block, thresholds["block"]),
                                  json.dumps({"blocked_wr": blocked_wr, "blocked_count": blocked_total}),
                                  "Block more losing trades")


def _check_autonomous_performance():
    with db.get_connection() as conn:
        paper = conn.execute(
            "SELECT COUNT(*) as cnt, "
            "SUM(CASE WHEN pnl_realized > 0 THEN 1 ELSE 0 END) as wins, "
            "COALESCE(SUM(pnl_realized), 0) as total_pnl "
            "FROM autonomous_trades WHERE status = 'closed' "
            "AND closed_at >= datetime('now', '-14 days', 'localtime')"
        ).fetchone()
    cnt = paper["cnt"] or 0
    wins = paper["wins"] or 0
    pnl = paper["total_pnl"] or 0
    wr = round(wins / cnt * 100, 1) if cnt > 0 else 0
    logger.info("[BRAIN] Autonomous: %d trades, %d wins (%.1f%%), PnL=$%.2f", cnt, wins, wr, pnl)
    from datetime import date
    today = date.today().isoformat()
    mode = "live" if not _is_autonomous_paper() else "paper"
    db.log_autonomous_daily(today, mode, "ALL", cnt, wins, round(pnl, 2))
    if _is_autonomous_paper():
        if cnt >= 30 and wr > 55 and pnl > 0:
            logger.info("[BRAIN] AUTONOMOUS PROMOTE: Paper->Live")
            _set_autonomous_mode("live")
            db.log_brain_decision("AUTONOMOUS_PROMOTE", "autonomous",
                                  "Paper: %d trades, %.1f%% WR, $%.2f PnL" % (cnt, wr, pnl),
                                  json.dumps({"trades": cnt, "wr": wr, "pnl": pnl}),
                                  "Enable live autonomous trading at 10% budget")


def _is_autonomous_paper() -> bool:
    content = _read_settings()
    match = re.search(r'^AUTONOMOUS_PAPER_MODE=(.*)$', content, re.MULTILINE)
    val = match.group(1).strip().lower() if match else "true"
    return val in ("true", "1", "yes")


def _set_autonomous_mode(mode: str):
    value = "true" if mode == "paper" else "false"
    _update_setting("AUTONOMOUS_PAPER_MODE", value)


def _add_category_blacklist(trader: str, category: str, reason: str):
    content = _read_settings()
    match = re.search(r'^CATEGORY_BLACKLIST_MAP=(.*)$', content, re.MULTILINE)
    current = match.group(1) if match else ""
    bl_map = {}
    for entry in current.split(","):
        entry = entry.strip()
        if ":" in entry:
            t, cats = entry.split(":", 1)
            bl_map[t.strip()] = set(cats.split("|"))
    # Early out: already blacklisted -> no write, no log, no spam.
    if category in bl_map.get(trader, set()):
        return
    bl_map.setdefault(trader, set()).add(category)
    parts = []
    for t, cats in sorted(bl_map.items()):
        if cats:
            parts.append("%s:%s" % (t, "|".join(sorted(cats))))
    new_val = ",".join(parts)
    _update_setting("CATEGORY_BLACKLIST_MAP", new_val)
    db.log_brain_decision("BLACKLIST_CATEGORY", "%s/%s" % (trader, category), reason, "", "")
    logger.info("[BRAIN] Blacklisted %s for %s", category, trader)


def _tighten_price_range(trader: str, reason: str):
    content = _read_settings()
    min_map = _parse_map(content, "MIN_ENTRY_PRICE_MAP")
    max_map = _parse_map(content, "MAX_ENTRY_PRICE_MAP")
    old_min = min_map.get(trader, config.MIN_ENTRY_PRICE)
    old_max = max_map.get(trader, config.MAX_ENTRY_PRICE)
    new_min = round(old_min + 0.05, 2)
    new_max = round(old_max - 0.05, 2)
    if new_min >= new_max:
        return
    min_map[trader] = new_min
    max_map[trader] = new_max
    # PATCH: Re-read settings fresh before writing to avoid overwriting mid-cycle changes
    content = _read_settings()
    map_str = ",".join("%s:%s" % (k, v) for k, v in sorted(min_map.items()))
    pattern = r'^(MIN_ENTRY_PRICE_MAP=).*$'
    if re.search(pattern, content, re.MULTILINE):
        content = re.sub(pattern, r'\g<1>' + map_str, content, flags=re.MULTILINE)
    map_str2 = ",".join("%s:%s" % (k, v) for k, v in sorted(max_map.items()))
    pattern2 = r'^(MAX_ENTRY_PRICE_MAP=).*$'
    if re.search(pattern2, content, re.MULTILINE):
        content = re.sub(pattern2, r'\g<1>' + map_str2, content, flags=re.MULTILINE)
    _write_settings(content)
    db.log_brain_decision("TIGHTEN_FILTER", trader, reason,
                          json.dumps({"old_min": old_min, "old_max": old_max,
                                      "new_min": new_min, "new_max": new_max}),
                          "Reduce exposure to extreme price entries")
    logger.info("[BRAIN] Tightened %s: %.0f-%.0fc -> %.0f-%.0fc",
                trader, old_min*100, old_max*100, new_min*100, new_max*100)


def _revert_obsolete_blacklists():
    """Review CATEGORY_BLACKLIST_MAP and remove entries where the underlying
    condition no longer holds.

    A blacklist was originally added when trader+category had WR<40% over
    5+ closed trades. If 7d data now shows 3+ trades with WR>=50% and
    total_pnl>=0, the blacklist is obsolete and we remove it. Returns
    the number of reverts performed.
    """
    content = _read_settings()
    match = re.search(r'^CATEGORY_BLACKLIST_MAP=(.*)$', content, re.MULTILINE)
    if not match:
        return 0
    current = match.group(1)
    if not current.strip():
        return 0
    bl_map = {}
    for entry in current.split(","):
        entry = entry.strip()
        if ":" in entry:
            t, cats = entry.split(":", 1)
            bl_map[t.strip()] = set(cats.split("|"))
    if not bl_map:
        return 0

    reverts = 0
    with db.get_connection() as conn:
        for trader in list(bl_map.keys()):
            cats = bl_map[trader]
            for cat in list(cats):
                row = conn.execute(
                    "SELECT COUNT(*) as cnt, "
                    "SUM(CASE WHEN pnl_realized > 0 THEN 1 ELSE 0 END) as wins, "
                    "SUM(COALESCE(pnl_realized, 0)) as pnl "
                    "FROM copy_trades "
                    "WHERE wallet_username = ? AND category = ? "
                    "  AND status = 'closed' AND pnl_realized IS NOT NULL "
                    "  AND closed_at >= datetime('now','-7 days','localtime')",
                    (trader, cat)
                ).fetchone()
                cnt = row["cnt"] or 0
                wins = row["wins"] or 0
                pnl = row["pnl"] or 0
                if cnt >= 3 and wins / cnt >= 0.50 and pnl >= 0:
                    cats.discard(cat)
                    db.log_brain_decision(
                        "REVERT_BLACKLIST", "%s/%s" % (trader, cat),
                        "7d: %d trades, %d wins, $%.2f PnL — condition cleared" % (cnt, wins, pnl),
                        "", "Allow trader to trade this category again"
                    )
                    logger.info("[BRAIN] Reverted blacklist %s/%s", trader, cat)
                    reverts += 1
            if not cats:
                del bl_map[trader]

    if reverts > 0:
        parts = []
        for t, cats in sorted(bl_map.items()):
            if cats:
                parts.append("%s:%s" % (t, "|".join(sorted(cats))))
        new_val = ",".join(parts)
        _update_setting("CATEGORY_BLACKLIST_MAP", new_val)

    return reverts


def _revert_obsolete_tightens():
    """Relax MIN/MAX_ENTRY_PRICE_MAP for traders whose 7d PnL is back in
    positive territory.

    Walks each trader's current min/max one step (0.05) toward the tier
    default — never in one big jump. Returns the number of relaxations.
    """
    from bot.auto_tuner import _load_tiers, _classify_trader
    tiers = _load_tiers()
    content = _read_settings()  # PATCH-024: always re-read fresh to avoid stale data
    min_map = _parse_map(content, "MIN_ENTRY_PRICE_MAP")
    max_map = _parse_map(content, "MAX_ENTRY_PRICE_MAP")
    if not min_map and not max_map:
        return 0

    relaxes = 0
    for trader in set(list(min_map.keys()) + list(max_map.keys())):
        if trader in _tightened_this_cycle:
            logger.info("[BRAIN] Skipping RELAX for %s — was TIGHTENED this cycle", trader)
            continue
        stats_7d = db.get_trader_rolling_pnl(trader, 7)
        pnl_7d = stats_7d.get("total_pnl", 0) or 0
        cnt_7d = stats_7d.get("cnt", 0) or 0
        wins_7d = stats_7d.get("wins", 0) or 0
        wr_7d = (wins_7d / cnt_7d * 100) if cnt_7d > 0 else 0
        if pnl_7d <= 0 or cnt_7d < 3:
            continue

        stats_30d = db.get_trader_rolling_pnl(trader, 30)
        pnl_30d = stats_30d.get("total_pnl", 0) or 0
        cnt_30d = stats_30d.get("cnt", 0) or 0
        wr_30d = (stats_30d.get("wins", 0) / cnt_30d * 100) if cnt_30d > 0 else 50

        tier_name = _classify_trader(pnl_7d, wr_7d, cnt_7d, pnl_30d, wr_30d)
        tier_cfg = tiers.get(tier_name, {})
        tier_min = tier_cfg.get("min_entry")
        tier_max = tier_cfg.get("max_entry")
        if tier_min is None or tier_max is None:
            continue

        old_min = min_map.get(trader, tier_min)
        old_max = max_map.get(trader, tier_max)
        new_min = round(max(old_min - 0.05, tier_min), 2)
        new_max = round(min(old_max + 0.05, tier_max), 2)
        if new_min >= new_max:
            continue
        if new_min == old_min and new_max == old_max:
            continue
        min_map[trader] = new_min
        max_map[trader] = new_max
        db.log_brain_decision(
            "RELAX_FILTER", trader,
            "7d pnl=$%.2f wr=%.0f%% tier=%s" % (pnl_7d, wr_7d, tier_name),
            "",
            "Loosen price range toward tier default"
        )
        logger.info("[BRAIN] Relaxed %s price range: %.0f-%.0fc -> %.0f-%.0fc",
                    trader, old_min * 100, old_max * 100, new_min * 100, new_max * 100)
        relaxes += 1

    if relaxes > 0:
        # PATCH-024: Re-read settings fresh before writing to avoid overwriting mid-cycle changes
        content = _read_settings()
        map_str = ",".join("%s:%s" % (k, v) for k, v in sorted(min_map.items()))
        pattern = r'^(MIN_ENTRY_PRICE_MAP=).*$'
        if re.search(pattern, content, re.MULTILINE):
            content = re.sub(pattern, r'\g<1>' + map_str, content, flags=re.MULTILINE)
        map_str2 = ",".join("%s:%s" % (k, v) for k, v in sorted(max_map.items()))
        pattern2 = r'^(MAX_ENTRY_PRICE_MAP=).*$'
        if re.search(pattern2, content, re.MULTILINE):
            content = re.sub(pattern2, r'\g<1>' + map_str2, content, flags=re.MULTILINE)
        _write_settings(content)

    return relaxes


def _read_settings() -> str:
    from bot.settings_lock import read_settings
    return read_settings()


def _update_setting(key: str, value: str):
    content = _read_settings()
    if not content:
        return
    pattern = r'^(%s=).*$' % re.escape(key)
    if re.search(pattern, content, re.MULTILINE):
        content = re.sub(pattern, r'\g<1>' + value, content, flags=re.MULTILINE)
    else:
        content += "\n%s=%s\n" % (key, value)
    _write_settings(content)


def _parse_map(content: str, key: str) -> dict:
    match = re.search(r'^%s=(.*)$' % re.escape(key), content, re.MULTILINE)
    if not match:
        return {}
    result = {}
    for entry in match.group(1).split(","):
        entry = entry.strip()
        if ":" in entry:
            parts = entry.split(":", 1)
            try:
                result[parts[0].strip()] = float(parts[1].strip())
            except ValueError:
                pass
    return result


def _write_settings(content: str):
    from bot.settings_lock import write_settings
    write_settings(content)
