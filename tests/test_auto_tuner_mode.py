"""TDD for AUTO_TUNER_MODE staged rollout.

disabled: auto_tune() returns immediately, no side effects
readonly: computes recommendations, logs to brain_decisions, does NOT write settings.env
active:   computes + writes (original behavior)
"""
import unittest
from unittest.mock import patch
from tests.conftest_helpers import setup_temp_db, teardown_temp_db, insert_copy_trade


class TestAutoTunerMode(unittest.TestCase):
    def setUp(self):
        self.path = setup_temp_db()
        from database import db
        self.db = db
        with db.get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO wallets (address, username, followed) "
                "VALUES ('0xaaa', 'alice', 1)"
            )
        for i in range(10):
            insert_copy_trade(
                db, wallet_address="0xaaa", wallet_username="alice",
                condition_id="cid_%d" % i, status="closed",
                pnl_realized=0.50 if i < 7 else -1.00,
                actual_size=2.0,
            )

    def tearDown(self):
        teardown_temp_db(self.path)

    def test_disabled_mode_does_nothing(self):
        """disabled: no brain_decisions written, no settings touched."""
        import config
        orig = getattr(config, "AUTO_TUNER_MODE", "disabled")
        config.AUTO_TUNER_MODE = "disabled"
        try:
            from bot.auto_tuner import auto_tune
            with patch("bot.auto_tuner._read_settings", return_value="BET_SIZE_MAP=\n"), \
                 patch("bot.settings_lock.write_settings") as mock_write:
                auto_tune()
            mock_write.assert_not_called()
            with self.db.get_connection() as conn:
                n = conn.execute(
                    "SELECT COUNT(*) FROM brain_decisions WHERE action='TUNER_RECOMMENDATION'"
                ).fetchone()[0]
            self.assertEqual(n, 0)
        finally:
            config.AUTO_TUNER_MODE = orig

    def test_readonly_mode_logs_but_does_not_write(self):
        """readonly: brain_decisions rows appear, settings.env untouched."""
        import config
        orig = getattr(config, "AUTO_TUNER_MODE", "disabled")
        config.AUTO_TUNER_MODE = "readonly"
        try:
            from bot.auto_tuner import auto_tune
            with patch("bot.auto_tuner._read_settings", return_value="BET_SIZE_MAP=\nTRADER_EXPOSURE_MAP=\n"), \
                 patch("bot.settings_lock.write_settings") as mock_write:
                auto_tune()
            mock_write.assert_not_called()
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM brain_decisions WHERE action='TUNER_RECOMMENDATION'"
                ).fetchall()
            self.assertGreater(len(rows), 0,
                               "readonly mode must log at least one recommendation")
        finally:
            config.AUTO_TUNER_MODE = orig


if __name__ == "__main__":
    unittest.main()
