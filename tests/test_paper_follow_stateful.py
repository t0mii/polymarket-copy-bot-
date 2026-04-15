"""TDD tests for stateful paper_follow watermark.

Before this fix, `bot/auto_discovery.py::paper_follow_candidates` used a
fixed `ENTRY_TRADE_SEC=300` (5 min) staleness filter copied from the live
copy path. The live copy path scans every 60s, so a 5min freshness window
makes sense. But paper_follow runs inside `discovery_scan` (every 3h
nominally), so 300s of freshness covers only 300/10800 = 2.78% of the
time between scans — ~97% of each trader's BUY trades were silently
dropped.

Fix: replace the fixed window with a per-candidate `last_paper_scan_ts`
watermark stored on `trader_candidates`. Each scan picks up BUYs strictly
newer than the watermark, then advances the watermark to the newest
captured timestamp. Robust against any scan cadence, produces no
duplicates, loses no trades.
"""
import time
import unittest
from unittest.mock import patch

from tests.conftest_helpers import setup_temp_db, teardown_temp_db


def _permissive_filters():
    return {
        "min_entry_price": 0.01,
        "max_entry_price": 0.99,
        "bet_size_pct": 0.01,
        "min_trade_size": 1.0,
        "max_position_size": 10.0,
        "detect_category": None,
    }


def _mk_trade(cid: str, ts: int, side: str = "YES", price: float = 0.55,
              trade_type: str = "BUY", market: str = "Q?"):
    return {
        "transaction_hash": "0x" + cid,
        "condition_id": cid,
        "side": side,
        "outcome_label": "",
        "price": price,
        "usdc_size": 100.0,
        "timestamp": ts,
        "market_question": market,
        "market_slug": "",
        "event_slug": "",
        "trade_type": trade_type,
        "end_date": "",
    }


class TestPaperFollowStateful(unittest.TestCase):
    def setUp(self):
        self.db_path = setup_temp_db()
        from database import db
        self.db = db
        # Make config loose so non-watermark filters pass
        import config
        self._saved = {
            "MIN_TRADER_USD": getattr(config, "MIN_TRADER_USD", 0),
            "MIN_CONVICTION_RATIO": getattr(config, "MIN_CONVICTION_RATIO", 0),
            "MAX_FEE_BPS": getattr(config, "MAX_FEE_BPS", 0),
            "GLOBAL_CATEGORY_BLACKLIST": getattr(config, "GLOBAL_CATEGORY_BLACKLIST", ""),
        }
        config.MIN_TRADER_USD = 0
        config.MIN_CONVICTION_RATIO = 0
        config.MAX_FEE_BPS = 0
        config.GLOBAL_CATEGORY_BLACKLIST = ""
        self.addr = "0xCANDIDATE1"
        with self.db.get_connection() as conn:
            conn.execute(
                "INSERT INTO trader_candidates (address, username, status) "
                "VALUES (?, ?, ?)",
                (self.addr, "cand1", "observing"),
            )

    def tearDown(self):
        import config
        for k, v in self._saved.items():
            setattr(config, k, v)
        teardown_temp_db(self.db_path)

    def _run_paper_follow(self, mock_trades):
        """Call paper_follow_candidates with filters + close stubbed."""
        from bot import auto_discovery
        with patch.object(auto_discovery, "fetch_wallet_recent_trades",
                          return_value=mock_trades), \
             patch.object(auto_discovery, "close_paper_trades",
                          new=lambda: None), \
             patch.object(auto_discovery, "_load_settings_filters",
                          return_value=_permissive_filters()), \
             patch.object(auto_discovery, "_paper_bet_size",
                          return_value=1.0):
            auto_discovery.paper_follow_candidates()

    def _paper_trade_count(self) -> int:
        with self.db.get_connection() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE candidate_address=?",
                (self.addr,)
            ).fetchone()[0]

    def test_first_scan_captures_all_new_trades_and_advances_watermark(self):
        """Fresh candidate (last_paper_scan_ts=0) — all 3 BUYs become paper_trades
        and the watermark advances to the newest timestamp."""
        t1, t2, t3 = 1_700_000_000, 1_700_000_100, 1_700_000_200
        trades = [_mk_trade("cid-1", t1), _mk_trade("cid-2", t2), _mk_trade("cid-3", t3)]
        self._run_paper_follow(trades)

        self.assertEqual(self._paper_trade_count(), 3,
                         "all 3 BUYs should be captured on first scan")
        self.assertEqual(self.db.get_candidate_paper_scan_ts(self.addr), t3,
                         "watermark should advance to newest timestamp")

    def test_second_scan_skips_already_seen_trades(self):
        """Watermark at t3 — API returns [t1, t2, t3, t4]. Only t4 is new."""
        t1, t2, t3, t4 = 1_700_000_000, 1_700_000_100, 1_700_000_200, 1_700_000_300
        self.db.set_candidate_paper_scan_ts(self.addr, t3)

        trades = [
            _mk_trade("cid-1", t1),
            _mk_trade("cid-2", t2),
            _mk_trade("cid-3", t3),  # equal to watermark → skip
            _mk_trade("cid-4", t4),  # newer → capture
        ]
        self._run_paper_follow(trades)

        self.assertEqual(self._paper_trade_count(), 1,
                         "only the trade newer than the watermark should be captured")
        self.assertEqual(self.db.get_candidate_paper_scan_ts(self.addr), t4,
                         "watermark should advance to t4")

    def test_no_duplicates_on_consecutive_scans_with_identical_response(self):
        """Back-to-back scans with the same API response — second scan is a no-op."""
        t1, t2 = 1_700_000_000, 1_700_000_100
        trades = [_mk_trade("cid-A", t1), _mk_trade("cid-B", t2)]

        self._run_paper_follow(trades)
        self.assertEqual(self._paper_trade_count(), 2)
        first_watermark = self.db.get_candidate_paper_scan_ts(self.addr)

        # Second scan — same trades, should not duplicate
        self._run_paper_follow(trades)
        self.assertEqual(self._paper_trade_count(), 2,
                         "second scan with identical trades must not duplicate")
        self.assertEqual(self.db.get_candidate_paper_scan_ts(self.addr), first_watermark,
                         "watermark stays the same when no new trades arrive")

    def test_sell_trades_filtered_but_advance_watermark(self):
        """Only BUYs create paper_trades, but the watermark advances on
        the newest-seen timestamp regardless of trade_type. This is the
        efficient behavior: a SELL-heavy window shouldn't cause the next
        scan to re-fetch the same SELL tail. The only data we can lose
        is >50 trades between scans — which is bounded by limit=50 and
        already assumed to be rare on the 3h scan interval."""
        t_sell, t_buy = 1_700_000_200, 1_700_000_100
        trades = [
            _mk_trade("cid-sell", t_sell, trade_type="SELL"),
            _mk_trade("cid-buy", t_buy, trade_type="BUY"),
        ]
        self._run_paper_follow(trades)

        self.assertEqual(self._paper_trade_count(), 1,
                         "only the BUY should become a paper_trade")
        self.assertEqual(self.db.get_candidate_paper_scan_ts(self.addr), t_sell,
                         "watermark should advance to the newest timestamp "
                         "regardless of trade_type, so next scan skips the "
                         "SELL tail")

    def test_empty_response_is_a_noop(self):
        """Empty wallet API response — paper_trades and watermark both untouched."""
        seed = 1_699_000_000
        self.db.set_candidate_paper_scan_ts(self.addr, seed)

        self._run_paper_follow([])

        self.assertEqual(self._paper_trade_count(), 0)
        self.assertEqual(self.db.get_candidate_paper_scan_ts(self.addr), seed,
                         "watermark should not be reset when response is empty")

    def test_set_candidate_paper_scan_ts_is_monotonic(self):
        """Concurrent-scan safety: a later scan that read a stale last_ts
        must never be able to roll the watermark backwards. Enforced by
        `SET last_paper_scan_ts = MAX(COALESCE(...), ?)`."""
        self.db.set_candidate_paper_scan_ts(self.addr, 2000)
        self.assertEqual(self.db.get_candidate_paper_scan_ts(self.addr), 2000)

        # Concurrent scan B finishes later but has an older last_ts in hand:
        self.db.set_candidate_paper_scan_ts(self.addr, 1500)

        self.assertEqual(self.db.get_candidate_paper_scan_ts(self.addr), 2000,
                         "watermark must not decrease — MAX guard required")


class TestPaperTradesUniqueIndex(unittest.TestCase):
    """The UNIQUE partial index on paper_trades(candidate_address,
    condition_id, side) WHERE status='open' prevents duplicate open rows
    for the same (trader, market, side) — which is exactly the collision
    surface that `add_paper_trade`'s INSERT OR IGNORE was silently failing
    to enforce (no constraint → OR IGNORE is a no-op).
    """
    def setUp(self):
        self.db_path = setup_temp_db()
        from database import db
        self.db = db
        with self.db.get_connection() as conn:
            conn.execute(
                "INSERT INTO trader_candidates (address, username, status) "
                "VALUES (?, ?, ?)",
                ("0xCAND", "cand", "observing"),
            )

    def tearDown(self):
        teardown_temp_db(self.db_path)

    def test_second_add_paper_trade_is_a_noop_on_open_row(self):
        """Two add_paper_trade calls with identical (cand, cid, side) when
        the first row is status='open' → UNIQUE constraint hits, INSERT OR
        IGNORE swallows, only 1 row in DB."""
        self.db.add_paper_trade("0xCAND", "CID-1", "Q?", "YES", 0.55)
        self.db.add_paper_trade("0xCAND", "CID-1", "Q?", "YES", 0.56)  # dup

        with self.db.get_connection() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE candidate_address='0xCAND'"
            ).fetchone()[0]
        self.assertEqual(n, 1, "UNIQUE partial index must block the dup insert")

    def test_reentry_after_close_is_allowed(self):
        """Reentry after a trade closes must still be possible, but under the
        Scenario-D Phase-A2 hour-bucket signature contract the reentry has to
        fall into a different clock hour from the original open (in prod this
        is guaranteed because close_paper_trades only fires after
        PAPER_EVAL_MAX_HOURS >= 24h — reentry never lands in the same hour)."""
        import datetime as _dtmod
        from unittest.mock import patch
        t1 = _dtmod.datetime(2026, 4, 15, 10, 30, 0)
        t2 = _dtmod.datetime(2026, 4, 16, 11, 30, 0)  # next day

        with patch("database.db._now", return_value=t1):
            self.db.add_paper_trade("0xCAND", "CID-1", "Q?", "YES", 0.55)
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE paper_trades SET status='closed' "
                "WHERE candidate_address='0xCAND' AND condition_id='CID-1'"
            )
        with patch("database.db._now", return_value=t2):
            self.db.add_paper_trade("0xCAND", "CID-1", "Q?", "YES", 0.60)

        with self.db.get_connection() as conn:
            n_total = conn.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE candidate_address='0xCAND'"
            ).fetchone()[0]
            n_open = conn.execute(
                "SELECT COUNT(*) FROM paper_trades "
                "WHERE candidate_address='0xCAND' AND status='open'"
            ).fetchone()[0]
        self.assertEqual(n_total, 2, "both rows should exist (one closed, one open)")
        self.assertEqual(n_open, 1, "exactly one open row after re-entry")

    def test_different_sides_allowed_on_same_market(self):
        """UNIQUE is on (cand, cid, side) — YES and NO on same market must
        both be allowed open simultaneously."""
        self.db.add_paper_trade("0xCAND", "CID-1", "Q?", "YES", 0.55)
        self.db.add_paper_trade("0xCAND", "CID-1", "Q?", "NO", 0.45)

        with self.db.get_connection() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE candidate_address='0xCAND'"
            ).fetchone()[0]
        self.assertEqual(n, 2)


class TestPaperTradesCleanupMigration(unittest.TestCase):
    """The init_db migration must DELETE duplicate open paper_trades
    (keeping the MIN(rowid) per group) before creating the UNIQUE index —
    otherwise the index creation would fail on existing contaminated DBs."""

    def test_init_db_collapses_existing_open_dupes(self):
        """Seed a DB with 5 duplicate open rows for the same (cand, cid,
        side), run init_db (which re-applies migrations idempotently),
        and assert only 1 row remains."""
        import os
        import sys
        import tempfile
        import importlib

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            import config
            config.DB_PATH = tmp.name
            if "database.db" in sys.modules:
                importlib.reload(sys.modules["database.db"])
            from database import db
            # First init_db — creates schema WITHOUT the new UNIQUE index
            # (the index migration is what we're testing). We simulate a
            # "legacy" DB by applying the base schema but bypassing the
            # UNIQUE index migration, then poisoning it.
            db.init_db()

            with db.get_connection() as conn:
                # Drop the UNIQUE index if it was created by the first init,
                # so we can seed dupes that wouldn't otherwise be allowed.
                try:
                    conn.execute("DROP INDEX IF EXISTS idx_paper_trades_open_dedup")
                except Exception:
                    pass
                conn.execute(
                    "INSERT INTO trader_candidates (address, username, status) "
                    "VALUES (?, ?, ?)",
                    ("0xDUP", "dup", "observing"),
                )
                for price in [0.55, 0.56, 0.57, 0.58, 0.59]:
                    conn.execute(
                        "INSERT INTO paper_trades "
                        "(candidate_address, condition_id, market_question, "
                        "side, entry_price, status) VALUES (?, ?, ?, ?, ?, 'open')",
                        ("0xDUP", "CID-X", "Q?", "YES", price),
                    )
            # Verify seeded state
            with db.get_connection() as conn:
                pre = conn.execute(
                    "SELECT COUNT(*) FROM paper_trades WHERE candidate_address='0xDUP'"
                ).fetchone()[0]
            self.assertEqual(pre, 5)

            # Re-run init_db — the cleanup migration should collapse to 1
            importlib.reload(sys.modules["database.db"])
            from database import db as db2
            db2.init_db()

            with db2.get_connection() as conn:
                post = conn.execute(
                    "SELECT COUNT(*) FROM paper_trades WHERE candidate_address='0xDUP'"
                ).fetchone()[0]
            self.assertEqual(post, 1, "cleanup migration must collapse open dupes")

            # Verify UNIQUE index now exists and blocks future dupes
            with db2.get_connection() as conn:
                idx = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' "
                    "AND name='idx_paper_trades_open_dedup'"
                ).fetchone()
            self.assertIsNotNone(idx,
                                 "UNIQUE partial index must be created by migration")
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    def test_init_db_adds_all_b0_columns(self):
        """Scenario-D Phase B0: additive schema migration for the 6 new
        columns Phase B1/B2 will populate. Must exist after init_db with
        correct types and default values (all nullable / zero / empty)."""
        from tests.conftest_helpers import setup_temp_db, teardown_temp_db
        path = setup_temp_db()
        try:
            from database import db
            with db.get_connection() as conn:
                info = {row[1]: row for row in conn.execute(
                    "PRAGMA table_info(paper_trades)").fetchall()}
            expected = {
                "category":       "TEXT",
                "filter_reason":  "TEXT",
                "ml_score":       "INTEGER",
                "close_reason":   "TEXT",
                "resolved_price": "REAL",
                "is_resolved":    "INTEGER",
            }
            for col, typ in expected.items():
                self.assertIn(col, info,
                              "Phase B0 column '%s' must exist on paper_trades" % col)
                # PRAGMA table_info: index 2 = type
                self.assertEqual(info[col][2].upper(), typ,
                                 "column %s has wrong type" % col)
        finally:
            teardown_temp_db(path)

    def test_close_paper_trades_respects_env_max_hours(self):
        """Scenario-D Phase B2: close_paper_trades must honor
        config.PAPER_EVAL_MAX_HOURS instead of the hardcoded 4h literal.
        Rows younger than the window stay open; rows past it get closed."""
        from tests.conftest_helpers import setup_temp_db, teardown_temp_db
        from unittest.mock import patch
        path = setup_temp_db()
        try:
            from database import db
            import config
            self._saved_hours = getattr(config, "PAPER_EVAL_MAX_HOURS", 24)
            config.PAPER_EVAL_MAX_HOURS = 2
            with db.get_connection() as conn:
                conn.execute(
                    "INSERT INTO trader_candidates (address, username, status) "
                    "VALUES ('0xMAXH', 'maxh', 'observing')"
                )
                # Row 1: 3h old → past 2h window, should close
                conn.execute(
                    "INSERT INTO paper_trades "
                    "(candidate_address, condition_id, market_question, side, "
                    " entry_price, status, created_at, signature) "
                    "VALUES ('0xMAXH', 'CID-OLD', 'Q?', 'YES', 0.55, 'open', "
                    " datetime('now','localtime','-3 hours'), 'sig_old')"
                )
                # Row 2: 1h old → inside window, should stay open
                conn.execute(
                    "INSERT INTO paper_trades "
                    "(candidate_address, condition_id, market_question, side, "
                    " entry_price, status, created_at, signature) "
                    "VALUES ('0xMAXH', 'CID-NEW', 'Q?', 'YES', 0.55, 'open', "
                    " datetime('now','localtime','-1 hours'), 'sig_new')"
                )

            from bot import auto_discovery, ws_price_tracker
            fake = type("FP", (), {
                "get_price": lambda self, cid, side: 0.60,
                "subscribe_condition": lambda self, cid: None,
            })()
            with patch.object(ws_price_tracker, "price_tracker", new=fake):
                auto_discovery.close_paper_trades()

            with db.get_connection() as conn:
                old_row = conn.execute(
                    "SELECT status, close_reason FROM paper_trades "
                    "WHERE condition_id='CID-OLD'"
                ).fetchone()
                new_row = conn.execute(
                    "SELECT status FROM paper_trades WHERE condition_id='CID-NEW'"
                ).fetchone()
            self.assertEqual(old_row["status"], "closed",
                             "row past PAPER_EVAL_MAX_HOURS must close")
            self.assertEqual(old_row["close_reason"], "time_cutoff")
            self.assertEqual(new_row["status"], "open",
                             "row inside window must stay open")
        finally:
            import config as _cfg
            _cfg.PAPER_EVAL_MAX_HOURS = self._saved_hours
            teardown_temp_db(path)

    def test_close_paper_trades_no_fake_pnl_when_price_is_none(self):
        """Scenario-D Phase B2: the `entry * 0.95` fake-loss fallback is
        REMOVED. When ws_price_tracker returns None, an old row must STAY
        OPEN (retry next cycle) — it must not be force-closed with a
        fabricated 5% loss.

        Row age is 5h, past the 2h PAPER_EVAL_MAX_HOURS window but NOT
        past the 3x abandonment threshold (6h), so the expected behavior
        is 'stay open, retry later', not 'force-close abandoned'."""
        from tests.conftest_helpers import setup_temp_db, teardown_temp_db
        from unittest.mock import patch
        path = setup_temp_db()
        try:
            from database import db
            import config
            self._saved_hours = getattr(config, "PAPER_EVAL_MAX_HOURS", 24)
            config.PAPER_EVAL_MAX_HOURS = 2
            with db.get_connection() as conn:
                conn.execute(
                    "INSERT INTO trader_candidates (address, username, status) "
                    "VALUES ('0xNOF', 'nof', 'observing')"
                )
                conn.execute(
                    "INSERT INTO paper_trades "
                    "(candidate_address, condition_id, market_question, side, "
                    " entry_price, status, created_at, signature) "
                    "VALUES ('0xNOF', 'CID-NP', 'Q?', 'YES', 0.55, 'open', "
                    " datetime('now','localtime','-5 hours'), 'sig_np')"
                )

            from bot import auto_discovery, ws_price_tracker
            fake = type("FP", (), {
                "get_price": lambda self, cid, side: None,
                "subscribe_condition": lambda self, cid: None,
            })()
            with patch.object(ws_price_tracker, "price_tracker", new=fake):
                auto_discovery.close_paper_trades()

            with db.get_connection() as conn:
                row = conn.execute(
                    "SELECT status, pnl, close_reason FROM paper_trades "
                    "WHERE condition_id='CID-NP'"
                ).fetchone()
            self.assertEqual(row["status"], "open",
                             "no price + no fake fallback => row stays open")
            self.assertAlmostEqual(row["pnl"] or 0, 0, places=4,
                                   msg="no fake pnl must be written")
            self.assertEqual(row["close_reason"], "",
                             "no close_reason since row stayed open")
        finally:
            import config as _cfg
            _cfg.PAPER_EVAL_MAX_HOURS = self._saved_hours
            teardown_temp_db(path)

    def test_close_paper_trades_force_closes_after_triple_budget(self):
        """Scenario-D Phase B2: to prevent unbounded open-row accumulation,
        any row still open after PAPER_EVAL_MAX_HOURS*3 without a price
        gets force-closed with pnl=0 and close_reason='abandoned'."""
        from tests.conftest_helpers import setup_temp_db, teardown_temp_db
        from unittest.mock import patch
        path = setup_temp_db()
        try:
            from database import db
            import config
            self._saved_hours = getattr(config, "PAPER_EVAL_MAX_HOURS", 24)
            config.PAPER_EVAL_MAX_HOURS = 2  # 3× = 6h abandonment threshold
            with db.get_connection() as conn:
                conn.execute(
                    "INSERT INTO trader_candidates (address, username, status) "
                    "VALUES ('0xABD', 'abd', 'observing')"
                )
                conn.execute(
                    "INSERT INTO paper_trades "
                    "(candidate_address, condition_id, market_question, side, "
                    " entry_price, status, created_at, signature) "
                    "VALUES ('0xABD', 'CID-AB', 'Q?', 'YES', 0.55, 'open', "
                    " datetime('now','localtime','-7 hours'), 'sig_ab')"
                )

            from bot import auto_discovery, ws_price_tracker
            fake = type("FP", (), {
                "get_price": lambda self, cid, side: None,
                "subscribe_condition": lambda self, cid: None,
            })()
            with patch.object(ws_price_tracker, "price_tracker", new=fake):
                auto_discovery.close_paper_trades()

            with db.get_connection() as conn:
                row = conn.execute(
                    "SELECT status, pnl, close_reason FROM paper_trades "
                    "WHERE condition_id='CID-AB'"
                ).fetchone()
            self.assertEqual(row["status"], "closed",
                             "row past 3× max_hours must be force-closed")
            self.assertAlmostEqual(row["pnl"] or 0, 0, places=4,
                                   msg="abandoned close has pnl=0, not fake loss")
            self.assertEqual(row["close_reason"], "abandoned")
        finally:
            import config as _cfg
            _cfg.PAPER_EVAL_MAX_HOURS = self._saved_hours
            teardown_temp_db(path)

    def test_close_paper_trades_skips_already_resolved_rows(self):
        """Scenario-D Phase B2: the defensive `is_resolved=0` guard in the
        SELECT ensures that even a race-condition row with `status='open'
        AND is_resolved=1` (e.g. track_paper_outcomes marked is_resolved
        but status update hasn't propagated for some reason) is NOT
        re-processed by close_paper_trades, preventing rollup
        double-counting."""
        from tests.conftest_helpers import setup_temp_db, teardown_temp_db
        from unittest.mock import patch
        path = setup_temp_db()
        try:
            from database import db
            import config
            self._saved_hours = getattr(config, "PAPER_EVAL_MAX_HOURS", 24)
            config.PAPER_EVAL_MAX_HOURS = 2
            with db.get_connection() as conn:
                conn.execute(
                    "INSERT INTO trader_candidates (address, username, status, "
                    " paper_trades, paper_wins, paper_pnl) "
                    "VALUES ('0xSKR', 'skr', 'observing', 1, 1, 0.50)"
                )
                # Edge-case row: status='open' (not yet flipped to 'closed')
                # but is_resolved=1 (tracker already marked resolution).
                # close_paper_trades must NOT treat this as a fresh close.
                conn.execute(
                    "INSERT INTO paper_trades "
                    "(candidate_address, condition_id, market_question, side, "
                    " entry_price, status, pnl, resolved_price, is_resolved, "
                    " close_reason, created_at, signature) "
                    "VALUES ('0xSKR', 'CID-SK', 'Q?', 'YES', 0.55, 'open', "
                    " 0.50, 0.95, 1, 'resolved_yes', "
                    " datetime('now','localtime','-5 hours'), 'sig_sk')"
                )

            from bot import auto_discovery, ws_price_tracker
            fake = type("FP", (), {
                "get_price": lambda self, cid, side: 0.60,
                "subscribe_condition": lambda self, cid: None,
            })()
            with patch.object(ws_price_tracker, "price_tracker", new=fake):
                auto_discovery.close_paper_trades()

            with db.get_connection() as conn:
                row = conn.execute(
                    "SELECT pnl, close_reason, is_resolved FROM paper_trades "
                    "WHERE condition_id='CID-SK'"
                ).fetchone()
                cand = conn.execute(
                    "SELECT paper_trades, paper_wins, paper_pnl "
                    "FROM trader_candidates WHERE address='0xSKR'"
                ).fetchone()
            self.assertAlmostEqual(row["pnl"], 0.50, places=4,
                                   msg="already-resolved row must not be touched")
            self.assertEqual(row["close_reason"], "resolved_yes")
            self.assertEqual(row["is_resolved"], 1)
            # Rollup must not have been double-counted
            self.assertEqual(cand["paper_trades"], 1)
            self.assertEqual(cand["paper_wins"], 1)
            self.assertAlmostEqual(cand["paper_pnl"], 0.50, places=4)
        finally:
            import config as _cfg
            _cfg.PAPER_EVAL_MAX_HOURS = self._saved_hours
            teardown_temp_db(path)

    def test_legacy_rows_survive_b0_migration(self):
        """Legacy rows inserted before B0 must survive init_db run and the
        new columns must come back as their declared defaults (empty/NULL/0),
        never as garbage or corrupted data."""
        from tests.conftest_helpers import setup_temp_db, teardown_temp_db
        path = setup_temp_db()
        try:
            from database import db
            with db.get_connection() as conn:
                conn.execute(
                    "INSERT INTO trader_candidates (address, username, status) "
                    "VALUES ('0xLEG', 'legacy', 'observing')"
                )
                # Legacy-shaped insert: only the pre-B0 columns.
                conn.execute(
                    "INSERT INTO paper_trades "
                    "(candidate_address, condition_id, market_question, side, "
                    " entry_price, current_price, status, pnl) "
                    "VALUES ('0xLEG', 'CID-L', 'Q?', 'YES', 0.55, 0.60, 'closed', 0.05)"
                )

            # Simulate a bot restart: re-run init_db (idempotent).
            db.init_db()

            with db.get_connection() as conn:
                row = conn.execute(
                    "SELECT category, filter_reason, ml_score, close_reason, "
                    "       resolved_price, is_resolved, entry_price, pnl "
                    "FROM paper_trades WHERE candidate_address='0xLEG'"
                ).fetchone()
            self.assertIsNotNone(row, "legacy row must survive migration")
            # Original data untouched
            self.assertAlmostEqual(row["entry_price"], 0.55, places=4)
            self.assertAlmostEqual(row["pnl"], 0.05, places=4)
            # New columns at defaults
            self.assertEqual(row["category"], "")
            self.assertEqual(row["filter_reason"], "")
            self.assertIsNone(row["ml_score"])
            self.assertEqual(row["close_reason"], "")
            self.assertIsNone(row["resolved_price"])
            self.assertEqual(row["is_resolved"], 0)
        finally:
            teardown_temp_db(path)


if __name__ == "__main__":
    unittest.main()
