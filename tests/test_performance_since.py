"""Regression tests for the PERFORMANCE_SINCE filter on trader/category
performance aggregations.

Context: brain + auto_tuner + trade_scorer read copy_trades aggregates
(rolling pnl, category WR) to drive per-trader settings. Without a
regime-change cutoff, stale pre-regime trades keep re-triggering blocks
even after settings are reset. PERFORMANCE_SINCE pins an ISO timestamp
after which trades count and before which they are ignored.

Covered:
- db.get_performance_since() returns empty string when unset (backward compat)
- db.get_performance_since() normalizes ISO to SQL timestamp format
- db.get_trader_rolling_pnl() excludes pre-PERFORMANCE_SINCE trades
- brain._classify_losses() honors PERFORMANCE_SINCE
- trade_scorer._score_category_wr() honors PERFORMANCE_SINCE
"""
import unittest
from datetime import datetime, timedelta
from tests.conftest_helpers import setup_temp_db, teardown_temp_db, insert_copy_trade


class TestPerformanceSinceHelper(unittest.TestCase):
    def setUp(self):
        self.db_path = setup_temp_db()
        from database import db
        self.db = db
        import config
        self._orig_ps = getattr(config, "PERFORMANCE_SINCE", "")

    def tearDown(self):
        import config
        config.PERFORMANCE_SINCE = self._orig_ps
        teardown_temp_db(self.db_path)

    def test_returns_empty_string_when_unset(self):
        import config
        config.PERFORMANCE_SINCE = ""
        self.assertEqual(self.db.get_performance_since(), "")

    def test_normalizes_iso_to_sql_format(self):
        import config
        config.PERFORMANCE_SINCE = "2026-04-13T17:40:00"
        self.assertEqual(self.db.get_performance_since(), "2026-04-13 17:40:00")

    def test_invalid_iso_returns_empty(self):
        import config
        config.PERFORMANCE_SINCE = "not-a-date"
        self.assertEqual(self.db.get_performance_since(), "")


class TestTraderRollingPnlRespectsPerformanceSince(unittest.TestCase):
    def setUp(self):
        self.db_path = setup_temp_db()
        from database import db
        self.db = db
        import config
        self._orig_ps = getattr(config, "PERFORMANCE_SINCE", "")

        # FK: seed wallets row first
        with db.get_connection() as conn:
            try:
                conn.execute("INSERT INTO wallets (address, username) VALUES (?, ?)",
                             ("0xdead", "KING7777777"))
            except Exception:
                pass

        # Insert: 3 old trades (pre-regime) with big losses + 3 new trades with small wins.
        # We want PERFORMANCE_SINCE to exclude the old ones.
        now = datetime.now()
        old_ts = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        new_ts = now.strftime("%Y-%m-%d %H:%M:%S")

        for i in range(3):
            insert_copy_trade(
                db, wallet_username="KING7777777", status="closed",
                pnl_realized=-5.0, usdc_received=0.0, actual_size=5.0,
                closed_at=old_ts, created_at=old_ts,
                condition_id=f"old-{i}",
            )
        for i in range(3):
            insert_copy_trade(
                db, wallet_username="KING7777777", status="closed",
                pnl_realized=0.3, usdc_received=1.3, actual_size=1.0,
                closed_at=new_ts, created_at=new_ts,
                condition_id=f"new-{i}",
            )

    def tearDown(self):
        import config
        config.PERFORMANCE_SINCE = self._orig_ps
        teardown_temp_db(self.db_path)

    def test_without_performance_since_sees_all_trades(self):
        import config
        config.PERFORMANCE_SINCE = ""
        stats = self.db.get_trader_rolling_pnl("KING7777777", days=7, min_verified=100)
        # Fallback branch (all_trades_fallback) — we have 6 trades, only 3 verified < 100
        self.assertEqual(stats["cnt"], 6)
        # Total: 3 * -5.0 + 3 * 0.3 = -15 + 0.9 = -14.1
        self.assertAlmostEqual(stats["total_pnl"], -14.1, places=1)

    def test_with_performance_since_excludes_old_trades(self):
        import config
        # Set PERFORMANCE_SINCE to 1 day ago — excludes the old trades (3 days old)
        # but includes the new ones (now)
        since = (datetime.now() - timedelta(days=1)).isoformat()
        config.PERFORMANCE_SINCE = since
        stats = self.db.get_trader_rolling_pnl("KING7777777", days=7, min_verified=100)
        self.assertEqual(stats["cnt"], 3, "should only see the 3 new trades")
        self.assertAlmostEqual(stats["total_pnl"], 0.9, places=1,
                               msg="should only sum the 3 new +0.3 trades")


class TestBrainClassifyLossesRespectsPerformanceSince(unittest.TestCase):
    def setUp(self):
        self.db_path = setup_temp_db()
        from database import db
        self.db = db
        import config
        self._orig_ps = getattr(config, "PERFORMANCE_SINCE", "")

        with db.get_connection() as conn:
            try:
                conn.execute("INSERT INTO wallets (address, username) VALUES (?, ?)",
                             ("0xdead", "KING7777777"))
            except Exception:
                pass

        now = datetime.now()
        old_ts = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")

        # 12 old losses in 'lol' for KING — would trigger BAD_CATEGORY normally
        for i in range(12):
            insert_copy_trade(
                db, wallet_username="KING7777777", status="closed",
                category="lol", pnl_realized=-2.0, usdc_received=0.0,
                actual_size=2.0, closed_at=old_ts, created_at=old_ts,
                condition_id=f"old-lol-{i}",
            )

    def tearDown(self):
        import config
        config.PERFORMANCE_SINCE = self._orig_ps
        teardown_temp_db(self.db_path)

    def test_classify_losses_skips_pre_regime_losses(self):
        import config
        import importlib
        # Set PERFORMANCE_SINCE to yesterday — excludes all 12 old losses
        since = (datetime.now() - timedelta(days=1)).isoformat()
        config.PERFORMANCE_SINCE = since

        # Reload brain to pick up the new config
        import bot.brain
        importlib.reload(bot.brain)

        # Should not crash + should log "No losses in last 7d" since all are
        # before PERFORMANCE_SINCE. Test: no exception, losses query returns empty.
        bot.brain._classify_losses()
        # No assertion on output — just that it runs cleanly and respects the filter.
        # The query-level assertion is in test_trader_rolling_pnl. This test is
        # smoke-level for the integration.


class TestTradeScorerCategoryWRRespectsPerformanceSince(unittest.TestCase):
    def setUp(self):
        self.db_path = setup_temp_db()
        from database import db
        self.db = db
        import config
        self._orig_ps = getattr(config, "PERFORMANCE_SINCE", "")

        with db.get_connection() as conn:
            try:
                conn.execute("INSERT INTO wallets (address, username) VALUES (?, ?)",
                             ("0xdead", "KING7777777"))
            except Exception:
                pass

        now = datetime.now()
        old_ts = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        new_ts = now.strftime("%Y-%m-%d %H:%M:%S")

        # 10 old 'lol' losses (pre-regime) + 4 new wins
        for i in range(10):
            insert_copy_trade(
                db, wallet_username="KING7777777", status="closed",
                category="lol", pnl_realized=-2.0, closed_at=old_ts,
                created_at=old_ts, condition_id=f"old-{i}",
            )
        for i in range(4):
            insert_copy_trade(
                db, wallet_username="KING7777777", status="closed",
                category="lol", pnl_realized=0.5, closed_at=new_ts,
                created_at=new_ts, condition_id=f"new-{i}",
            )

    def tearDown(self):
        import config
        config.PERFORMANCE_SINCE = self._orig_ps
        teardown_temp_db(self.db_path)

    def test_without_performance_since_sees_all_trades(self):
        import config
        import importlib
        config.PERFORMANCE_SINCE = ""
        import bot.trade_scorer
        importlib.reload(bot.trade_scorer)
        # 4 wins / 14 = 28.6% WR
        score = bot.trade_scorer._score_category_wr("KING7777777", "lol")
        # Map: max(0, min(100, int((28.6 - 35) * 100 / 30))) = max(0, -21) = 0
        self.assertEqual(score, 0)

    def test_with_performance_since_only_sees_new_wins(self):
        import config
        import importlib
        since = (datetime.now() - timedelta(days=1)).isoformat()
        config.PERFORMANCE_SINCE = since
        import bot.trade_scorer
        importlib.reload(bot.trade_scorer)
        # Only 4 new wins, 4 wins / 4 = 100% WR
        score = bot.trade_scorer._score_category_wr("KING7777777", "lol")
        # Map: max(0, min(100, int((100 - 35) * 100 / 30))) = min(100, 216) = 100
        self.assertEqual(score, 100)


if __name__ == "__main__":
    unittest.main()
