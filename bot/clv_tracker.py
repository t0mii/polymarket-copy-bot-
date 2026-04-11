"""
CLV Tracker — Closing Line Value.
Misst ob wir besser kaufen als der Schlusspreis.
Positiver CLV = echter Edge, negativer CLV = wir zahlen zu viel.
"""
import logging
import requests
from database import db

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"


def update_clv_for_closed_trades():
    """Berechne CLV fuer geschlossene Trades die noch keinen CLV-Wert haben."""
    with db.get_connection() as conn:
        # Trades die geschlossen sind aber noch keinen closing_price haben
        trades = conn.execute(
            "SELECT id, condition_id, side, entry_price, actual_entry_price, "
            "pnl_realized, current_price, market_question "
            "FROM copy_trades WHERE status = 'closed' AND condition_id != ''"
        ).fetchall()

    updated = 0
    total_clv = 0
    count = 0

    for t in trades:
        t = dict(t)
        cid = t["condition_id"]
        entry = t["actual_entry_price"] or t["entry_price"] or 0
        pnl = t["pnl_realized"] or 0
        if entry <= 0:
            continue

        # Use actual current_price as closing price, fallback to binary
        closing_price = t.get("current_price") if t.get("current_price") else (1.0 if pnl > 0 else 0.0)
        if closing_price is None or closing_price <= 0:
            continue

        # CLV = closing_price - entry_price (YES) or entry - closing_price (NO)
        side = (t.get("side") or "YES").upper()
        if side == "NO":
            clv = entry - closing_price
        else:
            clv = closing_price - entry
        total_clv += clv
        count += 1

    if count > 0:
        avg_clv = round(total_clv / count, 4)
        # Speichere CLV-Stats
        with db.get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO signal_performance "
                "(signal_type, trades_count, total_pnl, wins, losses, updated_at) "
                "VALUES ('clv_tracking', ?, ?, ?, 0, datetime('now','localtime'))",
                (count, round(avg_clv * 100, 2), int(avg_clv > 0))
            )
        logger.info("[CLV] Avg CLV: %.2f%% over %d trades (positive = edge!)",
                    avg_clv * 100, count)

    return {"avg_clv": round(total_clv / count * 100, 2) if count > 0 else 0,
            "trades": count}


def get_clv_by_trader():
    """CLV pro Trader berechnen."""
    with db.get_connection() as conn:
        trades = conn.execute(
            "SELECT wallet_username, side, entry_price, actual_entry_price, pnl_realized, current_price "
            "FROM copy_trades WHERE status = 'closed' AND pnl_realized IS NOT NULL"
        ).fetchall()

    by_trader = {}
    for t in trades:
        t = dict(t)
        trader = t["wallet_username"] or "?"
        entry = t["actual_entry_price"] or t["entry_price"] or 0
        pnl = t["pnl_realized"] or 0
        if entry <= 0:
            continue

        closing = t.get("current_price") if t.get("current_price") else (1.0 if pnl > 0 else 0.0)
        side = (t.get("side") or "YES").upper()
        clv = (entry - closing) if side == "NO" else (closing - entry)

        if trader not in by_trader:
            by_trader[trader] = {"total_clv": 0, "count": 0}
        by_trader[trader]["total_clv"] += clv
        by_trader[trader]["count"] += 1

    result = {}
    for trader, data in by_trader.items():
        if data["count"] > 0:
            result[trader] = round(data["total_clv"] / data["count"] * 100, 2)

    return result
