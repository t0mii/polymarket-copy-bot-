import sqlite3
import os
from contextlib import contextmanager

from database.models import SCHEMA
import config


def init_db():
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        # Migrations
        for migration in [
            "ALTER TABLE wallets ADD COLUMN roi REAL DEFAULT 0",
            "ALTER TABLE copy_trades ADD COLUMN outcome_label TEXT DEFAULT ''",
            "ALTER TABLE copy_trades ADD COLUMN event_slug TEXT DEFAULT ''",
            "ALTER TABLE copy_trades ADD COLUMN condition_id TEXT DEFAULT ''",
            "ALTER TABLE trader_scan_config ADD COLUMN last_closed_count INTEGER DEFAULT 0",
            "ALTER TABLE trader_scan_config ADD COLUMN last_trade_timestamp INTEGER DEFAULT 0",
            "ALTER TABLE copy_trades ADD COLUMN actual_entry_price REAL",
            "ALTER TABLE copy_trades ADD COLUMN actual_size REAL",
            "ALTER TABLE copy_trades ADD COLUMN shares_held REAL",
            "ALTER TABLE copy_trades ADD COLUMN usdc_received REAL",
        ]:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass


@contextmanager
def get_connection():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- Wallets ---

def upsert_wallet(wallet: dict):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO wallets (address, username, rank, volume, pnl, markets_traded,
                                 score, strategy_type, strengths, weaknesses,
                                 recommendation, reasoning, win_rate, total_trades,
                                 profile_url, last_scanned)
            VALUES (:address, :username, :rank, :volume, :pnl, :markets_traded,
                    :score, :strategy_type, :strengths, :weaknesses,
                    :recommendation, :reasoning, :win_rate, :total_trades,
                    :profile_url, datetime('now','localtime'))
            ON CONFLICT(address) DO UPDATE SET
                username=:username, rank=:rank, volume=:volume, pnl=:pnl,
                markets_traded=:markets_traded, score=:score, strategy_type=:strategy_type,
                strengths=:strengths, weaknesses=:weaknesses,
                recommendation=:recommendation, reasoning=:reasoning,
                win_rate=:win_rate, total_trades=:total_trades,
                profile_url=:profile_url, last_scanned=datetime('now','localtime')
        """, wallet)


def get_top_wallets(limit=20):
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM wallets ORDER BY score DESC, pnl DESC LIMIT ?",
            (limit,)
        ).fetchall()


def get_followed_wallets():
    with get_connection() as conn:
        return conn.execute("""
            SELECT w.*,
                   COALESCE(sc.last_trade_timestamp, 0) AS last_trade_timestamp
            FROM wallets w
            LEFT JOIN trader_scan_config sc ON sc.wallet_address = w.address
            WHERE w.followed=1
            ORDER BY w.score DESC
        """).fetchall()


def toggle_follow(address: str, followed: int):
    with get_connection() as conn:
        conn.execute(
            "UPDATE wallets SET followed=? WHERE address=?",
            (followed, address)
        )


def unfollow_all():
    """Unfollow all wallets. Baseline bleibt erhalten — wird nur fuer neue Wallets zurueckgesetzt."""
    with get_connection() as conn:
        conn.execute("UPDATE wallets SET followed=0")


def get_wallet(address: str):
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM wallets WHERE address=?", (address,)
        ).fetchone()


def get_wallet_count():
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) as cnt FROM wallets").fetchone()["cnt"]


def get_recommendation_stats():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT recommendation, COUNT(*) as cnt FROM wallets GROUP BY recommendation"
        ).fetchall()
        return {r["recommendation"]: r["cnt"] for r in rows}


# --- Scan History ---

def save_scan(scan: dict):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO scan_history (wallets_scanned, wallets_filtered,
                                       wallets_analyzed, top_score, report_path)
            VALUES (:wallets_scanned, :wallets_filtered, :wallets_analyzed,
                    :top_score, :report_path)
        """, scan)


def get_recent_scans(limit=10):
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM scan_history ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()


# --- Wallet Snapshots ---

def save_wallet_snapshot(snapshot: dict):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO wallet_snapshots (address, pnl, volume, win_rate, score, rank)
            VALUES (:address, :pnl, :volume, :win_rate, :score, :rank)
        """, snapshot)


def get_wallet_history(address: str, limit=30):
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM wallet_snapshots WHERE address=? ORDER BY created_at DESC LIMIT ?",
            (address, limit)
        ).fetchall()


# --- Positions ---

def create_copy_trade(trade: dict):
    # Ensure new columns have defaults for callers that don't set them
    trade.setdefault("actual_entry_price", None)
    trade.setdefault("actual_size", None)
    trade.setdefault("shares_held", None)
    trade.setdefault("usdc_received", None)
    with get_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO copy_trades (wallet_address, wallet_username, market_question,
                                      market_slug, side, entry_price, size, end_date,
                                      outcome_label, event_slug, condition_id,
                                      actual_entry_price, actual_size, shares_held, usdc_received)
            VALUES (:wallet_address, :wallet_username, :market_question,
                    :market_slug, :side, :entry_price, :size, :end_date,
                    :outcome_label, :event_slug, :condition_id,
                    :actual_entry_price, :actual_size, :shares_held, :usdc_received)
        """, trade)
        return cursor.lastrowid


def get_open_copy_trades():
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM copy_trades WHERE status='open' ORDER BY created_at DESC"
        ).fetchall()


def get_all_copy_trades_for_wallet(wallet_address: str):
    """Alle Trades (open + baseline) für eine Wallet — für Position-Diff-Check."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT condition_id FROM copy_trades WHERE wallet_address=? AND status IN ('open', 'baseline')",
            (wallet_address,)
        ).fetchall()


def get_all_copy_trades(limit=2000):
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM copy_trades WHERE status != 'baseline' ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()


def update_copy_trade_price(trade_id: int, current_price: float, pnl_unrealized: float):
    with get_connection() as conn:
        conn.execute(
            "UPDATE copy_trades SET current_price=?, pnl_unrealized=?, miss_count=0 WHERE id=?",
            (current_price, pnl_unrealized, trade_id)
        )


def increment_miss_count(trade_id: int) -> int:
    """Increment miss counter (position not found in wallet). Returns new count."""
    with get_connection() as conn:
        conn.execute("UPDATE copy_trades SET miss_count = miss_count + 1 WHERE id=?", (trade_id,))
        row = conn.execute("SELECT miss_count FROM copy_trades WHERE id=?", (trade_id,)).fetchone()
        return row["miss_count"] if row else 0


def reset_miss_count(trade_id: int):
    """Reset miss counter when position is found again."""
    with get_connection() as conn:
        conn.execute("UPDATE copy_trades SET miss_count=0 WHERE id=?", (trade_id,))


def update_copy_trade_outcome_label(trade_id: int, outcome_label: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE copy_trades SET outcome_label=? WHERE id=?",
            (outcome_label, trade_id)
        )


def update_copy_trade_condition_id(trade_id: int, condition_id: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE copy_trades SET condition_id=? WHERE id=?",
            (condition_id, trade_id)
        )


def update_copy_trade_end_date(trade_id: int, end_date: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE copy_trades SET end_date=? WHERE id=?",
            (end_date, trade_id)
        )


def update_closed_trade_pnl(trade_id: int, pnl: float, usdc_received: float):
    """Correct P&L after actual sell fill is known (USDC delta)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE copy_trades SET pnl_realized=?, usdc_received=? WHERE id=?",
            (pnl, usdc_received, trade_id)
        )


def close_copy_trade(trade_id: int, pnl_realized: float, close_price: float = None) -> bool:
    """Close a trade atomically — only if still open. Returns True if actually closed."""
    with get_connection() as conn:
        if close_price is not None:
            rows = conn.execute(
                "UPDATE copy_trades SET status='closed', pnl_realized=?, current_price=?, closed_at=datetime('now','localtime') "
                "WHERE id=? AND status='open'",
                (pnl_realized, close_price, trade_id)
            ).rowcount
        else:
            rows = conn.execute(
                "UPDATE copy_trades SET status='closed', pnl_realized=?, closed_at=datetime('now','localtime') "
                "WHERE id=? AND status='open'",
                (pnl_realized, trade_id)
            ).rowcount
        return rows > 0


def reopen_copy_trade(trade_id: int):
    """Re-open a copy trade that was incorrectly closed."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE copy_trades SET status='open', pnl_realized=NULL, closed_at=NULL WHERE id=?",
            (trade_id,)
        )


def get_copy_trade_stats():
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) as cnt FROM copy_trades").fetchone()["cnt"]
        closed = conn.execute("SELECT COUNT(*) as cnt FROM copy_trades WHERE status='closed'").fetchone()["cnt"]
        open_count = conn.execute("SELECT COUNT(*) as cnt FROM copy_trades WHERE status='open'").fetchone()["cnt"]
        wins = conn.execute(
            "SELECT COUNT(*) as cnt FROM copy_trades WHERE status='closed' AND pnl_realized > 0"
        ).fetchone()["cnt"]
        total_pnl = conn.execute(
            "SELECT COALESCE(SUM(pnl_realized), 0) as total FROM copy_trades WHERE status='closed'"
        ).fetchone()["total"]
        total_unrealized = conn.execute(
            "SELECT COALESCE(SUM(pnl_unrealized), 0) as total FROM copy_trades WHERE status='open'"
        ).fetchone()["total"]
        total_exposure = conn.execute(
            "SELECT COALESCE(SUM(size), 0) as total FROM copy_trades WHERE status='open'"
        ).fetchone()["total"]
        return {
            "total_trades": total,
            "closed_trades": closed,
            "open_trades": open_count,
            "wins": wins,
            "win_rate": round(wins / closed * 100, 1) if closed > 0 else 0,
            "total_pnl": total_pnl,
            "unrealized_pnl": total_unrealized,
            "total_exposure": total_exposure,
        }


def is_trade_duplicate(wallet_address: str, market_question: str, condition_id: str = ""):
    """Blockt NUR Baseline-Einträge (Positionen die vor Bot-Start existierten).

    Repeat-Käufe (Trader kauft gleichen Markt nochmal) werden ERLAUBT —
    wir kopieren das Verhalten 1:1. Open/Closed-Einträge blockieren nicht mehr.
    """
    with get_connection() as conn:
        if condition_id:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM copy_trades WHERE wallet_address=? AND condition_id=? AND status='baseline'",
                (wallet_address, condition_id)
            ).fetchone()
            if row["cnt"] > 0:
                return True
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM copy_trades WHERE wallet_address=? AND market_question=? AND condition_id='' AND status='baseline'",
            (wallet_address, market_question)
        ).fetchone()
        return row["cnt"] > 0


def count_copies_for_market(wallet_address: str, condition_id: str) -> int:
    """Wie viele aktive Kopien haben wir von diesem Markt/Trader?
    Counts OPEN trades + trades closed in the last 30 minutes (prevents rapid re-entry loops).
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM copy_trades WHERE wallet_address=? AND condition_id=? "
            "AND (status='open' OR (status='closed' AND closed_at > datetime('now', '-30 minutes', 'localtime')))",
            (wallet_address, condition_id)
        ).fetchone()
        return row["cnt"] if row else 0


def is_market_already_open(condition_id: str, from_wallet: str = "") -> bool:
    """Ist dieser Market bereits offen von einem ANDEREN Trader?

    Same trader double-down → erlaubt (from_wallet ist ausgenommen)
    Anderer Trader kauft dasselbe → geblockt (Duplikat)
    Also counts trades closed in the last 30 minutes (prevents rapid re-entry).
    """
    if not condition_id:
        return False
    _status_filter = "(status='open' OR (status='closed' AND closed_at > datetime('now', '-30 minutes', 'localtime')))"
    with get_connection() as conn:
        if from_wallet:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM copy_trades WHERE condition_id=? AND %s AND wallet_address!=?" % _status_filter,
                (condition_id, from_wallet)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM copy_trades WHERE condition_id=? AND %s" % _status_filter,
                (condition_id,)
            ).fetchone()
        return row["cnt"] > 0 if row else False


def is_wallet_baselined(address: str) -> bool:
    with get_connection() as conn:
        row = conn.execute("SELECT baseline_scanned FROM wallets WHERE address=?", (address,)).fetchone()
        return row and row["baseline_scanned"] == 1


def set_wallet_baselined(address: str):
    with get_connection() as conn:
        conn.execute("UPDATE wallets SET baseline_scanned=1 WHERE address=?", (address,))


def set_wallet_unbaselined(address: str):
    with get_connection() as conn:
        conn.execute("UPDATE wallets SET baseline_scanned=0 WHERE address=?", (address,))


def clear_wallet_snapshot(address: str):
    """Delete all position snapshots for a wallet (forces fresh baseline)."""
    with get_connection() as conn:
        conn.execute("DELETE FROM trader_position_snapshots WHERE wallet_address=?", (address,))


def create_baseline_trade(trade: dict):
    """Save a position as baseline (known, not traded)."""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO copy_trades (wallet_address, wallet_username, market_question,
                                      market_slug, side, entry_price, size, status, end_date, outcome_label, event_slug, condition_id)
            VALUES (:wallet_address, :wallet_username, :market_question,
                    :market_slug, :side, :entry_price, 0, 'baseline', :end_date, :outcome_label, :event_slug, :condition_id)
        """, trade)


# --- Copy Portfolio ---

def save_copy_portfolio_snapshot(snapshot: dict):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO copy_portfolio (total_value, cash_balance, open_positions_value, pnl_total)
            VALUES (:total_value, :cash_balance, :open_positions_value, :pnl_total)
        """, snapshot)


def get_daily_copy_pnl() -> float:
    """Get today's realized PnL from closed trades."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl_realized), 0) as total FROM copy_trades "
            "WHERE status='closed' AND closed_at >= date('now','localtime')"
        ).fetchone()
        return row["total"]


def get_closed_copy_trades(limit=2000):
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM copy_trades WHERE status='closed' ORDER BY closed_at DESC LIMIT ?",
            (limit,)
        ).fetchall()


def get_copy_portfolio_snapshots(limit=168):
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM copy_portfolio ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()


def get_copy_snapshots_in_range(start: str, end: str):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM copy_portfolio WHERE created_at BETWEEN ? AND ? ORDER BY created_at ASC",
            (start, end)
        ).fetchall()
        return [dict(r) for r in rows]


def get_copy_trades_in_range(start: str, end: str):
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM copy_trades WHERE created_at BETWEEN ? AND ? ORDER BY created_at DESC",
            (start, end)
        ).fetchall()


# --- Position Tracking (for smart new position detection) ---

def save_position_snapshot(wallet_address: str, positions: list):
    """Save current position snapshot to detect changes.
    Replaces old snapshot completely — no UNIQUE constraint issues.
    """
    with get_connection() as conn:
        # Delete old snapshot for this wallet and replace with current state
        conn.execute("DELETE FROM trader_position_snapshots WHERE wallet_address=?", (wallet_address,))
        for pos in positions:
            cid = pos.get("condition_id", "")
            if not cid:
                continue
            conn.execute("""
                INSERT OR REPLACE INTO trader_position_snapshots
                (wallet_address, condition_id, market_question, side, size, current_price, is_open)
                VALUES (?, ?, ?, ?, ?, ?, 1)
            """, (wallet_address, cid, pos.get("market_question", ""), pos.get("side", ""),
                  pos.get("size", 0), pos.get("current_price", 0)))


def get_new_positions(wallet_address: str, current_positions: list) -> list:
    """Detect positions that are NEW (not in previous snapshots)."""
    with get_connection() as conn:
        # Get previously known open positions
        previous = conn.execute(
            "SELECT condition_id FROM trader_position_snapshots WHERE wallet_address=? AND is_open=1",
            (wallet_address,)
        ).fetchall()
        previous_cids = {row["condition_id"] for row in previous}
        
        # Find new ones
        new = []
        for pos in current_positions:
            cid = pos.get("condition_id", "")
            if cid and cid not in previous_cids:
                new.append(pos)
        
        return new


def get_position_count(wallet_address: str) -> int:
    """Get current open position count for a wallet."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM trader_position_snapshots WHERE wallet_address=? AND is_open=1",
            (wallet_address,)
        ).fetchone()
        return row["cnt"] if row else 0


# --- Closed Positions Tracking ---

def save_closed_positions(wallet_address: str, closed_positions: list):
    """Save trader's closed positions for matching with our copies."""
    with get_connection() as conn:
        for pos in closed_positions:
            cid = pos.get("condition_id", "")
            if not cid:
                continue
            
            conn.execute("""
                INSERT INTO trader_closed_positions 
                (wallet_address, condition_id, market_question, side, closed_price, pnl_actual)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet_address, condition_id) DO UPDATE SET
                    closed_price=excluded.closed_price,
                    pnl_actual=excluded.pnl_actual,
                    last_seen_at=datetime('now','localtime')
            """, (wallet_address, cid, pos.get("market_question", ""), pos.get("side", ""),
                  pos.get("closed_price", 0), pos.get("realized_pnl", 0)))


def get_trader_closed_position(wallet_address: str, condition_id: str) -> dict or None:
    """Check if trader has this position closed."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM trader_closed_positions WHERE wallet_address=? AND condition_id=?",
            (wallet_address, condition_id)
        ).fetchone()
        return dict(row) if row else None


def mark_closed_position_matched(wallet_address: str, condition_id: str):
    """Mark that we matched and closed our copy for this trader position."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE trader_closed_positions SET is_matched=1 WHERE wallet_address=? AND condition_id=?",
            (wallet_address, condition_id)
        )


# --- Dynamic Scan Intensity ---

def get_or_create_scan_config(wallet_address: str) -> dict:
    """Get scan config for wallet (for dynamic intensity)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM trader_scan_config WHERE wallet_address=?",
            (wallet_address,)
        ).fetchone()

        if row:
            keys = row.keys()
            return {
                "last_position_count": row["last_position_count"],
                "target_scan_count": row["target_scan_count"],
                "scans_completed": row["scans_completed"],
                "last_closed_count": row["last_closed_count"] if "last_closed_count" in keys else 0,
                "last_trade_timestamp": row["last_trade_timestamp"] if "last_trade_timestamp" in keys else 0,
            }

        # Create default config
        conn.execute(
            "INSERT INTO trader_scan_config (wallet_address, last_position_count, target_scan_count, scans_completed, last_closed_count, last_trade_timestamp) VALUES (?, 0, 100, 0, 0, 0)",
            (wallet_address,)
        )
        conn.commit()
        return {"last_position_count": 0, "target_scan_count": 100, "scans_completed": 0, "last_closed_count": 0, "last_trade_timestamp": 0}


def update_scan_intensity(wallet_address: str, current_position_count: int):
    """Update scan intensity based on position count.
    
    Logic: If trader has N positions, scan N+100 times
    E.g., 200 positions → scan 300 times
          40 positions → scan 140 times
    """
    with get_connection() as conn:
        target = current_position_count + 100
        conn.execute(
            "UPDATE trader_scan_config SET last_position_count=?, target_scan_count=?, scans_completed=0, scan_cycle_started_at=datetime('now','localtime') WHERE wallet_address=?",
            (current_position_count, target, wallet_address)
        )


def increment_scan_count(wallet_address: str) -> tuple:
    """Increment scan count, returns (scans_completed, target_scan_count)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE trader_scan_config SET scans_completed = scans_completed + 1 WHERE wallet_address=?",
            (wallet_address,)
        )
        row = conn.execute(
            "SELECT scans_completed, target_scan_count FROM trader_scan_config WHERE wallet_address=?",
            (wallet_address,)
        ).fetchone()
        if row:
            return row["scans_completed"], row["target_scan_count"]
        return 0, 100


def set_last_trade_timestamp(wallet_address: str, timestamp: int):
    """Set the last seen trade unix timestamp for a wallet."""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO trader_scan_config (wallet_address, last_trade_timestamp)
            VALUES (?, ?)
            ON CONFLICT(wallet_address) DO UPDATE SET last_trade_timestamp=excluded.last_trade_timestamp
        """, (wallet_address, timestamp))


def update_closed_count(wallet_address: str, closed_count: int):
    """Update the last known closed position count for a wallet."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE trader_scan_config SET last_closed_count=? WHERE wallet_address=?",
            (closed_count, wallet_address)
        )


# --- Confirmed New Positions ---

def save_confirmed_new_position(wallet_address: str, condition_id: str, market_question: str, side: str, entry_price: float):
    """Save a position that's confirmed as new and should be copied."""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO confirmed_new_positions 
            (wallet_address, condition_id, market_question, side, entry_price, is_confirmed, confirmed_at)
            VALUES (?, ?, ?, ?, ?, 1, datetime('now','localtime'))
            ON CONFLICT(wallet_address, condition_id) DO UPDATE SET is_confirmed=1, confirmed_at=datetime('now','localtime')
        """, (wallet_address, condition_id, market_question, side, entry_price))


def is_position_confirmed(wallet_address: str, condition_id: str) -> bool:
    """Check if position is confirmed as new."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT is_confirmed FROM confirmed_new_positions WHERE wallet_address=? AND condition_id=?",
            (wallet_address, condition_id)
        ).fetchone()
        return row and row["is_confirmed"] == 1 if row else False


# --- Reset ---

def reset_copy_trading():
    """Full reset: delete all copy trades, baselines, snapshots. Keep followed wallets."""
    with get_connection() as conn:
        conn.execute("DELETE FROM copy_trades")
        conn.execute("DELETE FROM copy_portfolio")
        conn.execute("DELETE FROM trader_position_snapshots")
        conn.execute("DELETE FROM trader_position_trace")
        conn.execute("DELETE FROM trader_closed_positions")
        conn.execute("DELETE FROM trader_scan_config")
        conn.execute("DELETE FROM confirmed_new_positions")
        conn.execute("UPDATE wallets SET baseline_scanned=0")
        conn.execute("UPDATE save_point SET value=50, is_stopped=0 WHERE id=1")


# --- Save Point ---

def get_save_point() -> dict:
    with get_connection() as conn:
        row = conn.execute("SELECT value, is_stopped FROM save_point WHERE id=1").fetchone()
        if row:
            return {"value": row["value"], "is_stopped": bool(row["is_stopped"])}
        return {"value": 50.0, "is_stopped": False}


def update_save_point(new_value: float, is_stopped: bool):
    with get_connection() as conn:
        conn.execute("UPDATE save_point SET value=?, is_stopped=? WHERE id=1",
                     (new_value, int(is_stopped)))


# --- Activity Log ---

def log_activity(event_type: str, icon: str, title: str, detail: str = "", pnl: float = 0):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO activity_log (event_type, icon, title, detail, pnl) VALUES (?, ?, ?, ?, ?)",
            (event_type, icon, title, detail, pnl)
        )


def get_activity_log(limit=100):
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM activity_log ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()


# --- AI Reports ---

def save_report(report_text: str, data_snapshot: str = ""):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO ai_reports (report_text, data_snapshot) VALUES (?, ?)",
            (report_text, data_snapshot))


def get_latest_report():
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM ai_reports ORDER BY created_at DESC LIMIT 1"
        ).fetchone()


def count_activities_since(timestamp: str) -> int:
    """Count activity log entries newer than the given timestamp."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM activity_log WHERE created_at > ?", (timestamp,)
        ).fetchone()
        return row[0] if row else 0


def get_reports(limit=10):
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM ai_reports ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
