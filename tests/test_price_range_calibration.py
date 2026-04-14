"""TDD tests for bot.price_range_calibrator.compute_verified_price_range.

The auto-tuner historically set MIN/MAX_ENTRY_PRICE_MAP from a tier-
based win-rate heuristic which clipped the profitable tails of traders
whose edge is at underdog (low price) or favorite (high price) extremes
— e.g. xsaghav had +$41.65 in the 30-40c bucket that the 45c MIN blocked
and +$9.76 in the 80-90c bucket that the 65c MAX blocked. This function
replaces that heuristic with a magnitude-aware per-bucket PnL compute
that selects the widest range covering profitable buckets.
"""
import unittest
from tests.conftest_helpers import setup_temp_db, teardown_temp_db, insert_copy_trade


class TestVerifiedPriceRange(unittest.TestCase):
    def setUp(self):
        self.db_path = setup_temp_db()
        from database import db
        self.db = db
        with db.get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO wallets (address, username) VALUES (?, ?)",
                ("0xdead", "trader1"),
            )

    def tearDown(self):
        teardown_temp_db(self.db_path)

    def _make_trades(self, price, count, pnl_per_trade, size=5.0, tag=""):
        """Insert `count` verified closed trades at `price` with `pnl_per_trade` each."""
        for i in range(count):
            insert_copy_trade(
                self.db,
                wallet_username="trader1",
                actual_entry_price=price,
                entry_price=price,
                size=size,
                actual_size=size,
                usdc_received=size + pnl_per_trade,
                pnl_realized=pnl_per_trade,
                status="closed",
                condition_id="cid-%s-%.2f-%d" % (tag, price, i),
                market_question="Q at %.2f #%d" % (price, i),
            )

    def test_xsaghav_like_bimodal_absorbs_middle_gap(self):
        """Trader profitable at 30-40c and 80-90c with a mediocre 40-50c
        middle should return (0.30, 0.90) — absorbing the gap since we
        can't express disjoint ranges."""
        self._make_trades(price=0.35, count=6, pnl_per_trade=6.67, tag="a")
        self._make_trades(price=0.45, count=13, pnl_per_trade=-0.08, tag="b")
        self._make_trades(price=0.55, count=14, pnl_per_trade=1.79, tag="c")
        self._make_trades(price=0.85, count=6, pnl_per_trade=1.67, tag="d")

        from bot.price_range_calibrator import compute_verified_price_range
        result = compute_verified_price_range(self.db, "trader1")
        self.assertEqual(result, (0.30, 0.90))

    def test_insufficient_total_trades_returns_none(self):
        """Even with multiple good buckets spanning a range, if total
        verified trades are below min_total_trades the sample is too
        small to trust and the function returns None to fall back
        to the tier default."""
        # 3 good buckets × 3 trades each = 9 total, below default 20
        self._make_trades(price=0.35, count=3, pnl_per_trade=5.0, tag="a")
        self._make_trades(price=0.55, count=3, pnl_per_trade=3.0, tag="b")
        self._make_trades(price=0.75, count=3, pnl_per_trade=2.0, tag="c")

        from bot.price_range_calibrator import compute_verified_price_range
        result = compute_verified_price_range(self.db, "trader1", min_total_trades=20)
        self.assertIsNone(result)

    def test_all_losing_buckets_returns_none(self):
        """A trader where every bucket is below the pnl threshold should
        return None — no range can be recommended, fall back to tier."""
        self._make_trades(price=0.25, count=10, pnl_per_trade=-3.0, tag="l1")
        self._make_trades(price=0.55, count=10, pnl_per_trade=-4.0, tag="l2")
        self._make_trades(price=0.85, count=10, pnl_per_trade=-2.5, tag="l3")

        from bot.price_range_calibrator import compute_verified_price_range
        result = compute_verified_price_range(self.db, "trader1")
        self.assertIsNone(result)

    def test_unverified_trades_excluded_from_count(self):
        """Unverified trades (older rows without usdc_received) must not
        count toward min_total_trades. 15 verified trades + 30 unverified
        = 45 total rows on disk, but verified count is only 15, which is
        below min_total_trades=20 → None.

        If the SQL didn't filter by usdc_received IS NOT NULL, the code
        would see 45 rows, proceed past the count guard, and compute a
        range from bucket 3 + bucket 7 → (0.30, 0.80). That's the exact
        poisoning we need to prevent."""
        # 15 verified profitable trades across 3 buckets
        self._make_trades(price=0.35, count=5, pnl_per_trade=2.0, tag="v1")
        self._make_trades(price=0.55, count=5, pnl_per_trade=2.0, tag="v2")
        self._make_trades(price=0.75, count=5, pnl_per_trade=2.0, tag="v3")
        # 30 unverified rows — older era, no usdc_received
        for i in range(30):
            insert_copy_trade(
                self.db,
                wallet_username="trader1",
                actual_entry_price=0.55,
                entry_price=0.55,
                size=5.0,
                actual_size=5.0,
                usdc_received=None,
                pnl_realized=2.0,
                status="closed",
                condition_id="unver-%d" % i,
                market_question="Unverified Q %d" % i,
            )

        from bot.price_range_calibrator import compute_verified_price_range
        result = compute_verified_price_range(self.db, "trader1", min_total_trades=20)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
