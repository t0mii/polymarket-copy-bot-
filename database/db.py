import sqlite3
import os
from contextlib import contextmanager

from database.models import SCHEMA
try:
    from database.models import SCHEMA_UPGRADE
except ImportError:
    SCHEMA_UPGRADE = ""
import config


def init_db():
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    with get_connection() as conn:
        # PATCH-034: WAL pragma moved to init_db()
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-8192")
        conn.executescript(SCHEMA)
        if SCHEMA_UPGRADE:
            conn.executescript(SCHEMA_UPGRADE)
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
            "ALTER TABLE copy_trades ADD COLUMN peak_price REAL",
            "ALTER TABLE copy_trades ADD COLUMN category TEXT DEFAULT ''",
            "ALTER TABLE copy_trades ADD COLUMN fee_bps INTEGER",
            "ALTER TABLE blocked_trades ADD COLUMN asset TEXT DEFAULT ''",
            "ALTER TABLE blocked_trades ADD COLUMN category TEXT DEFAULT ''",
            # 2026-04-14: separate train / test / copy-only / baseline accuracies + sample sizes
            "ALTER TABLE ml_training_log ADD COLUMN train_accuracy REAL",
            "ALTER TABLE ml_training_log ADD COLUMN copy_only_accuracy REAL",
            "ALTER TABLE ml_training_log ADD COLUMN baseline_accuracy REAL",
            "ALTER TABLE ml_training_log ADD COLUMN train_n INTEGER",
            "ALTER TABLE ml_training_log ADD COLUMN test_n INTEGER",
            "ALTER TABLE ml_training_log ADD COLUMN model_name TEXT DEFAULT 'ml_copy'",
            "CREATE INDEX IF NOT EXISTS idx_ml_training_log_model_name ON ml_training_log(model_name)",
        ]:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # column already exists

        # Verify critical columns exist (migration may have silently failed)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(copy_trades)").fetchall()}
        _critical = ["actual_entry_price", "actual_size", "shares_held", "usdc_received", "peak_price"]
        _missing = [c for c in _critical if c not in cols]
        if _missing:
            import logging as _log
            _log.getLogger(__name__).warning("DB MIGRATION: adding missing columns: %s", _missing)
            for col in _missing:
                conn.execute("ALTER TABLE copy_trades ADD COLUMN %s REAL" % col)


@contextmanager
def get_connection():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # PATCH-034: WAL pragma moved to init_db()
    conn.execute("PRAGMA foreign_keys=ON")
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


def add_followed_wallet(address, username):
    """Add a new wallet to follow (for auto-promoted traders)."""
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO wallets (address, username, followed, baseline_scanned) "
            "VALUES (?, ?, 1, 0)",
            (address, username)
        )
        # Also mark as followed if already exists
        conn.execute(
            "UPDATE wallets SET followed = 1, username = ? WHERE address = ?",
            (username, address)
        )


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
    trade.setdefault("category", "")
    trade.setdefault("fee_bps", None)
    with get_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO copy_trades (wallet_address, wallet_username, market_question,
                                      market_slug, side, entry_price, size, end_date,
                                      outcome_label, event_slug, condition_id,
                                      actual_entry_price, actual_size, shares_held, usdc_received,
                                      category, fee_bps)
            VALUES (:wallet_address, :wallet_username, :market_question,
                    :market_slug, :side, :entry_price, :size, :end_date,
                    :outcome_label, :event_slug, :condition_id,
                    :actual_entry_price, :actual_size, :shares_held, :usdc_received,
                    :category, :fee_bps)
        """, trade)
        return cursor.lastrowid


def get_open_copy_trades():
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM copy_trades WHERE status='open' ORDER BY created_at DESC"
        ).fetchall()]


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
            "UPDATE copy_trades SET current_price=?, pnl_unrealized=?, miss_count=0, "
            "peak_price = MAX(COALESCE(peak_price, 0), ?) WHERE id=?",
            (current_price, pnl_unrealized, current_price, trade_id)
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


def get_invested_for_event(event_slug: str) -> float:
    """Total invested on an event (open + closed within NO_REBUY window). Prevents rapid re-entry."""
    if not event_slug:
        return 0
    _window = max(config.NO_REBUY_MINUTES, 30) if config.NO_REBUY_MINUTES > 0 else 30
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(size), 0) as total FROM copy_trades WHERE event_slug=? "
            "AND (status='open' OR (status='closed' AND closed_at > datetime('now', '-' || ? || ' minutes', 'localtime')))",
            (event_slug, str(_window))
        ).fetchone()
        return row["total"] if row else 0


def get_invested_for_match(match_pattern: str) -> float:
    """Total invested on a match pattern (open + closed within NO_REBUY window). Prevents rapid re-entry."""
    if not match_pattern or len(match_pattern) <= 3:
        return 0
    _window = max(config.NO_REBUY_MINUTES, 30) if config.NO_REBUY_MINUTES > 0 else 30
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(size), 0) as total FROM copy_trades "
            "WHERE LOWER(market_question) LIKE ? "
            "AND (status='open' OR (status='closed' AND closed_at > datetime('now', '-' || ? || ' minutes', 'localtime')))",
            (match_pattern + '%', str(_window))
        ).fetchone()
        return row["total"] if row else 0


def get_trader_exposure(wallet_address: str) -> float:
    """Total invested by a trader (open + closed within NO_REBUY window). Prevents rapid re-entry."""
    _window = max(config.NO_REBUY_MINUTES, 30) if config.NO_REBUY_MINUTES > 0 else 30
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(size), 0) as total FROM copy_trades WHERE wallet_address=? "
            "AND (status='open' OR (status='closed' AND closed_at > datetime('now', '-' || ? || ' minutes', 'localtime')))",
            (wallet_address, str(_window))
        ).fetchone()
        return row["total"] if row else 0


def update_closed_trade_pnl(trade_id: int, pnl: float, usdc_received: float):
    """Correct P&L after actual sell fill is known (USDC delta)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE copy_trades SET pnl_realized=?, usdc_received=? WHERE id=?",
            (pnl, usdc_received, trade_id)
        )


def close_copy_trade(trade_id: int, pnl_realized: float, close_price: float = None,
                     usdc_received: float = None) -> bool:
    """Close a trade atomically — only if still open. Returns True if actually closed.

    When `usdc_received` is provided, the UPDATE also stores the capital-verified
    wallet delta in the same statement AND recomputes `pnl_realized` from it
    (usdc_received - COALESCE(actual_size, size)) so the column reflects the
    real wallet delta instead of a formula-computed estimate. This collapses
    what used to be a 2-step pattern (close_copy_trade then update_closed_trade_pnl)
    into a single atomic UPDATE, eliminating the race window that left 87% of
    historical rows with usdc_received=NULL. Caller should pass the usdc_received
    value from the sell_shares() response dict when available; passing None
    keeps the legacy formula-pnl behavior (used in paper mode and reconcile paths).
    """
    with get_connection() as conn:
        if usdc_received is not None and close_price is not None:
            rows = conn.execute(
                "UPDATE copy_trades SET "
                "status='closed', "
                "pnl_realized = round(? - COALESCE(actual_size, size, 0), 4), "
                "usdc_received = ?, "
                "current_price = ?, "
                "closed_at = datetime('now','localtime') "
                "WHERE id=? AND status='open'",
                (usdc_received, usdc_received, close_price, trade_id)
            ).rowcount
        elif usdc_received is not None:
            rows = conn.execute(
                "UPDATE copy_trades SET "
                "status='closed', "
                "pnl_realized = round(? - COALESCE(actual_size, size, 0), 4), "
                "usdc_received = ?, "
                "closed_at = datetime('now','localtime') "
                "WHERE id=? AND status='open'",
                (usdc_received, usdc_received, trade_id)
            ).rowcount
        elif close_price is not None:
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
    try:
        with get_connection() as conn:
            conn.execute(
                "UPDATE copy_trades SET status='open', pnl_realized=NULL, closed_at=NULL WHERE id=?",
                (trade_id,)
            )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("[REOPEN] UNIQUE constraint on trade %s: %s", trade_id, e)
        return None


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
    Counts OPEN trades + trades closed within NO_REBUY_MINUTES (min 30min) to prevent rapid re-entry.
    """
    _window = max(config.NO_REBUY_MINUTES, 30) if config.NO_REBUY_MINUTES > 0 else 30
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM copy_trades WHERE wallet_address=? AND condition_id=? "
            "AND (status='open' OR (status='closed' AND closed_at > datetime('now', '-' || ? || ' minutes', 'localtime')))",
            (wallet_address, condition_id, str(_window))
        ).fetchone()
        return row["cnt"] if row else 0


def is_market_already_open(condition_id: str, from_wallet: str = "", side: str = "") -> bool:
    """Ist dieser Market bereits offen von einem ANDEREN Trader (gleiche Seite)?

    Same trader double-down -> erlaubt (from_wallet ist ausgenommen)
    Anderer Trader kauft exakt dieselbe Seite -> geblockt (Duplikat)
    Anderer Trader kauft andere Seite (z.B. Over vs Under) -> erlaubt
    Also counts trades closed in the last 30 minutes (prevents rapid re-entry).
    """
    if not condition_id:
        return False
    _window = max(config.NO_REBUY_MINUTES, 30) if config.NO_REBUY_MINUTES > 0 else 30
    _window_mod = "-%d minutes" % int(_window)
    with get_connection() as conn:
        if from_wallet and side:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM copy_trades WHERE condition_id=? "
                "AND side=? "
                "AND (status='open' OR (status='closed' AND closed_at > datetime('now', ?, 'localtime'))) "
                "AND wallet_address!=?",
                (condition_id, side, _window_mod, from_wallet)
            ).fetchone()
        elif from_wallet:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM copy_trades WHERE condition_id=? "
                "AND (status='open' OR (status='closed' AND closed_at > datetime('now', ?, 'localtime'))) "
                "AND wallet_address!=?",
                (condition_id, _window_mod, from_wallet)
            ).fetchone()
        elif side:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM copy_trades WHERE condition_id=? "
                "AND side=? "
                "AND (status='open' OR (status='closed' AND closed_at > datetime('now', ?, 'localtime')))",
                (condition_id, side, _window_mod)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM copy_trades WHERE condition_id=? "
                "AND (status='open' OR (status='closed' AND closed_at > datetime('now', ?, 'localtime')))",
                (condition_id, _window_mod)
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
    """Snapshots in [start, end]. Honours PERFORMANCE_SINCE so a baseline reset
    wipes the displayed curve even if snapshots have been accumulated since."""
    with get_connection() as conn:
        performance_since = get_performance_since()
        if performance_since and performance_since > start:
            start = performance_since
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
        # conn.commit()  # PATCH-023: removed, context manager handles commit
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


# --- Blocked Trades ---

# In-memory dedup cache for log_blocked_trade. Prevents spamming
# blocked_trades with the same (trader, cid, reason) tuple every scan cycle
# when the trader's position hasn't moved. Process-local only — resets on
# bot restart, which is fine because the dedup window is small (60s).
#
# Without this, one sovereign2013 position on "Bucks vs 76ers O/U 225.5" that
# hit exposure_limit would log ~6 rows per 10-second scan = 36 rows/minute
# for the entire duration the trader holds the position. Observed peak:
# 11,372 blocked_trades rows in one 15-minute ralph iteration.
import time as _time_mod
_blocked_dedup_cache: dict = {}
_BLOCKED_DEDUP_TTL_SEC = 60
_BLOCKED_DEDUP_MAX_SIZE = 20000

# Same-pattern dedup for log_trade_score. Found 2026-04-13 iter 25: scorer
# runs every scan tick and writes one trade_scores row per call. Observed:
# 86 rows for a single (sovereign2013, Barcelona Open Buse vs Moutet, QUEUE)
# triple across 14 minutes, and score-range buckets were inflated to the
# point of looking "inverted" (80-100 bucket had 16 rows that were really
# only 4 unique trades duplicated 2-7 times each). Dedup makes the feedback
# cohort meaningful again.
_score_dedup_cache: dict = {}
_SCORE_DEDUP_TTL_SEC = 60
_SCORE_DEDUP_MAX_SIZE = 20000


def log_blocked_trade(trader: str, market_question: str, condition_id: str,
                      side: str, trader_price: float, block_reason: str,
                      block_detail: str = "", buy_path: str = "",
                      asset: str = "", category: str = ""):
    """Log a trade that was blocked by a filter.

    Deduped in-memory: if the same (trader, condition_id, block_reason) was
    logged within the last BLOCKED_DEDUP_TTL_SEC seconds, silently skip
    instead of writing another row. Cache is bounded; oldest entries are
    evicted when size exceeds _BLOCKED_DEDUP_MAX_SIZE.
    """
    now = _time_mod.time()
    key = (trader or "", condition_id or "", block_reason or "")
    last_ts = _blocked_dedup_cache.get(key, 0)
    if last_ts and (now - last_ts) < _BLOCKED_DEDUP_TTL_SEC:
        return  # recently logged — skip
    _blocked_dedup_cache[key] = now

    # Cache cleanup: drop entries older than 2 * TTL when size grows too large.
    if len(_blocked_dedup_cache) > _BLOCKED_DEDUP_MAX_SIZE:
        _cutoff = now - (2 * _BLOCKED_DEDUP_TTL_SEC)
        _blocked_dedup_cache.clear()
        # note: clearing fully is simpler than selective eviction; TTL re-fills
        # normal traffic within a minute
        _blocked_dedup_cache[key] = now

    with get_connection() as conn:
        conn.execute(
            "INSERT INTO blocked_trades (trader, market_question, condition_id, side, "
            "trader_price, block_reason, block_detail, buy_path, asset, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (trader, market_question[:200], condition_id, side, trader_price,
             block_reason, block_detail[:500], buy_path, asset, category)
        )


def get_blocked_trades_since(hours: int = 24, limit: int = 2000) -> list:
    """Get blocked trades from the last N hours."""
    _hours_mod = "-%d hours" % int(hours)
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM blocked_trades WHERE created_at > datetime('now', ?, 'localtime') "
            "ORDER BY created_at DESC LIMIT ?", (_hours_mod, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def get_blocked_trades_unchecked(limit: int = 500) -> list:
    """Get blocked trades that haven't had their outcome checked yet."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM blocked_trades WHERE outcome_price IS NULL "
            "AND condition_id != '' "
            "ORDER BY created_at ASC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def update_blocked_trade_outcome(trade_id: int, outcome_price: float, would_have_won: int):
    """Update a blocked trade with its outcome."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE blocked_trades SET outcome_price=?, would_have_won=?, "
            "checked_at=datetime('now','localtime') WHERE id=?",
            (outcome_price, would_have_won, trade_id)
        )


def get_blocked_trade_stats(hours: int = 24) -> dict:
    """Get aggregated stats on blocked trades."""
    _hours_mod = "-%d hours" % int(hours)
    with get_connection() as conn:
        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM blocked_trades "
            "WHERE created_at > datetime('now', ?, 'localtime')", (_hours_mod,)
        ).fetchone()["cnt"]
        by_reason = conn.execute(
            "SELECT block_reason, COUNT(*) as cnt FROM blocked_trades "
            "WHERE created_at > datetime('now', ?, 'localtime') "
            "GROUP BY block_reason ORDER BY cnt DESC", (_hours_mod,)
        ).fetchall()
        checked = conn.execute(
            "SELECT COUNT(*) as cnt, "
            "SUM(CASE WHEN would_have_won=1 THEN 1 ELSE 0 END) as winners "
            "FROM blocked_trades "
            "WHERE created_at > datetime('now', ?, 'localtime') "
            "AND would_have_won IS NOT NULL", (_hours_mod,)
        ).fetchone()
        by_trader = conn.execute(
            "SELECT trader, COUNT(*) as cnt, "
            "SUM(CASE WHEN would_have_won=1 THEN 1 ELSE 0 END) as winners, "
            "SUM(CASE WHEN would_have_won=0 THEN 1 ELSE 0 END) as losers "
            "FROM blocked_trades "
            "WHERE created_at > datetime('now', ?, 'localtime') "
            "AND would_have_won IS NOT NULL "
            "GROUP BY trader", (_hours_mod,)
        ).fetchall()
        return {
            "total": total,
            "by_reason": {r["block_reason"]: r["cnt"] for r in by_reason},
            "checked": checked["cnt"] or 0,
            "would_have_won": checked["winners"] or 0,
            "by_trader": {r["trader"]: {"total": r["cnt"], "winners": r["winners"] or 0, "losers": r["losers"] or 0} for r in by_trader},
        }


# --- AI Recommendations ---

def save_ai_recommendation(analysis_text: str, recommendations_json: str,
                           blocked_count: int, executed_count: int,
                           would_have_won_pct: float = None):
    """Save a new AI recommendation."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO ai_recommendations (analysis_text, recommendations_json, "
            "blocked_count, executed_count, would_have_won_pct) "
            "VALUES (?, ?, ?, ?, ?)",
            (analysis_text, recommendations_json, blocked_count, executed_count, would_have_won_pct)
        )


def get_latest_recommendation():
    """Get the most recent AI recommendation."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM ai_recommendations ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_recommendations(limit: int = 10):
    """Get recent AI recommendations."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM ai_recommendations ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# --- Trader Activity History ---

def store_trader_activity(activities: list[dict]):
    """Bulk-store trader activity (BUY + SELL trades). Ignores duplicates."""
    if not activities:
        return 0
    stored = 0
    with get_connection() as conn:
        for a in activities:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO trader_activity "
                    "(wallet_address, trader, condition_id, asset, trade_type, side, "
                    "price, usdc_size, market_question, market_slug, event_slug, category, timestamp) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (a["wallet_address"], a["trader"], a["condition_id"], a.get("asset", ""),
                     a["trade_type"], a.get("side", ""), a.get("price", 0), a.get("usdc_size", 0),
                     a.get("market_question", "")[:200], a.get("market_slug", ""),
                     a.get("event_slug", ""), a.get("category", ""), a["timestamp"])
                )
                stored += 1
            except Exception:
                pass  # duplicate or constraint violation
    return stored


def get_trader_activity_stats(trader: str = None, hours: int = 24) -> list:
    """Get aggregated trader activity stats by category and trade type."""
    with get_connection() as conn:
        sql = (
            "SELECT trader, category, trade_type, COUNT(*) as cnt, "
            "ROUND(SUM(usdc_size), 2) as total_usd, ROUND(AVG(price), 3) as avg_price "
            "FROM trader_activity WHERE timestamp > strftime('%%s', 'now', '-%d hours') " % hours
        )
        if trader:
            sql += "AND trader=? "
            sql += "GROUP BY trader, category, trade_type ORDER BY total_usd DESC"
            rows = conn.execute(sql, (trader,)).fetchall()
        else:
            sql += "GROUP BY trader, category, trade_type ORDER BY total_usd DESC"
            rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]


def get_trader_last_activity_ts(wallet_address: str) -> int:
    """Get the most recent trade timestamp stored for a trader."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(timestamp) as max_ts FROM trader_activity WHERE wallet_address=?",
            (wallet_address,)
        ).fetchone()
        return row["max_ts"] or 0 if row else 0


def update_recommendation_status(rec_id: int, status: str):
    """Mark a recommendation as applied or dismissed."""
    with get_connection() as conn:
        if status == "applied":
            conn.execute(
                "UPDATE ai_recommendations SET status='applied', applied_at=datetime('now','localtime') WHERE id=?",
                (rec_id,)
            )
        elif status == "dismissed":
            conn.execute(
                "UPDATE ai_recommendations SET status='dismissed', dismissed_at=datetime('now','localtime') WHERE id=?",
                (rec_id,)
            )


# =====================================================================
# UPGRADE: Performance, ML, Discovery, Autonomous — Helper Functions
# =====================================================================
from datetime import datetime, timedelta


def get_performance_since() -> str:
    """Return config.PERFORMANCE_SINCE as SQL timestamp string, or empty if unset.

    Empty string means "no filter" — caller should check and skip the
    AND-clause. This keeps backward compat when PERFORMANCE_SINCE is not
    configured.
    """
    try:
        import config
        ps = getattr(config, "PERFORMANCE_SINCE", "") or ""
        if ps:
            return datetime.fromisoformat(ps).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return ""


def get_trader_rolling_pnl(trader_name: str, days: int = 7, min_verified: int = 10) -> dict:
    """Rolling P&L fuer einen Trader ueber die letzten X Tage.

    Strategy: When the trader has >= min_verified trades with full fill data
    (usdc_received + actual_size), return ONLY those verified trades. Verified
    data is ground truth from wallet receipts. Otherwise fall back to ALL
    trades using pnl_realized (less accurate, includes formula-based estimates
    for old trades that lacked fill verification).

    If config.PERFORMANCE_SINCE is set, the effective cutoff is
    max(days-ago, PERFORMANCE_SINCE) — trades before the regime change are
    excluded from both verified and fallback branches.
    """
    with get_connection() as conn:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        performance_since = get_performance_since()
        if performance_since and performance_since > cutoff:
            cutoff = performance_since
        verified_count = conn.execute(
            "SELECT COUNT(*) FROM copy_trades WHERE wallet_username = ? AND status = 'closed' "
            "AND closed_at >= ? AND usdc_received IS NOT NULL AND actual_size IS NOT NULL",
            (trader_name, cutoff)
        ).fetchone()[0] or 0

        if verified_count >= min_verified:
            # Use ONLY verified trades — they're ground truth from wallet receipts
            row = conn.execute(
                "SELECT COUNT(*) as cnt, "
                "SUM(CASE WHEN (usdc_received - actual_size) > 0 THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN (usdc_received - actual_size) < 0 THEN 1 ELSE 0 END) as losses, "
                "ROUND(COALESCE(SUM(usdc_received - actual_size), 0), 2) as total_pnl "
                "FROM copy_trades WHERE wallet_username = ? AND status = 'closed' "
                "AND closed_at >= ? AND usdc_received IS NOT NULL AND actual_size IS NOT NULL",
                (trader_name, cutoff)
            ).fetchone()
            result = dict(row)
            result["verified_count"] = verified_count
            result["source"] = "verified_only"
            return result

        # Not enough verified data — fall back to all trades, formula-based PnL
        row = conn.execute(
            "SELECT COUNT(*) as cnt, "
            "SUM(CASE WHEN pnl_realized > 0 THEN 1 ELSE 0 END) as wins, "
            "SUM(CASE WHEN pnl_realized < 0 THEN 1 ELSE 0 END) as losses, "
            "COALESCE(SUM(pnl_realized), 0) as total_pnl "
            "FROM copy_trades WHERE wallet_username = ? AND status = 'closed' "
            "AND closed_at >= ?",
            (trader_name, cutoff)
        ).fetchone()
        result = dict(row) if row else {"cnt": 0, "wins": 0, "losses": 0, "total_pnl": 0}
        result["verified_count"] = verified_count
        result["source"] = "all_trades_fallback"
        return result


def upsert_trader_performance(trader: str, period: str, stats: dict):
    """Trader-Performance upserten."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO trader_performance (trader_name, period, trades_count, wins, losses, total_pnl, winrate, avg_pnl) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(trader_name, period) DO UPDATE SET "
            "trades_count=excluded.trades_count, wins=excluded.wins, losses=excluded.losses, "
            "total_pnl=excluded.total_pnl, winrate=excluded.winrate, avg_pnl=excluded.avg_pnl, "
            "calculated_at=datetime('now','localtime')",
            (trader, period, stats["cnt"], stats["wins"], stats["losses"],
             stats["total_pnl"], stats["winrate"], stats["avg_pnl"])
        )


def upsert_category_performance(category: str, period: str, stats: dict):
    """Kategorie-Performance upserten."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO category_performance (category, period, trades_count, wins, losses, total_pnl, winrate) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(category, period) DO UPDATE SET "
            "trades_count=excluded.trades_count, wins=excluded.wins, losses=excluded.losses, "
            "total_pnl=excluded.total_pnl, winrate=excluded.winrate, "
            "calculated_at=datetime('now','localtime')",
            (category, period, stats["cnt"], stats["wins"], stats["losses"],
             stats["total_pnl"], stats["winrate"])
        )


def get_category_rolling_pnl(category: str, days: int = 30) -> dict:
    """Rolling P&L fuer eine Kategorie aus category_performance."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM category_performance WHERE category = ? AND period = ?",
            (category, f"{days}d")
        ).fetchone()
        return dict(row) if row else {"trades_count": 0, "total_pnl": 0, "winrate": 0}


def get_trader_status(trader_name: str) -> dict:
    """Aktueller Status eines Traders (active/throttled/paused)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM trader_status WHERE trader_name = ?", (trader_name,)
        ).fetchone()
        if row:
            return dict(row)
        return {"status": "active", "bet_multiplier": 1.0, "reason": ""}


def set_trader_status(trader_name: str, status: str, multiplier: float, reason: str):
    """Trader-Status setzen."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO trader_status (trader_name, status, bet_multiplier, reason) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(trader_name) DO UPDATE SET "
            "status=excluded.status, bet_multiplier=excluded.bet_multiplier, "
            "reason=excluded.reason, updated_at=datetime('now','localtime')",
            (trader_name, status, multiplier, reason)
        )


def get_all_candidates(status=None):
    """Alle Trader-Kandidaten."""
    with get_connection() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM trader_candidates WHERE status = ? ORDER BY paper_pnl DESC",
                (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trader_candidates ORDER BY paper_pnl DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def upsert_candidate(address: str, username: str, profit: float, volume: float,
                     winrate: float, markets: int):
    """Kandidat einfuegen oder aktualisieren."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO trader_candidates (address, username, profit_total, volume_total, "
            "winrate, markets_traded) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(address) DO UPDATE SET "
            "username=excluded.username, profit_total=excluded.profit_total, "
            "volume_total=excluded.volume_total, winrate=excluded.winrate, "
            "markets_traded=excluded.markets_traded, last_checked_at=datetime('now','localtime')",
            (address, username, profit, volume, winrate, markets)
        )


def add_paper_trade(address: str, cid: str, question: str, side: str, price: float):
    """Paper-Trade hinzufuegen."""
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO paper_trades (candidate_address, condition_id, "
            "market_question, side, entry_price) VALUES (?, ?, ?, ?, ?)",
            (address, cid, question, side, price)
        )


def get_candidate_stats(address: str) -> dict:
    """Paper-Trade-Stats fuer einen Kandidaten."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
            "SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses, "
            "COALESCE(SUM(pnl), 0) as total_pnl "
            "FROM paper_trades WHERE candidate_address = ? AND status = 'closed'",
            (address,)
        ).fetchone()
        return dict(row) if row else {"total": 0, "wins": 0, "losses": 0, "total_pnl": 0}


# === Brain Engine Helpers ===

def log_brain_decision(action: str, target: str, reason: str, data: str = "",
                       expected_impact: str = "", dedup_hours: int = 3):
    """Insert a brain decision row, skipping duplicates within the last
    `dedup_hours` window for the same (action, target) pair.

    Brain runs every 2h and without this guard would write the same 5 rows
    (TIGHTEN KING, PAUSE sov/xsaghav/fsavhlc, RELAX KING) every cycle
    because the underlying conditions persist. 6+ cycles observed in one
    overnight session = 30+ cumulative duplicate rows for information that
    could be represented by 5 rows.

    Match is on (action, target) only, not reason — the reason often embeds
    changing numbers (e.g. "7d PnL $-135.55 < -$20") but the decision is
    functionally the same if the same action is taken on the same target.

    Set dedup_hours=0 to bypass the guard for any truly event-driven
    decision (e.g. a new KICK that must always be logged).
    """
    with get_connection() as conn:
        if dedup_hours > 0:
            cutoff = (datetime.now() - timedelta(hours=int(dedup_hours))).strftime("%Y-%m-%d %H:%M:%S")
            existing = conn.execute(
                "SELECT id FROM brain_decisions "
                "WHERE action = ? AND target = ? AND created_at >= ? LIMIT 1",
                (action, target, cutoff)
            ).fetchone()
            if existing:
                return  # same decision recently logged — skip
        conn.execute(
            "INSERT INTO brain_decisions (action, target, reason, data, expected_impact) "
            "VALUES (?, ?, ?, ?, ?)",
            (action, target, reason, data, expected_impact)
        )

def get_brain_decisions(limit: int = 50) -> list:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM brain_decisions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# === Trade Scorer Helpers ===

def log_trade_score(condition_id: str, trader_name: str, side: str, entry_price: float,
                    market_question: str, score_total: int, components: dict,
                    action: str, trade_id: int = None):
    """Write one trade_scores row. Deduped in-memory on (trader_name,
    condition_id, action) within _SCORE_DEDUP_TTL_SEC. NO_REBUY_MINUTES
    already guarantees at most one active NULL-outcome row per
    (trader, cid) pair, so skipping intermediate writes is safe —
    update_trade_score_outcome() resolves by newest NULL row.

    trade_id != None bypasses the dedup: when a buy actually lands and
    we want to stamp the score with its trade_id, always write.
    """
    if trade_id is None:
        now = _time_mod.time()
        key = (trader_name or "", condition_id or "", action or "")
        last_ts = _score_dedup_cache.get(key, 0)
        if last_ts and (now - last_ts) < _SCORE_DEDUP_TTL_SEC:
            return  # recently logged — skip
        _score_dedup_cache[key] = now
        if len(_score_dedup_cache) > _SCORE_DEDUP_MAX_SIZE:
            _score_dedup_cache.clear()
            _score_dedup_cache[key] = now

    with get_connection() as conn:
        conn.execute(
            "INSERT INTO trade_scores (condition_id, trader_name, side, entry_price, "
            "market_question, score_total, score_trader_edge, score_category_wr, "
            "score_price_signal, score_conviction, score_market_quality, score_correlation, "
            "action, trade_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (condition_id, trader_name, side, entry_price, market_question, score_total,
             components.get("trader_edge", 0), components.get("category_wr", 0),
             components.get("price_signal", 0), components.get("conviction", 0),
             components.get("market_quality", 0), components.get("correlation", 0),
             action, trade_id)
        )

def get_trade_scores_with_outcomes(days: int = 7) -> list:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT ts.*, ct.pnl_realized FROM trade_scores ts "
            "LEFT JOIN copy_trades ct ON ts.trade_id = ct.id "
            "WHERE ts.created_at >= datetime('now', '-%d days', 'localtime') "
            "ORDER BY ts.created_at DESC" % days
        ).fetchall()
        return [dict(r) for r in rows]

def get_score_range_performance() -> list:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT "
            "CASE "
            "  WHEN ts.score_total < 40 THEN '0-39' "
            "  WHEN ts.score_total < 60 THEN '40-59' "
            "  WHEN ts.score_total < 80 THEN '60-79' "
            "  ELSE '80-100' "
            "END as score_range, "
            "COUNT(*) as total, "
            "SUM(CASE WHEN ct.pnl_realized > 0 THEN 1 ELSE 0 END) as wins, "
            "SUM(CASE WHEN ct.pnl_realized <= 0 THEN 1 ELSE 0 END) as losses, "
            "ROUND(SUM(COALESCE(ct.pnl_realized, 0)), 2) as total_pnl "
            "FROM trade_scores ts "
            "LEFT JOIN copy_trades ct ON ts.trade_id = ct.id "
            "WHERE ts.trade_id IS NOT NULL AND ct.status = 'closed' "
            "GROUP BY score_range ORDER BY score_range"
        ).fetchall()
        return [dict(r) for r in rows]


def update_trade_score_outcome(condition_id: str, trader_name: str, pnl: float) -> int:
    """Write outcome_pnl onto the newest matching trade_scores row.

    Match: newest (highest id) trade_scores row with matching
    condition_id + trader_name + outcome_pnl IS NULL. Returns rowcount.

    NO_REBUY_MINUTES=120 guarantees at most one NULL-outcome score row
    per (condition_id, trader_name) at any moment in production, so the
    newest-by-id match is deterministic without a time window.
    """
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE trade_scores SET outcome_pnl = ? "
            "WHERE id = ("
            "    SELECT id FROM trade_scores "
            "    WHERE condition_id = ? AND trader_name = ? "
            "      AND outcome_pnl IS NULL "
            "    ORDER BY id DESC LIMIT 1"
            ")",
            (pnl, condition_id, trader_name)
        )
        return cur.rowcount


def backfill_trade_score_outcomes(days: int = 30) -> int:
    """Join trade_scores with closed copy_trades and fill missing outcome_pnl.

    Called periodically by outcome_tracker.track_outcomes. Scans the last
    `days` days of trade_scores rows with NULL outcome_pnl and fills them
    from the matching (condition_id, trader) copy_trades row's pnl_realized.
    Returns count updated.
    """
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE trade_scores SET outcome_pnl = ("
            "    SELECT ct.pnl_realized FROM copy_trades ct "
            "    WHERE ct.condition_id = trade_scores.condition_id "
            "      AND ct.wallet_username = trade_scores.trader_name "
            "      AND ct.status = 'closed' AND ct.pnl_realized IS NOT NULL "
            "    ORDER BY ct.id DESC LIMIT 1"
            ") "
            "WHERE outcome_pnl IS NULL "
            "  AND created_at >= datetime('now','-' || ? || ' days','localtime') "
            "  AND EXISTS ("
            "    SELECT 1 FROM copy_trades ct2 "
            "    WHERE ct2.condition_id = trade_scores.condition_id "
            "      AND ct2.wallet_username = trade_scores.trader_name "
            "      AND ct2.status = 'closed' AND ct2.pnl_realized IS NOT NULL"
            "  )",
            (days,)
        )
        return cur.rowcount


# === Trader Lifecycle Helpers ===

def get_lifecycle_trader(address: str) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM trader_lifecycle WHERE address = ?", (address,)
        ).fetchone()
        return dict(row) if row else None

def get_lifecycle_traders_by_status(status: str) -> list:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM trader_lifecycle WHERE status = ?", (status,)
        ).fetchall()
        return [dict(r) for r in rows]

def upsert_lifecycle_trader(address: str, username: str, status: str, source: str = ""):
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id, status, status_changed_at FROM trader_lifecycle WHERE address = ?", (address,)
        ).fetchone()
        if existing:
            # KICKED traders get 30 day cooldown before re-entry
            if existing["status"] == "KICKED":
                from datetime import datetime, timedelta
                changed = datetime.fromisoformat(existing["status_changed_at"]) if existing["status_changed_at"] else datetime.now()
                if (datetime.now() - changed).days < 49:
                    return  # Still in cooldown, ignore
                # Cooldown over — reset pause_count and let them back in
                conn.execute(
                    "UPDATE trader_lifecycle SET status = ?, username = ?, source = ?, "
                    "pause_count = 0, paper_trades = 0, paper_pnl = 0, paper_wr = 0, "
                    "status_changed_at = datetime('now','localtime') WHERE address = ?",
                    (status, username, source, address)
                )
                return
            conn.execute(
                "UPDATE trader_lifecycle SET status = ?, username = ?, "
                "status_changed_at = datetime('now','localtime') WHERE address = ?",
                (status, username, address)
            )
        else:
            conn.execute(
                "INSERT INTO trader_lifecycle (address, username, status, source) "
                "VALUES (?, ?, ?, ?)",
                (address, username, status, source)
            )

def update_lifecycle_status(address: str, status: str, notes: str = ""):
    import json as _json
    with get_connection() as conn:
        old = conn.execute(
            "SELECT notes, pause_count FROM trader_lifecycle WHERE address = ?", (address,)
        ).fetchone()
        old_notes = []
        if old and old["notes"]:
            try:
                old_notes = _json.loads(old["notes"])
                if not isinstance(old_notes, list):
                    old_notes = []
            except (_json.JSONDecodeError, TypeError):
                old_notes = []
        if notes:
            from datetime import datetime
            old_notes.append({"ts": datetime.now().isoformat(), "status": status, "reason": notes})
        pause_increment = ""
        if status == "PAUSED":
            pause_increment = ", pause_count = pause_count + 1"
        conn.execute(
            "UPDATE trader_lifecycle SET status = ?, status_changed_at = datetime('now','localtime'), "
            "notes = ?" + pause_increment + " WHERE address = ?",
            (status, _json.dumps(old_notes), address)
        )

def update_lifecycle_paper_stats(address: str, paper_trades: int, paper_pnl: float, paper_wr: float):
    with get_connection() as conn:
        conn.execute(
            "UPDATE trader_lifecycle SET paper_trades = ?, paper_pnl = ?, paper_wr = ? WHERE address = ?",
            (paper_trades, paper_pnl, paper_wr, address)
        )

def set_lifecycle_pause_until(address: str, pause_until: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE trader_lifecycle SET pause_until = ? WHERE address = ?",
            (pause_until, address)
        )

def get_lifecycle_pause_count(address: str) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT pause_count FROM trader_lifecycle WHERE address = ?", (address,)
        ).fetchone()
        return row["pause_count"] if row else 0


# === Unified Trader State ===

def get_trader_effective_state(username: str) -> dict:
    """Combine trader_status (soft throttle) + trader_lifecycle (hard pause).

    Returns a dict with:
      - hard_status: lifecycle status ('LIVE_FOLLOW', 'PAUSED', 'KICKED', ...)
      - soft_status: trader_status ('active', 'throttled', 'paused')
      - multiplier: soft bet multiplier (0.0-1.0) from trader_status
      - is_paused: True if EITHER system reports paused/kicked
      - reasons: list of reason strings from both sources

    This is the canonical reader going forward. Writers are still split:
    bot.trader_lifecycle.pause_trader writes the hard lifecycle;
    bot.trader_performance writes the soft throttling. They are
    orthogonal axes representing different signals.
    """
    hard_status = "LIVE_FOLLOW"
    hard_reason = ""
    with get_connection() as conn:
        lc = conn.execute(
            "SELECT status, kick_reason, notes FROM trader_lifecycle "
            "WHERE username = ? ORDER BY id DESC LIMIT 1",
            (username,)
        ).fetchone()
        if lc:
            hard_status = lc["status"] or "LIVE_FOLLOW"
            hard_reason = lc["kick_reason"] or ""

    soft_status = "active"
    soft_multiplier = 1.0
    soft_reason = ""
    with get_connection() as conn:
        ts = conn.execute(
            "SELECT status, bet_multiplier, reason FROM trader_status "
            "WHERE trader_name = ?",
            (username,)
        ).fetchone()
        if ts:
            soft_status = ts["status"] or "active"
            soft_multiplier = float(ts["bet_multiplier"] or 1.0)
            soft_reason = ts["reason"] or ""

    is_paused = (
        hard_status in ("PAUSED", "KICKED") or
        soft_status == "paused"
    )
    reasons = [r for r in (hard_reason, soft_reason) if r]

    return {
        "hard_status": hard_status,
        "soft_status": soft_status,
        "multiplier": soft_multiplier,
        "is_paused": is_paused,
        "reasons": reasons,
    }


def is_trader_paused(username: str) -> bool:
    """Return True if the trader is paused in either lifecycle or trader_status.

    Convenience wrapper over get_trader_effective_state for call sites
    that only care about the boolean.
    """
    return get_trader_effective_state(username)["is_paused"]


# === Autonomous Performance Helpers ===

def log_autonomous_daily(date_str: str, mode: str, signal_type: str, trades: int, wins: int, pnl: float):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO autonomous_performance (date, mode, signal_type, trades, wins, pnl) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(date, mode, signal_type) DO UPDATE SET "
            "trades = excluded.trades, wins = excluded.wins, pnl = excluded.pnl",
            (date_str, mode, signal_type, trades, wins, pnl)
        )

def get_autonomous_performance(days: int = 14, mode: str = None) -> list:
    with get_connection() as conn:
        q = "SELECT * FROM autonomous_performance WHERE date >= date('now', '-%d days', 'localtime')" % days
        params = []
        if mode:
            q += " AND mode = ?"
            params.append(mode)
        q += " ORDER BY date DESC"
        rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]


# === Equity Curve Helper ===

def get_equity_curve(period: str = "all") -> list:
    """Equity curve data. Uses portfolio snapshots for short periods (4h/1d),
    daily aggregates for longer periods (1w/1m/all).

    All periods honour PERFORMANCE_SINCE — when the config key is set, rows
    before that timestamp are excluded so a baseline reset (advancing
    PERFORMANCE_SINCE to the current time) effectively clears the curve
    across every period without deleting historical copy_trades.
    """
    performance_since = get_performance_since()  # "" or "YYYY-MM-DD HH:MM:SS"
    with get_connection() as conn:
        if period in ("4h", "1d"):
            if period == "4h":
                cutoff = "-4 hours"
            else:
                cutoff = "-24 hours"
            if performance_since:
                rows = conn.execute(
                    "SELECT created_at, pnl_total FROM copy_portfolio "
                    "WHERE created_at >= datetime('now', ?, 'localtime') "
                    "AND created_at >= ? "
                    "ORDER BY created_at",
                    (cutoff, performance_since)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT created_at, pnl_total FROM copy_portfolio "
                    "WHERE created_at >= datetime('now', ?, 'localtime') "
                    "ORDER BY created_at",
                    (cutoff,)
                ).fetchall()
            return [{"date": r["created_at"], "value": round(r["pnl_total"] or 0, 2)} for r in rows]
        else:
            # Daily aggregates for 1w/1m/all
            if period == "1w":
                cutoff_sql = "AND closed_at >= datetime('now', '-7 days', 'localtime')"
            elif period == "1m":
                cutoff_sql = "AND closed_at >= datetime('now', '-30 days', 'localtime')"
            else:
                cutoff_sql = ""
            since_sql = ""
            params = []
            if performance_since:
                since_sql = "AND closed_at >= ? "
                params.append(performance_since)
            rows = conn.execute(
                "SELECT DATE(closed_at) as date, SUM(pnl_realized) as daily_pnl "
                "FROM copy_trades WHERE status = 'closed' AND closed_at IS NOT NULL "
                + cutoff_sql + " " + since_sql +
                " GROUP BY DATE(closed_at) ORDER BY date",
                tuple(params)
            ).fetchall()
            result = []
            cumulative = 0
            for r in rows:
                cumulative += (r["daily_pnl"] or 0)
                result.append({"date": r["date"], "value": round(cumulative, 2)})
            return result
