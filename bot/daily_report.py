"""
Daily Report — automatische Zusammenfassung aus Bot-Daten.
Kein AI noetig, alles aus der DB berechnet.
Laeuft um Mitternacht UTC.
"""
import logging
import json
from datetime import datetime, timedelta
from database import db

logger = logging.getLogger(__name__)


def generate_daily_report():
    """Tages-Report aus Bot-Daten generieren."""
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")

    with db.get_connection() as conn:
        # Heutige geschlossene Trades
        trades = conn.execute(
            "SELECT wallet_username, market_question, pnl_realized, side, "
            "entry_price, actual_entry_price, created_at, closed_at "
            "FROM copy_trades WHERE status = 'closed' AND closed_at >= ? "
            "ORDER BY pnl_realized DESC",
            (yesterday,)
        ).fetchall()

        # Trader Performance 7d
        perf = conn.execute(
            "SELECT tp.trader_name, tp.trades_count, tp.winrate, tp.total_pnl, "
            "ts.status as trader_status "
            "FROM trader_performance tp "
            "LEFT JOIN trader_status ts ON tp.trader_name = ts.trader_name "
            "WHERE tp.period = '7d' ORDER BY tp.total_pnl DESC"
        ).fetchall()

        # Offene Positionen
        open_info = conn.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(size), 0) as total "
            "FROM copy_trades WHERE status = 'open'"
        ).fetchone()

        # Blocked Trades
        blocked = conn.execute(
            "SELECT block_reason, COUNT(*) as cnt FROM blocked_trades "
            "WHERE created_at >= ? GROUP BY block_reason ORDER BY cnt DESC",
            (yesterday,)
        ).fetchall()

        # Candidates
        cand_info = conn.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN status='promoted' THEN 1 ELSE 0 END) as promoted "
            "FROM trader_candidates"
        ).fetchone()

    # Berechne Tages-P&L
    total_pnl = sum(t["pnl_realized"] or 0 for t in trades)
    wins = [t for t in trades if (t["pnl_realized"] or 0) > 0]
    losses = [t for t in trades if (t["pnl_realized"] or 0) < 0]
    trade_count = len(trades)
    winrate = round(len(wins) / trade_count * 100, 1) if trade_count > 0 else 0

    # Bester und schlechtester Trade
    best = trades[0] if trades else None
    worst = trades[-1] if trades else None

    # P&L pro Trader
    by_trader = {}
    for t in trades:
        name = t["wallet_username"] or "?"
        if name not in by_trader:
            by_trader[name] = {"pnl": 0, "cnt": 0, "wins": 0}
        by_trader[name]["pnl"] += (t["pnl_realized"] or 0)
        by_trader[name]["cnt"] += 1
        if (t["pnl_realized"] or 0) > 0:
            by_trader[name]["wins"] += 1

    # Build Report Text
    lines = []

    # Headline
    if total_pnl > 5:
        lines.append("PROFIT TAG! Der Bot hat heute $%.2f verdient!" % total_pnl)
        lines.append("Zeit fuer ein Bier in der Sauna! Prost!")
    elif total_pnl > 0:
        lines.append("Leichtes Plus heute: $%.2f" % total_pnl)
        lines.append("Kleine Schritte, aber in die richtige Richtung.")
    elif total_pnl == 0:
        lines.append("Ruhiger Tag. Keine geschlossenen Trades.")
    elif total_pnl > -5:
        lines.append("Leichtes Minus: $%.2f" % total_pnl)
        lines.append("Morgen wird besser. Erstmal in die Sauna.")
    else:
        lines.append("Roter Tag: $%.2f Verlust." % total_pnl)
        lines.append("Der Saunaofen brennt trotzdem weiter!")

    lines.append("")

    # Stats
    lines.append("=== TAGES-STATS ===")
    lines.append("Trades: %d (%d Wins, %d Losses)" % (trade_count, len(wins), len(losses)))
    lines.append("Winrate: %.1f%%" % winrate)
    lines.append("P&L: $%.2f" % total_pnl)
    lines.append("")

    # Pro Trader
    if by_trader:
        lines.append("=== PRO TRADER ===")
        for name, data in sorted(by_trader.items(), key=lambda x: x[1]["pnl"], reverse=True):
            wr = round(data["wins"] / data["cnt"] * 100) if data["cnt"] > 0 else 0
            emoji = "+" if data["pnl"] >= 0 else ""
            lines.append("  %s: %s$%.2f (%d Trades, %d%% WR)" % (name, emoji, data["pnl"], data["cnt"], wr))
        lines.append("")

    # Bester/Schlechtester
    if best and (best["pnl_realized"] or 0) > 0:
        lines.append("Bester Trade: %s | +$%.2f" % ((best["market_question"] or "")[:40], best["pnl_realized"]))
    if worst and (worst["pnl_realized"] or 0) < 0:
        lines.append("Schlechtester: %s | $%.2f" % ((worst["market_question"] or "")[:40], worst["pnl_realized"]))
    lines.append("")

    # Trader Status
    paused = [p for p in perf if db.is_trader_paused(p["trader_name"] or "")]
    if paused:
        lines.append("=== GESPERRT ===")
        for p in paused:
            lines.append("  %s: 7d P&L $%.2f (Saunaverbot!)" % (p["trader_name"], p["total_pnl"] or 0))
        lines.append("")

    # Offene Positionen
    lines.append("=== OFFENE POSITIONEN ===")
    lines.append("  %d Positionen, $%.2f investiert" % (open_info["cnt"] or 0, open_info["total"] or 0))
    lines.append("")

    # Blocked
    if blocked:
        lines.append("=== GEFILTERTE TRADES ===")
        for b in blocked:
            lines.append("  %s: %d geblockt" % (b["block_reason"], b["cnt"]))
        lines.append("")

    # Scouting
    lines.append("=== SCOUTING ===")
    lines.append("  %d Kandidaten beobachtet, %d promoted" % (
        cand_info["total"] or 0, cand_info["promoted"] or 0))

    # Sauna-Spruch
    import random
    sprueche = [
        "Nach dem Aufguss ist vor dem Aufguss. Morgen gehts weiter!",
        "90 Grad in der Sauna, 100%% Einsatz beim Trading. SSC Style!",
        "Der beste Trade ist der, den man nach einer guten Sauna macht.",
        "Prost! Auf morgen und bessere Trades!",
        "Was in der Sauna besprochen wird, bleibt in der Sauna. Die P&L leider nicht.",
        "Erst schwitzen, dann traden. Die SSC Philosophie.",
        "Der Bot schwitzt fuer uns. Wir sitzen in der Sauna. Teamwork!",
        "Kein Aufguss ohne Abkuehlung. Kein Verlust ohne Comeback!",
    ]
    lines.append("")
    lines.append("--- %s ---" % random.choice(sprueche))

    report_text = "\n".join(lines)

    # Speichere in DB
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO ai_reports (report_text, data_snapshot) VALUES (?, ?)",
            (report_text, json.dumps({
                "date": today, "pnl": round(total_pnl, 2),
                "trades": trade_count, "type": "daily_auto"
            }))
        )

    db.log_activity("daily_report", "REPORT",
                    "Daily Report: $%.2f P&L" % total_pnl,
                    "%d Trades, %.0f%% WR" % (trade_count, winrate),
                    round(total_pnl, 2))

    logger.info("[DAILY] Report generated: %d trades, P&L=$%.2f", trade_count, total_pnl)
    return report_text
