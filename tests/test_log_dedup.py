"""Regression tests for the log_blocked_trade and log_brain_decision
dedup guards added on 2026-04-13.

Both helpers were exhibiting cross-cycle / cross-scan spam:
- log_blocked_trade: same (trader, cid, reason) wrote a row every scan
  cycle (~6 rows per market per 10s scan)
- log_brain_decision: same (action, target) wrote a row every brain
  cycle (~5 duplicate rows per 2h cycle)

Both guards must drop subsequent calls within their dedup window
without raising and without writing a new row.
"""
import unittest
from tests.conftest_helpers import setup_temp_db, teardown_temp_db


class TestBlockedTradeDedup(unittest.TestCase):
    def setUp(self):
        self.db_path = setup_temp_db()
        from database import db
        self.db = db
        db._blocked_dedup_cache.clear()

    def tearDown(self):
        teardown_temp_db(self.db_path)

    def test_first_call_writes_row(self):
        self.db.log_blocked_trade(
            trader="alice", market_question="Will X?",
            condition_id="cid-1", side="YES", trader_price=0.5,
            block_reason="price_range", block_detail="x"
        )
        with self.db.get_connection() as conn:
            n = conn.execute("SELECT COUNT(*) FROM blocked_trades").fetchone()[0]
        self.assertEqual(n, 1)

    def test_duplicate_within_ttl_is_skipped(self):
        for _ in range(10):
            self.db.log_blocked_trade(
                trader="alice", market_question="Will X?",
                condition_id="cid-1", side="YES", trader_price=0.5,
                block_reason="price_range", block_detail="x"
            )
        with self.db.get_connection() as conn:
            n = conn.execute("SELECT COUNT(*) FROM blocked_trades").fetchone()[0]
        self.assertEqual(n, 1)

    def test_different_reason_writes_separate_row(self):
        self.db.log_blocked_trade(
            trader="alice", market_question="Will X?",
            condition_id="cid-1", side="YES", trader_price=0.5,
            block_reason="price_range", block_detail="x"
        )
        self.db.log_blocked_trade(
            trader="alice", market_question="Will X?",
            condition_id="cid-1", side="YES", trader_price=0.5,
            block_reason="exposure_limit", block_detail="y"
        )
        with self.db.get_connection() as conn:
            n = conn.execute("SELECT COUNT(*) FROM blocked_trades").fetchone()[0]
        self.assertEqual(n, 2)

    def test_different_trader_writes_separate_row(self):
        for trader in ("alice", "bob"):
            self.db.log_blocked_trade(
                trader=trader, market_question="Will X?",
                condition_id="cid-1", side="YES", trader_price=0.5,
                block_reason="price_range", block_detail="x"
            )
        with self.db.get_connection() as conn:
            n = conn.execute("SELECT COUNT(*) FROM blocked_trades").fetchone()[0]
        self.assertEqual(n, 2)

    def test_different_cid_writes_separate_row(self):
        for cid in ("cid-1", "cid-2"):
            self.db.log_blocked_trade(
                trader="alice", market_question="Will X?",
                condition_id=cid, side="YES", trader_price=0.5,
                block_reason="price_range", block_detail="x"
            )
        with self.db.get_connection() as conn:
            n = conn.execute("SELECT COUNT(*) FROM blocked_trades").fetchone()[0]
        self.assertEqual(n, 2)

    def test_simulated_scan_loop_only_keeps_one(self):
        for _ in range(500):
            self.db.log_blocked_trade(
                trader="sovereign2013",
                market_question="Bucks vs. 76ers: O/U 225.5",
                condition_id="0x14d57e73", side="Under",
                trader_price=0.515,
                block_reason="exposure_limit",
                block_detail="$3 >= $3 max"
            )
        with self.db.get_connection() as conn:
            n = conn.execute("SELECT COUNT(*) FROM blocked_trades").fetchone()[0]
        self.assertEqual(n, 1)


class TestBrainDecisionDedup(unittest.TestCase):
    def setUp(self):
        self.db_path = setup_temp_db()
        from database import db
        self.db = db

    def tearDown(self):
        teardown_temp_db(self.db_path)

    def test_first_call_writes_row(self):
        self.db.log_brain_decision(
            action="PAUSE_TRADER", target="xsaghav",
            reason="7d PnL bad", data="", expected_impact="x"
        )
        with self.db.get_connection() as conn:
            n = conn.execute("SELECT COUNT(*) FROM brain_decisions").fetchone()[0]
        self.assertEqual(n, 1)

    def test_duplicate_action_target_skipped(self):
        for reason in ("7d PnL $-100", "7d PnL $-105", "7d PnL $-110"):
            self.db.log_brain_decision(
                action="PAUSE_TRADER", target="xsaghav",
                reason=reason, data="", expected_impact=""
            )
        with self.db.get_connection() as conn:
            n = conn.execute("SELECT COUNT(*) FROM brain_decisions").fetchone()[0]
        self.assertEqual(n, 1)

    def test_different_target_writes_separate_row(self):
        for trader in ("xsaghav", "fsavhlc", "sovereign2013"):
            self.db.log_brain_decision(
                action="PAUSE_TRADER", target=trader,
                reason="7d PnL bad", data="", expected_impact=""
            )
        with self.db.get_connection() as conn:
            n = conn.execute("SELECT COUNT(*) FROM brain_decisions").fetchone()[0]
        self.assertEqual(n, 3)

    def test_different_action_writes_separate_row(self):
        for action in ("TIGHTEN_FILTER", "RELAX_FILTER"):
            self.db.log_brain_decision(
                action=action, target="KING7777777",
                reason="x", data="", expected_impact=""
            )
        with self.db.get_connection() as conn:
            n = conn.execute("SELECT COUNT(*) FROM brain_decisions").fetchone()[0]
        self.assertEqual(n, 2)

    def test_dedup_hours_zero_disables_guard(self):
        for _ in range(3):
            self.db.log_brain_decision(
                action="KICK_TRADER", target="xsaghav",
                reason="kicked", data="", expected_impact="",
                dedup_hours=0
            )
        with self.db.get_connection() as conn:
            n = conn.execute("SELECT COUNT(*) FROM brain_decisions").fetchone()[0]
        self.assertEqual(n, 3)

    def test_simulated_brain_cycles_dedup(self):
        for _ in range(5):
            self.db.log_brain_decision("TIGHTEN_FILTER", "KING7777777",
                                        "12 BAD_PRICE losses", "", "")
            self.db.log_brain_decision("PAUSE_TRADER", "sovereign2013",
                                        "5 consecutive losses", "", "")
            self.db.log_brain_decision("PAUSE_TRADER", "xsaghav",
                                        "7d PnL bad", "", "")
            self.db.log_brain_decision("PAUSE_TRADER", "fsavhlc",
                                        "7d PnL bad", "", "")
            self.db.log_brain_decision("RELAX_FILTER", "KING7777777",
                                        "tier=solid", "", "")
        with self.db.get_connection() as conn:
            n = conn.execute("SELECT COUNT(*) FROM brain_decisions").fetchone()[0]
        self.assertEqual(n, 5)


class TestTradeScoreDedup(unittest.TestCase):
    """Regression tests for log_trade_score dedup added 2026-04-13 iter 25.

    Observed: scorer runs every scan tick (~5-10s) and writes one row
    per call. Same (sovereign2013, Barcelona Open Buse vs Moutet, QUEUE)
    triple wrote 86 rows in 14 minutes. Inflates feedback cohort and makes
    score-range bucket stats look inverted when the raw numbers are just
    the same trade duplicated 2-7 times.
    """

    def setUp(self):
        self.db_path = setup_temp_db()
        from database import db
        self.db = db
        db._score_dedup_cache.clear()

    def tearDown(self):
        teardown_temp_db(self.db_path)

    def _log(self, **overrides):
        defaults = dict(
            condition_id="cid-score-1",
            trader_name="sovereign2013",
            side="YES",
            entry_price=0.655,
            market_question="Barcelona Open: Buse vs Moutet",
            score_total=53,
            components={"trader_edge": 60, "category_wr": 50,
                        "price_signal": 70, "conviction": 30,
                        "market_quality": 50, "correlation": 50},
            action="QUEUE",
            trade_id=None,
        )
        defaults.update(overrides)
        self.db.log_trade_score(**defaults)

    def _count(self):
        with self.db.get_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM trade_scores").fetchone()[0]

    def test_first_call_writes_row(self):
        self._log()
        self.assertEqual(self._count(), 1)

    def test_duplicate_within_ttl_is_skipped(self):
        for _ in range(10):
            self._log()
        self.assertEqual(self._count(), 1)

    def test_simulated_86_scan_cycles_collapse_to_one(self):
        for _ in range(86):
            self._log()
        self.assertEqual(self._count(), 1)

    def test_different_action_writes_separate_row(self):
        self._log(action="QUEUE")
        self._log(action="EXECUTE")
        self.assertEqual(self._count(), 2)

    def test_different_cid_writes_separate_row(self):
        self._log(condition_id="cid-A")
        self._log(condition_id="cid-B")
        self.assertEqual(self._count(), 2)

    def test_different_trader_writes_separate_row(self):
        self._log(trader_name="alice")
        self._log(trader_name="bob")
        self.assertEqual(self._count(), 2)

    def test_trade_id_bypasses_dedup(self):
        """When a real buy lands, we always want the score stamped with
        its trade_id even if a deduped row already exists."""
        self._log(action="EXECUTE")  # initial score (trade_id=None)
        self._log(action="EXECUTE", trade_id=42)  # buy completed → stamp
        self._log(action="EXECUTE", trade_id=43)  # another buy in same minute
        self.assertEqual(self._count(), 3)

    def test_outcome_lookup_still_finds_newest_null(self):
        """After dedup skips intermediate writes, update_trade_score_outcome
        must still find the one surviving NULL-outcome row for linkage."""
        self._log(action="EXECUTE")
        for _ in range(10):
            self._log(action="EXECUTE")  # all skipped by dedup
        updated = self.db.update_trade_score_outcome(
            condition_id="cid-score-1", trader_name="sovereign2013", pnl=-2.35
        )
        self.assertEqual(updated, 1)
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT outcome_pnl FROM trade_scores WHERE outcome_pnl IS NOT NULL"
            ).fetchone()
        self.assertAlmostEqual(row[0], -2.35)


class TestBrainOscillationMutex(unittest.TestCase):
    """Regression tests for brain.py intra-cycle TIGHTEN/RELAX mutex added
    2026-04-13 iter 25. Observed: KING7777777 got TIGHTEN_FILTER and
    RELAX_FILTER 1 second apart in the same brain cycle because the two
    rules (_classify_losses → _tighten_price_range and
    _revert_obsolete_tightens → RELAX_FILTER) are independent and both
    fire on the same trader. The mutex prevents RELAX from undoing a
    TIGHTEN decided in the same run.
    """

    def setUp(self):
        from bot import brain
        self.brain = brain
        brain._tightened_this_cycle.clear()

    def test_mutex_set_exists_and_resettable(self):
        self.brain._tightened_this_cycle.add("KING7777777")
        self.assertIn("KING7777777", self.brain._tightened_this_cycle)
        self.brain._tightened_this_cycle.clear()
        self.assertNotIn("KING7777777", self.brain._tightened_this_cycle)

    def test_revert_skips_trader_in_mutex(self):
        """_revert_obsolete_tightens must log SKIP and continue without
        calling log_brain_decision for any trader in _tightened_this_cycle.
        We verify by monkey-patching _classify_trader to never be reached
        for the tightened trader."""
        import io
        import logging
        from unittest.mock import patch

        self.brain._tightened_this_cycle.add("KING7777777")

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.INFO)
        brain_logger = logging.getLogger("bot.brain")
        brain_logger.addHandler(handler)
        old_level = brain_logger.level
        brain_logger.setLevel(logging.INFO)

        min_map_fake = {"KING7777777": 0.43, "Jargs": 0.15}
        max_map_fake = {"KING7777777": 0.70, "Jargs": 0.92}

        def fake_parse_map(_content, key):
            return dict(min_map_fake) if key == "MIN_ENTRY_PRICE_MAP" else dict(max_map_fake)

        calls = []
        def fake_get_trader_rolling_pnl(trader, days):
            calls.append(trader)
            return {"total_pnl": 10.0, "cnt": 5, "wins": 4}

        try:
            with patch.object(self.brain, "_parse_map", side_effect=fake_parse_map), \
                 patch.object(self.brain, "_read_settings", return_value="MIN_ENTRY_PRICE_MAP=\nMAX_ENTRY_PRICE_MAP=\n"), \
                 patch.object(self.brain.db, "get_trader_rolling_pnl", side_effect=fake_get_trader_rolling_pnl):
                try:
                    self.brain._revert_obsolete_tightens()
                except Exception:
                    pass  # we only care that the mutex path was exercised
        finally:
            brain_logger.removeHandler(handler)
            brain_logger.setLevel(old_level)

        log_text = buf.getvalue()
        self.assertIn("Skipping RELAX for KING7777777", log_text)
        self.assertNotIn("KING7777777", calls,
                         "get_trader_rolling_pnl should NOT have been called for KING — mutex should have short-circuited")


if __name__ == "__main__":
    unittest.main()
