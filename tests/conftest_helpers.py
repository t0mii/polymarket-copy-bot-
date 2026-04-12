"""Shared helpers for brain-fix unit tests.

Tests use real SQLite in a temp file because database/db.py reads
config.DB_PATH at import time. Each test creates a temp path and
points config.DB_PATH at it BEFORE importing database.db.
"""
import os
import sys
import tempfile
import importlib


def setup_temp_db() -> str:
    """Create a temp SQLite path, patch config.DB_PATH, init schema.

    Returns the temp path so tests can clean it up.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()

    # Patch config BEFORE db import so DB_PATH is the temp file
    import config
    config.DB_PATH = tmp.name

    # Force-reload database.db so it picks up new DB_PATH
    if "database.db" in sys.modules:
        importlib.reload(sys.modules["database.db"])
    from database import db
    db.init_db()

    return tmp.name


def teardown_temp_db(path: str):
    """Remove the temp DB file."""
    try:
        os.unlink(path)
    except OSError:
        pass


def insert_copy_trade(db_module, **fields):
    """Insert a copy_trades row with sensible defaults."""
    defaults = {
        "wallet_address": "0xdead",
        "wallet_username": "trader1",
        "market_question": "Will X happen?",
        "market_slug": "market-x",
        "side": "YES",
        "entry_price": 0.5,
        "current_price": 0.5,
        "size": 10.0,
        "pnl_unrealized": 0.0,
        "pnl_realized": 0.0,
        "status": "closed",
        "condition_id": "cid-test-1",
        "actual_entry_price": 0.5,
        "actual_size": 10.0,
        "shares_held": 20.0,
        "usdc_received": 10.0,
        "category": "cs",
    }
    defaults.update(fields)
    cols = ",".join(defaults.keys())
    placeholders = ",".join("?" for _ in defaults)
    with db_module.get_connection() as conn:
        cur = conn.execute(
            f"INSERT INTO copy_trades ({cols}) VALUES ({placeholders})",
            tuple(defaults.values())
        )
        return cur.lastrowid
