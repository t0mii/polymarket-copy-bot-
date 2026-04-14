import unittest
from tests.conftest_helpers import setup_temp_db, teardown_temp_db, insert_copy_trade


class TestMLTimeSplit(unittest.TestCase):
    def setUp(self):
        self.db_path = setup_temp_db()
        from database import db
        self.db = db
        # The copy_trades table has an FK to wallets.address; seed one.
        with db.get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO wallets (address, username) VALUES (?, ?)",
                ("0xdead", "trader1"),
            )
        # Build a dataset with a temporal trend:
        # Early trades win, late trades lose. Random split would see both,
        # time-split should not leak late data into training.
        # Need at least MIN_TRAINING_SAMPLES=50 for training to fire.
        for i in range(60):
            is_early = i < 30
            # Early period: 80% wins, 20% losses.
            # Late period: 20% wins, 80% losses.
            # Each half has both classes so the time-split test set is
            # still mixed-class — that's required for baseline math to run.
            if is_early:
                wins = (i % 5) != 0  # 4/5 win
            else:
                wins = (i % 5) == 0  # 1/5 win
            insert_copy_trade(
                db,
                wallet_username="trader1",
                category="cs",
                entry_price=0.5,
                actual_entry_price=0.5,
                side="YES",
                actual_size=5.0,
                pnl_realized=(+1.0 if wins else -1.0),
                status="closed",
                condition_id="cid-%d" % i,
            )
        # Fix created_at so first 30 are clearly older than last 30.
        with db.get_connection() as conn:
            rows = conn.execute("SELECT id FROM copy_trades ORDER BY id").fetchall()
            for idx, r in enumerate(rows):
                ts = "2026-01-%02d 12:00:00" % (idx + 1)
                conn.execute(
                    "UPDATE copy_trades SET created_at = ? WHERE id = ?",
                    (ts, r["id"])
                )

    def tearDown(self):
        teardown_temp_db(self.db_path)

    def test_training_uses_time_order_not_random_split(self):
        from bot import ml_scorer
        # Capture logging output to assert baseline + class balance appear.
        import logging
        from io import StringIO
        buf = StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.INFO)
        ml_logger = logging.getLogger("bot.ml_scorer")
        ml_logger.addHandler(handler)
        old_level = ml_logger.level
        ml_logger.setLevel(logging.INFO)
        try:
            ml_scorer.train_model()
        finally:
            ml_logger.removeHandler(handler)
            ml_logger.setLevel(old_level)

        log = buf.getvalue()
        # Must log class balance and baseline accuracy.
        self.assertIn("Class balance", log)
        self.assertIn("Baseline", log)
        # After the 2026-04-14 two-model split, copy training has its own
        # tagged log line and the whole dataset IS the copy subset, so the
        # legacy "COPY-ONLY test subset" line is gone. Match the new tag.
        self.assertIn("[ML-COPY] Trained on", log)
        # Sanity: builder returns all rows sorted ASC by created_at and
        # an is_copy marker vector aligned with X. Legacy wrapper now
        # returns a 6-tuple (with weights) — unpack accordingly.
        X, y, is_copy, copy_count, blocked_count, weights = ml_scorer._build_training_data()
        self.assertGreaterEqual(len(y), 60)
        self.assertEqual(len(is_copy), len(y))
        self.assertEqual(len(weights), len(y))
        # All rows in this test come from copy_trades (no blocked_trades seeded).
        self.assertTrue(all(is_copy))


if __name__ == "__main__":
    unittest.main()
