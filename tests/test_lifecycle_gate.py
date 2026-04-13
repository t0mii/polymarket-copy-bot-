"""Regression tests for AUTO_DISCOVERY_AUTO_PROMOTE gate on the
trader_lifecycle promotion paths.

Context: auto_discovery._add_followed_trader (the candidate promotion
site) already respects AUTO_DISCOVERY_AUTO_PROMOTE. But
trader_lifecycle._check_paper_to_live() also calls _add_followed_trader
when a trader meets paper criteria, bypassing the gate entirely.
A paused trader entering rehab can therefore be auto-re-added to
FOLLOWED_TRADERS without consent.

These tests lock in:
- _check_paper_to_live must not mutate FOLLOWED_TRADERS when the flag is
  off, even if paper criteria are met.
- _add_followed_trader must early-return when the flag is off, as
  defense-in-depth for any other call site.
- Both functions still work when the flag is explicitly on.
"""
import unittest
from unittest.mock import patch
from tests.conftest_helpers import setup_temp_db, teardown_temp_db


FAKE_SETTINGS = (
    "FOLLOWED_TRADERS=KING7777777:0xaaa,Jargs:0xbbb\n"
    "BET_SIZE_MAP=KING7777777:0.07,Jargs:0.02\n"
    "TRADER_EXPOSURE_MAP=KING7777777:0.40,Jargs:0.03\n"
    "MIN_ENTRY_PRICE_MAP=KING7777777:0.30,Jargs:0.42\n"
    "MAX_ENTRY_PRICE_MAP=KING7777777:0.85,Jargs:0.70\n"
    "MIN_TRADER_USD_MAP=KING7777777:3,Jargs:8\n"
    "TAKE_PROFIT_MAP=KING7777777:3.0,Jargs:1.5\n"
    "MAX_COPIES_PER_MARKET_MAP=KING7777777:3,Jargs:1\n"
    "HEDGE_WAIT_TRADERS=KING7777777:30,Jargs:90\n"
    "AVG_TRADER_SIZE_MAP=KING7777777:50,Jargs:20\n"
)


class TestLifecyclePromoteGate(unittest.TestCase):
    def setUp(self):
        self.db_path = setup_temp_db()
        from database import db
        self.db = db
        # Seed a PAPER_FOLLOW trader whose stats meet PATCH-038 criteria:
        # paper_trades >= 10, paper_pnl > 0, days_in_paper >= PAPER_MIN_DAYS (default 3)
        db.upsert_lifecycle_trader("0xsov", "sovereign2013", "PAPER_FOLLOW", "rehab")
        db.update_lifecycle_paper_stats("0xsov", 30, 10.0, 65.0)
        # Backdate status_changed_at to 5 days ago so PATCH-038's
        # _days_in_paper >= PAPER_MIN_DAYS (3) check passes
        from datetime import datetime, timedelta
        _old_ts = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE trader_lifecycle SET status_changed_at=? WHERE address=?",
                (_old_ts, "0xsov")
            )

    def tearDown(self):
        teardown_temp_db(self.db_path)

    def _reload_trader_lifecycle(self):
        """Force-reload trader_lifecycle so module-level state (like the
        function reference to _add_followed_trader) picks up patched values.
        """
        import importlib
        import bot.trader_lifecycle
        importlib.reload(bot.trader_lifecycle)
        return bot.trader_lifecycle

    def test_paper_to_live_blocks_when_auto_promote_false(self):
        """When AUTO_DISCOVERY_AUTO_PROMOTE=false, a trader meeting paper
        criteria must NOT be added to FOLLOWED_TRADERS, and the lifecycle
        status must NOT transition PAPER_FOLLOW -> LIVE_FOLLOW.
        """
        import config
        config.AUTO_DISCOVERY_AUTO_PROMOTE = False

        tl = self._reload_trader_lifecycle()

        writes = []

        def fake_write(content):
            writes.append(content)

        with patch.object(tl, "_read_settings", return_value=FAKE_SETTINGS), \
             patch.object(tl, "_write_settings", side_effect=fake_write):
            tl._check_paper_to_live()

        # settings.env must not have been touched
        self.assertEqual(writes, [], "FOLLOWED_TRADERS was mutated despite gate")

        # Lifecycle status must still be PAPER_FOLLOW, not LIVE_FOLLOW
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT status FROM trader_lifecycle WHERE address='0xsov'"
            ).fetchone()
        self.assertEqual(row["status"], "PAPER_FOLLOW",
                         "status transitioned despite gate")

    def test_paper_to_live_promotes_when_auto_promote_true(self):
        """When AUTO_DISCOVERY_AUTO_PROMOTE=true, the promotion path still
        works: settings.env is mutated and lifecycle status flips.
        """
        import config
        config.AUTO_DISCOVERY_AUTO_PROMOTE = True

        tl = self._reload_trader_lifecycle()

        writes = []

        def fake_write(content):
            writes.append(content)

        with patch.object(tl, "_read_settings", return_value=FAKE_SETTINGS), \
             patch.object(tl, "_write_settings", side_effect=fake_write):
            tl._check_paper_to_live()

        # settings.env must have been written at least once
        self.assertGreaterEqual(len(writes), 1,
                                "FOLLOWED_TRADERS was not mutated when flag on")
        self.assertIn("sovereign2013", writes[-1],
                      "sovereign2013 missing from written content")

        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT status FROM trader_lifecycle WHERE address='0xsov'"
            ).fetchone()
        self.assertEqual(row["status"], "LIVE_FOLLOW",
                         "status did not transition with flag on")

    def test_add_followed_trader_direct_call_blocked_when_false(self):
        """Defense-in-depth: calling _add_followed_trader directly with the
        flag off must early-return without mutating settings.env. This
        protects any future/hidden call site.
        """
        import config
        config.AUTO_DISCOVERY_AUTO_PROMOTE = False

        tl = self._reload_trader_lifecycle()

        writes = []

        def fake_write(content):
            writes.append(content)

        with patch.object(tl, "_read_settings", return_value=FAKE_SETTINGS), \
             patch.object(tl, "_write_settings", side_effect=fake_write):
            tl._add_followed_trader("0xsov", "sovereign2013")

        self.assertEqual(writes, [],
                         "_add_followed_trader wrote despite gate")

    def test_add_followed_trader_direct_call_allowed_when_true(self):
        """With the flag on, the direct call still works as before."""
        import config
        config.AUTO_DISCOVERY_AUTO_PROMOTE = True

        tl = self._reload_trader_lifecycle()

        writes = []

        def fake_write(content):
            writes.append(content)

        with patch.object(tl, "_read_settings", return_value=FAKE_SETTINGS), \
             patch.object(tl, "_write_settings", side_effect=fake_write):
            tl._add_followed_trader("0xsov", "sovereign2013")

        self.assertEqual(len(writes), 1,
                         "_add_followed_trader did not write with flag on")
        self.assertIn("sovereign2013", writes[0])


if __name__ == "__main__":
    unittest.main()
