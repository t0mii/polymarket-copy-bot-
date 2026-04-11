"""Fix activity log entries from existing trade data."""
from database.db import init_db, get_connection


def main():
    init_db()

    with get_connection() as conn:
        conn.execute("DELETE FROM activity_log")

        all_t = conn.execute("SELECT * FROM copy_trades ORDER BY created_at ASC").fetchall()

        for r in all_t:
            q = (r["market_question"] or "")[:30]
            ep = r["entry_price"] or 0

            conn.execute(
                "INSERT INTO activity_log (event_type, icon, title, detail, pnl, created_at) VALUES (?,?,?,?,?,?)",
                ("buy", "BUY", "Copied trade from RN1",
                 "#%d %s %s @ %dc — $%.2f" % (r["id"], q, r["side"], ep * 100, r["size"]),
                 0, r["created_at"]))

            if r["status"] == "closed":
                pnl = r["pnl_realized"] or 0
                conn.execute(
                    "INSERT INTO activity_log (event_type, icon, title, detail, pnl, created_at) VALUES (?,?,?,?,?,?)",
                    ("resolved", "WIN" if pnl > 0 else "LOSS",
                     "Market resolved — %s" % ("won" if pnl > 0 else "lost"),
                     "#%d %s — P&L $%+.2f" % (r["id"], q, pnl),
                     pnl, r["created_at"]))

        conn.execute(
            "INSERT INTO activity_log (event_type, icon, title, detail, pnl) VALUES (?,?,?,?,?)",
            ("system", "SYS", "Bot running — LIVE mode", "Tracking RN1 | Data restored from Polymarket", 0))

        total = conn.execute("SELECT COUNT(*) as c FROM copy_trades").fetchone()["c"]
        closed = len([t for t in all_t if t["status"] == "closed"])
        opn = total - closed
        w = len([t for t in all_t if t["status"] == "closed" and (t["pnl_realized"] or 0) > 0])
        pnl = sum(t["pnl_realized"] or 0 for t in all_t if t["status"] == "closed")
        logs = conn.execute("SELECT COUNT(*) as c FROM activity_log").fetchone()["c"]

        print("Trades: %d (open=%d, closed=%d, wins=%d)" % (total, opn, closed, w))
        print("Realized P&L: $%.2f" % pnl)
        print("Activity log: %d entries" % logs)


if __name__ == "__main__":
    main()
