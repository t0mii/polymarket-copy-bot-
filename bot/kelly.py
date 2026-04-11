"""
Kelly Criterion Bet Sizing + Win-Streak Boost + Correlation Filter.
Integriert sich in den _apply_upgrade_gates() Flow.
"""
import logging
import math
from datetime import datetime, timedelta
from database import db

logger = logging.getLogger(__name__)

# Kelly dampening: full Kelly is too aggressive, use fraction
KELLY_FRACTION = 0.25  # Quarter-Kelly (standard for volatile markets)
MIN_KELLY_TRADES = 30  # Min trades before Kelly kicks in

# Win-streak boost
STREAK_BOOST_THRESHOLD = 3  # 3+ wins in a row
STREAK_BOOST_MULT = 1.3     # 30% more on streaks
MAX_STREAK_BOOST = 1.5      # Cap at 50% boost

# Correlation filter
MAX_SAME_CATEGORY_OPEN = 5  # Max 5 open positions in same category


def get_kelly_multiplier(trader_name):
    """Calculate Kelly Criterion multiplier for a trader.
    Returns a multiplier (0.5 - 2.0) based on their edge.
    """
    with db.get_connection() as conn:
        r = conn.execute(
            "SELECT COUNT(*) as cnt, "
            "SUM(CASE WHEN pnl_realized > 0 THEN 1 ELSE 0 END) as wins, "
            "AVG(CASE WHEN pnl_realized > 0 THEN pnl_realized END) as avg_win, "
            "AVG(CASE WHEN pnl_realized < 0 THEN ABS(pnl_realized) END) as avg_loss "
            "FROM copy_trades WHERE wallet_username = ? AND status = 'closed'",
            (trader_name,)
        ).fetchone()

    cnt = r["cnt"] or 0
    if cnt < MIN_KELLY_TRADES:
        return 1.0  # Not enough data

    wins = r["wins"] or 0
    wr = wins / cnt if cnt > 0 else 0
    avg_win = r["avg_win"] or 0
    avg_loss = r["avg_loss"] or 0.01

    if avg_loss <= 0 or avg_win <= 0:
        return 1.0

    wl_ratio = avg_win / avg_loss
    kelly = wr - (1 - wr) / wl_ratio

    if kelly <= 0:
        return 0.5  # Negative edge: minimum sizing

    # Quarter-Kelly, clamped to 0.5 - 2.0
    mult = 1.0 + (kelly * KELLY_FRACTION)  # Quarter-Kelly (no artificial scaling)
    mult = max(0.5, min(2.0, mult))

    logger.debug("[KELLY] %s: wr=%.1f%% ratio=%.2f kelly=%.3f mult=%.2f",
                 trader_name, wr * 100, wl_ratio, kelly, mult)
    return round(mult, 2)


def get_streak_multiplier(trader_name):
    """Boost bets when trader is on a winning streak."""
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT pnl_realized FROM copy_trades "
            "WHERE wallet_username = ? AND status = 'closed' "
            "ORDER BY closed_at DESC LIMIT 10",
            (trader_name,)
        ).fetchall()

    if not rows:
        return 1.0

    streak = 0
    for r in rows:
        if (r["pnl_realized"] or 0) > 0:
            streak += 1
        else:
            break

    if streak >= STREAK_BOOST_THRESHOLD:
        mult = STREAK_BOOST_MULT + (streak - STREAK_BOOST_THRESHOLD) * 0.1
        mult = min(mult, MAX_STREAK_BOOST)
        logger.info("[STREAK] %s on %d-win streak! Boost: %.1fx", trader_name, streak, mult)
        return round(mult, 2)

    return 1.0


def check_correlation(category, max_open=MAX_SAME_CATEGORY_OPEN):
    """Check if we have too many open positions in the same category.
    Returns True if OK to trade, False if too correlated.
    """
    if not category:
        return True

    with db.get_connection() as conn:
        # Count open trades in same category
        rows = conn.execute(
            "SELECT market_question FROM copy_trades WHERE status = 'open'"
        ).fetchall()

    from bot.copy_trader import _detect_category
    same_cat = 0
    for r in rows:
        if _detect_category(r["market_question"] or "") == category:
            same_cat += 1

    if same_cat >= int(max_open):
        logger.info("[CORR] Too many %s positions open (%d/%d), skipping",
                    category, same_cat, max_open)
        return False

    return True
