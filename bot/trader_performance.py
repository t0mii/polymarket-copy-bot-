"""
Trader Performance Tracker - berechnet Rolling-Performance und passt Trader-Status an.
Laeuft als Scheduler-Job alle 30 Minuten.
"""
import logging
from datetime import datetime, timedelta

from database import db

logger = logging.getLogger(__name__)

THROTTLE_PNL_7D = -10.0
PAUSE_PNL_7D = -20.0
UNPAUSE_PNL_7D = 0.0


def update_all_trader_stats():
    traders = set()
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT wallet_username FROM copy_trades WHERE wallet_username != ''"
        ).fetchall()
        traders = {r["wallet_username"] for r in rows if r["wallet_username"]}

    for trader in traders:
        for days, period in [(7, "7d"), (30, "30d")]:
            stats = db.get_trader_rolling_pnl(trader, days)
            cnt = stats.get("cnt", 0) or 0
            wins = stats.get("wins", 0) or 0
            losses = stats.get("losses", 0) or 0
            total_pnl = stats.get("total_pnl", 0) or 0
            winrate = round(wins / cnt * 100, 1) if cnt > 0 else 0
            avg_pnl = round(total_pnl / cnt, 2) if cnt > 0 else 0

            db.upsert_trader_performance(trader, period, {
                "cnt": cnt, "wins": wins, "losses": losses,
                "total_pnl": round(total_pnl, 2), "winrate": winrate, "avg_pnl": avg_pnl,
            })

        stats_7d = db.get_trader_rolling_pnl(trader, 7)
        pnl_7d = stats_7d.get("total_pnl", 0) or 0
        current = db.get_trader_status(trader)

        if pnl_7d <= PAUSE_PNL_7D:
            if current["status"] != "paused":
                db.set_trader_status(trader, "paused", 0.0,
                                     "Auto-paused: 7d P&L $%.2f" % pnl_7d)
                logger.warning("[PERF] %s PAUSED: 7d P&L $%.2f", trader, pnl_7d)
        elif pnl_7d <= THROTTLE_PNL_7D:
            if current["status"] != "throttled":
                db.set_trader_status(trader, "throttled", 0.5,
                                     "Auto-throttled: 7d P&L $%.2f" % pnl_7d)
                logger.warning("[PERF] %s THROTTLED: 7d P&L $%.2f", trader, pnl_7d)
        elif pnl_7d >= UNPAUSE_PNL_7D and current["status"] in ("paused", "throttled"):
            db.set_trader_status(trader, "active", 1.0,
                                 "Auto-restored: 7d P&L $%.2f" % pnl_7d)
            logger.info("[PERF] %s RESTORED: 7d P&L $%.2f", trader, pnl_7d)

    logger.info("[PERF] Updated stats for %d traders", len(traders))


def update_category_stats():
    from bot.copy_trader import _detect_category

    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT id, market_question, pnl_realized, closed_at FROM copy_trades "
            "WHERE status = 'closed' AND pnl_realized IS NOT NULL"
        ).fetchall()

    cat_trades = {}
    now = datetime.now()
    for row in rows:
        cat = _detect_category(row["market_question"] or "")
        if not cat:
            cat = "other"
        closed_at = row["closed_at"] or ""

        for days, period in [(7, "7d"), (30, "30d")]:
            cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
            if closed_at >= cutoff:
                key = (cat, period)
                if key not in cat_trades:
                    cat_trades[key] = {"cnt": 0, "wins": 0, "losses": 0, "total_pnl": 0}
                cat_trades[key]["cnt"] += 1
                pnl = row["pnl_realized"] or 0
                cat_trades[key]["total_pnl"] += pnl
                if pnl > 0:
                    cat_trades[key]["wins"] += 1
                elif pnl < 0:
                    cat_trades[key]["losses"] += 1

    for (cat, period), stats in cat_trades.items():
        stats["winrate"] = round(stats["wins"] / stats["cnt"] * 100, 1) if stats["cnt"] > 0 else 0
        db.upsert_category_performance(cat, period, stats)

    logger.info("[PERF] Updated category stats: %d categories",
                len(set(k[0] for k in cat_trades)))


def update_adaptive_stop_loss():
    """Analyze stop-loss effectiveness per trader and adjust settings."""
    import re

    with db.get_connection() as conn:
        # Find trades closed by stop-loss (P&L is roughly -stop_loss% of size)
        # A "false trigger" = market recovered after our stop-loss
        # We can detect this by checking if the current_price went back up
        # after we sold at stop-loss

        traders = conn.execute(
            "SELECT DISTINCT wallet_username FROM copy_trades "
            "WHERE wallet_username != '' AND status = 'closed'"
        ).fetchall()

    SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'settings.env')

    for trader_row in traders:
        trader = trader_row["wallet_username"]
        if not trader or trader == "imported":
            continue

        with db.get_connection() as conn:
            # Count stop-loss trades (loss > 20% of size)
            sl_trades = conn.execute(
                "SELECT COUNT(*) as cnt, ROUND(AVG(ABS(pnl_realized) / NULLIF(size, 0)), 3) as avg_loss_pct "
                "FROM copy_trades WHERE wallet_username = ? AND status = 'closed' "
                "AND pnl_realized < 0 AND size > 0 AND ABS(pnl_realized) / size > 0.15",
                (trader,)
            ).fetchone()

            # Count all losing trades
            all_losses = conn.execute(
                "SELECT COUNT(*) as cnt FROM copy_trades "
                "WHERE wallet_username = ? AND status = 'closed' AND pnl_realized < 0",
                (trader,)
            ).fetchone()

            # Count wins
            all_wins = conn.execute(
                "SELECT COUNT(*) as cnt FROM copy_trades "
                "WHERE wallet_username = ? AND status = 'closed' AND pnl_realized > 0",
                (trader,)
            ).fetchone()

        sl_count = sl_trades["cnt"] or 0
        total_losses = all_losses["cnt"] or 0
        total_wins = all_wins["cnt"] or 0
        avg_loss_pct = sl_trades["avg_loss_pct"] or 0.25

        if total_losses + total_wins < 10:
            continue  # Not enough data

        # Ratio of stop-loss triggers to total trades
        sl_ratio = sl_count / (total_losses + total_wins) if (total_losses + total_wins) > 0 else 0

        # If > 30% of trades hit stop-loss, it might be too tight
        # If < 5% hit stop-loss, it might be too loose
        if sl_ratio > 0.30:
            # Too many stop-losses — maybe widen for this trader
            logger.info("[ADAPTIVE-SL] %s: %.0f%% trades hit SL — consider widening", trader, sl_ratio * 100)
        elif sl_ratio < 0.05 and total_losses > 10:
            # Very few stop-losses but still losing — SL isn't helping
            logger.info("[ADAPTIVE-SL] %s: only %.0f%% hit SL — losses come from other paths", trader, sl_ratio * 100)

    logger.info("[ADAPTIVE-SL] Stop-loss analysis complete")
