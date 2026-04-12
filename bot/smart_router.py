"""
Smart Router — verteilt Kapital dynamisch basierend auf Kategorie-Performance.
Rebalancing nur wenn sich die Daten geaendert haben. Floor 3%, Cap 35%.
"""
import logging
import json
import os
import hashlib

from database import db

logger = logging.getLogger(__name__)

ALLOCATION_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "allocation.json")
HASH_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "router_hash.txt")
DEFAULT_ALLOCATION = 0.10
MIN_ALLOCATION = 0.03
MAX_ALLOCATION = 0.35


def _load_allocations() -> dict:
    if os.path.exists(ALLOCATION_PATH):
        try:
            with open(ALLOCATION_PATH) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_allocations(allocs: dict):
    os.makedirs(os.path.dirname(ALLOCATION_PATH), exist_ok=True)
    with open(ALLOCATION_PATH, "w") as f:
        json.dump(allocs, f, indent=2)


def _get_data_hash(rows) -> str:
    """Hash der Kategorie-Daten um Aenderungen zu erkennen."""
    data_str = json.dumps([(r["category"], r["trades_count"], round(r["total_pnl"] or 0, 2))
                           for r in rows], sort_keys=True)
    return hashlib.md5(data_str.encode()).hexdigest()


def _load_last_hash() -> str:
    if os.path.exists(HASH_PATH):
        try:
            return open(HASH_PATH).read().strip()
        except Exception:
            return ""
    return ""


def _save_hash(h: str):
    os.makedirs(os.path.dirname(HASH_PATH), exist_ok=True)
    with open(HASH_PATH, "w") as f:
        f.write(h)


def get_category_allocation(category: str) -> float:
    allocs = _load_allocations()
    return allocs.get(category, DEFAULT_ALLOCATION)


def get_trader_category_multiplier(trader: str, category: str) -> float:
    """Per-Trader Category Multiplier — based on individual trader performance in this category."""
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt, "
            "SUM(CASE WHEN pnl_realized > 0 THEN 1 ELSE 0 END) as wins, "
            "COALESCE(SUM(pnl_realized), 0) as total_pnl "
            "FROM copy_trades WHERE wallet_username = ? AND status = 'closed' "
            "AND pnl_realized IS NOT NULL",
            (trader,)
        ).fetchone()

    # Not enough data for this trader — use global category multiplier
    total = row["cnt"] or 0
    if total < 5:
        return get_category_multiplier(category)

    # Now check this trader's performance in THIS category specifically
    from bot.copy_trader import _detect_category
    with db.get_connection() as conn:
        trades = conn.execute(
            "SELECT market_question, pnl_realized FROM copy_trades "
            "WHERE wallet_username = ? AND status = 'closed' AND pnl_realized IS NOT NULL",
            (trader,)
        ).fetchall()

    cat_pnl = 0
    cat_cnt = 0
    cat_wins = 0
    for t in trades:
        if _detect_category(t["market_question"] or "") == category:
            cat_cnt += 1
            cat_pnl += (t["pnl_realized"] or 0)
            if (t["pnl_realized"] or 0) > 0:
                cat_wins += 1

    # Not enough data in this category — use global
    if cat_cnt < 3:
        return get_category_multiplier(category)

    cat_wr = cat_wins / cat_cnt * 100

    # Calculate multiplier based on trader's performance in this category
    if cat_pnl > 5 and cat_wr > 55:
        mult = 2.0  # This trader CRUSHES this category
    elif cat_pnl > 0 and cat_wr > 50:
        mult = 1.5  # Profitable
    elif cat_pnl > -3 and cat_wr > 40:
        mult = 1.0  # Neutral
    elif cat_pnl > -10:
        mult = 0.5  # Losing in this category
    else:
        mult = 0.2  # Terrible in this category

    logger.debug("[ROUTER] %s x %s: %d trades, %.0f%% WR, $%.2f -> mult=%.1f",
                 trader, category, cat_cnt, cat_wr, cat_pnl, mult)
    return mult


def get_category_multiplier(category: str) -> float:
    """Multiplikator fuer Bet-Sizing basierend auf Kategorie-Allokation."""
    allocs = _load_allocations()
    if not allocs:
        return 1.0
    alloc = allocs.get(category, DEFAULT_ALLOCATION)
    avg_alloc = sum(allocs.values()) / len(allocs) if allocs else DEFAULT_ALLOCATION
    if avg_alloc <= 0:
        return 1.0
    return round(min(max(alloc / avg_alloc, 0.3), 2.5), 2)


def rebalance():
    """Rebalancing nur wenn sich die Kategorie-Daten geaendert haben."""
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT category, trades_count, total_pnl, winrate "
            "FROM category_performance WHERE period = '30d'"
        ).fetchall()

    if not rows:
        logger.info("[ROUTER] No category data yet, skipping rebalance")
        return

    # Check ob sich was geaendert hat
    current_hash = _get_data_hash(rows)
    last_hash = _load_last_hash()
    if current_hash == last_hash:
        logger.info("[ROUTER] No changes since last rebalance, skipping")
        return

    categories = {r["category"]: dict(r) for r in rows}
    scores = {}
    for cat, data in categories.items():
        trades = data.get("trades_count", 0) or 0
        if trades < 5:
            scores[cat] = 0
            continue
        pnl = data.get("total_pnl", 0) or 0
        wr = data.get("winrate", 50) or 50
        pnl_score = max(min(pnl / 20, 1), -1)
        wr_score = (wr - 50) / 50
        scores[cat] = 0.6 * pnl_score + 0.4 * wr_score

    if not scores:
        return

    min_score = min(scores.values())
    shifted = {cat: score - min_score + 0.1 for cat, score in scores.items()}
    total = sum(shifted.values())

    allocs = {}
    for cat, s in shifted.items():
        raw_alloc = s / total if total > 0 else DEFAULT_ALLOCATION
        allocs[cat] = round(max(MIN_ALLOCATION, min(MAX_ALLOCATION, raw_alloc)), 3)

    alloc_total = sum(allocs.values())
    if alloc_total > 0:
        allocs = {cat: round(a / alloc_total, 3) for cat, a in allocs.items()}
        # Re-clamp nach Normalisierung
        allocs = {cat: round(max(MIN_ALLOCATION, min(MAX_ALLOCATION, a)), 3) for cat, a in allocs.items()}

    _save_allocations(allocs)
    _save_hash(current_hash)
    logger.info("[ROUTER] Rebalanced: %s", allocs)

    # Logged to journal only, not dashboard activity feed
    pass
