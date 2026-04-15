"""TDD for Scenario D Phase B2: `track_paper_outcomes`.

New function in `bot/outcome_tracker.py` that processes open paper_trades
on a 30min schedule:
- Reads open rows where is_resolved=0.
- Calls `get_market_price(cid, side=row.side)` with the new side-aware
  helper (so team-name markets get THEIR side's price, not outcomes[0]).
- Resolved markets: close the row with real resolved_price, compute pnl
  from shares × (resolved - entry), update candidate rollups, set
  close_reason and is_resolved=1.
- Unresolved but active markets: just update current_price for the
  dashboard view. Row stays open.
- Null price from Gamma: leave row untouched (no fake-loss fallback).
- If config.PAPER_RESOLUTION_TRACKING_ENABLED is False: no-op.

This is the piece that replaces the 4h `entry * 0.95` fake-loss fallback
with actual market outcomes.
"""
import unittest
from unittest.mock import patch

from tests.conftest_helpers import setup_temp_db, teardown_temp_db


CAND = "0xcand_paper_outcome"
CID_RESOLVED_WIN = "0xcid_resolved_win"
CID_RESOLVED_LOSS = "0xcid_resolved_loss"
CID_ACTIVE = "0xcid_active"
CID_UNKNOWN = "0xcid_unknown"


class TestTrackPaperOutcomes(unittest.TestCase):
    def setUp(self):
        self.db_path = setup_temp_db()
        from database import db
        self.db = db
        import config
        self._saved_flag = getattr(config, "PAPER_RESOLUTION_TRACKING_ENABLED", True)
        config.PAPER_RESOLUTION_TRACKING_ENABLED = True
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO trader_candidates (address, username, status, paper_trades, paper_wins, paper_pnl) "
                "VALUES (?, 'tester', 'observing', 0, 0, 0)",
                (CAND,),
            )

    def tearDown(self):
        import config
        config.PAPER_RESOLUTION_TRACKING_ENABLED = self._saved_flag
        teardown_temp_db(self.db_path)

    def _seed_open(self, cid: str, side: str, entry: float):
        self.db.add_paper_trade(CAND, cid, "Test market", side, entry)

    def _row(self, cid: str) -> dict:
        with self.db.get_connection() as conn:
            r = conn.execute(
                "SELECT status, current_price, resolved_price, is_resolved, "
                "close_reason, pnl FROM paper_trades "
                "WHERE candidate_address=? AND condition_id=?",
                (CAND, cid),
            ).fetchone()
        return dict(r) if r else None

    def _cand(self) -> dict:
        with self.db.get_connection() as conn:
            r = conn.execute(
                "SELECT paper_trades, paper_wins, paper_pnl "
                "FROM trader_candidates WHERE address=?", (CAND,)
            ).fetchone()
        return dict(r) if r else None

    def _fake_price_fn(self, mapping: dict):
        """Return a side-effect function that maps condition_id to (price, resolved)."""
        def _fn(cid, asset="", side=""):
            return mapping.get(cid, (None, False))
        return _fn

    def test_resolved_win_closes_paper_trade(self):
        self._seed_open(CID_RESOLVED_WIN, "YES", 0.58)

        from bot import outcome_tracker
        with patch.object(outcome_tracker, "get_market_price",
                          side_effect=self._fake_price_fn({
                              CID_RESOLVED_WIN: (0.97, True),
                          })), \
             patch.object(outcome_tracker.time, "sleep", new=lambda *_: None):
            updated = outcome_tracker.track_paper_outcomes()

        self.assertGreaterEqual(updated, 1)
        row = self._row(CID_RESOLVED_WIN)
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["is_resolved"], 1)
        self.assertAlmostEqual(row["resolved_price"], 0.97, places=4)
        self.assertAlmostEqual(row["current_price"], 0.97, places=4)
        self.assertEqual(row["close_reason"], "resolved_yes")
        self.assertGreater(row["pnl"], 0, "resolved win must have positive pnl")

    def test_resolved_loss_closes_paper_trade_with_negative_pnl(self):
        self._seed_open(CID_RESOLVED_LOSS, "YES", 0.58)

        from bot import outcome_tracker
        with patch.object(outcome_tracker, "get_market_price",
                          side_effect=self._fake_price_fn({
                              CID_RESOLVED_LOSS: (0.02, True),
                          })), \
             patch.object(outcome_tracker.time, "sleep", new=lambda *_: None):
            outcome_tracker.track_paper_outcomes()

        row = self._row(CID_RESOLVED_LOSS)
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["is_resolved"], 1)
        self.assertEqual(row["close_reason"], "resolved_no")
        self.assertLess(row["pnl"], 0, "resolved loss must have negative pnl")

    def test_unresolved_active_market_only_updates_current_price(self):
        self._seed_open(CID_ACTIVE, "YES", 0.55)

        from bot import outcome_tracker
        with patch.object(outcome_tracker, "get_market_price",
                          side_effect=self._fake_price_fn({
                              CID_ACTIVE: (0.60, False),
                          })), \
             patch.object(outcome_tracker.time, "sleep", new=lambda *_: None):
            outcome_tracker.track_paper_outcomes()

        row = self._row(CID_ACTIVE)
        self.assertEqual(row["status"], "open", "active market must stay open")
        self.assertEqual(row["is_resolved"], 0)
        self.assertAlmostEqual(row["current_price"], 0.60, places=4)
        self.assertIsNone(row["resolved_price"])
        self.assertEqual(row["close_reason"], "")
        self.assertAlmostEqual(row["pnl"] or 0, 0, places=4)

    def test_null_price_leaves_row_completely_untouched(self):
        self._seed_open(CID_UNKNOWN, "YES", 0.55)

        from bot import outcome_tracker
        with patch.object(outcome_tracker, "get_market_price",
                          side_effect=self._fake_price_fn({
                              CID_UNKNOWN: (None, False),
                          })), \
             patch.object(outcome_tracker.time, "sleep", new=lambda *_: None):
            outcome_tracker.track_paper_outcomes()

        row = self._row(CID_UNKNOWN)
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["is_resolved"], 0)
        self.assertIsNone(row["current_price"])
        self.assertIsNone(row["resolved_price"])
        self.assertEqual(row["close_reason"], "")

    def test_tracking_disabled_is_noop(self):
        self._seed_open(CID_RESOLVED_WIN, "YES", 0.58)

        import config
        config.PAPER_RESOLUTION_TRACKING_ENABLED = False

        from bot import outcome_tracker
        with patch.object(outcome_tracker, "get_market_price",
                          side_effect=Exception("must not be called")), \
             patch.object(outcome_tracker.time, "sleep", new=lambda *_: None):
            updated = outcome_tracker.track_paper_outcomes()

        self.assertEqual(updated, 0)
        row = self._row(CID_RESOLVED_WIN)
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["is_resolved"], 0)

    def test_rollups_update_after_resolution(self):
        self._seed_open(CID_RESOLVED_WIN, "YES", 0.58)

        from bot import outcome_tracker
        with patch.object(outcome_tracker, "get_market_price",
                          side_effect=self._fake_price_fn({
                              CID_RESOLVED_WIN: (0.97, True),
                          })), \
             patch.object(outcome_tracker.time, "sleep", new=lambda *_: None):
            outcome_tracker.track_paper_outcomes()

        cand = self._cand()
        self.assertEqual(cand["paper_trades"], 1)
        self.assertEqual(cand["paper_wins"], 1,
                         "a resolved win must increment paper_wins")
        self.assertGreater(cand["paper_pnl"], 0)

    def test_skips_rows_already_is_resolved(self):
        """An already-resolved row must not be double-counted on a second
        tracker cycle (idempotency)."""
        self._seed_open(CID_RESOLVED_WIN, "YES", 0.58)

        from bot import outcome_tracker
        fake = self._fake_price_fn({CID_RESOLVED_WIN: (0.97, True)})
        with patch.object(outcome_tracker, "get_market_price",
                          side_effect=fake), \
             patch.object(outcome_tracker.time, "sleep", new=lambda *_: None):
            outcome_tracker.track_paper_outcomes()
            with patch.object(outcome_tracker, "get_market_price",
                              side_effect=Exception(
                                  "second cycle must not re-check resolved row")):
                updated = outcome_tracker.track_paper_outcomes()
                self.assertEqual(updated, 0)

        cand = self._cand()
        self.assertEqual(cand["paper_trades"], 1,
                         "rollup must not be double-counted")
        self.assertEqual(cand["paper_wins"], 1)


if __name__ == "__main__":
    unittest.main()
