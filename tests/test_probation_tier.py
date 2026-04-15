"""TDD for Scenario D Phase γ.5 — probation tier state management.

When auto_discovery auto-promotes a trader to live, they enter a
probation window before getting full NEUTRAL tier sizing. During
probation:

- bet size is PROBATION_BET_SIZE_PCT (50%) of NEUTRAL baseline
- max exposure is PROBATION_MAX_EXPOSURE_USD ($5) hard cap
- duration is min(PROBATION_DURATION_DAYS, PROBATION_MAX_TRADES) —
  whichever expires first

This module provides the state primitives. Actual wiring into
`bot/copy_trader.py` bet-sizing is deferred to a separate shadow-
canary commit because it touches the live trading path.
"""
import unittest

from tests.conftest_helpers import setup_temp_db, teardown_temp_db


def _ms_ago(days: float) -> str:
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _ms_future(days: float) -> str:
    from datetime import datetime, timedelta
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


ADDR = "0xprob"
USER = "probie"


class TestProbationTier(unittest.TestCase):
    def setUp(self):
        self.path = setup_temp_db()
        from database import db
        self.db = db
        import config
        self._saved = {
            "days": getattr(config, "PROBATION_DURATION_DAYS", 14),
            "trades": getattr(config, "PROBATION_MAX_TRADES", 20),
        }
        config.PROBATION_DURATION_DAYS = 14
        config.PROBATION_MAX_TRADES = 20
        with self.db.get_connection() as conn:
            conn.execute(
                "INSERT INTO trader_candidates (address, username, status) "
                "VALUES (?, ?, 'promoted')",
                (ADDR, USER),
            )

    def tearDown(self):
        import config
        config.PROBATION_DURATION_DAYS = self._saved["days"]
        config.PROBATION_MAX_TRADES = self._saved["trades"]
        teardown_temp_db(self.path)

    def _get_cand(self):
        with self.db.get_connection() as conn:
            return dict(conn.execute(
                "SELECT auto_promoted_at, probation_until, probation_trades_left "
                "FROM trader_candidates WHERE address=?", (ADDR,)
            ).fetchone())

    def test_start_probation_sets_all_three_fields(self):
        from bot.promotion import start_probation
        start_probation(ADDR)

        row = self._get_cand()
        self.assertNotEqual(row["auto_promoted_at"], "")
        self.assertNotEqual(row["probation_until"], "")
        self.assertEqual(row["probation_trades_left"], 20)

    def test_is_in_probation_true_after_start(self):
        from bot.promotion import start_probation, is_in_probation
        start_probation(ADDR)
        active, _ = is_in_probation(USER)
        self.assertTrue(active)

    def test_is_in_probation_false_for_fresh_trader(self):
        from bot.promotion import is_in_probation
        active, _ = is_in_probation(USER)
        self.assertFalse(active,
                         "trader with no probation state must not be in probation")

    def test_is_in_probation_graduates_after_time(self):
        """Manually set probation_until to a past timestamp → no longer active."""
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE trader_candidates SET auto_promoted_at=?, "
                "probation_until=?, probation_trades_left=5 WHERE address=?",
                (_ms_ago(15), _ms_ago(1), ADDR),
            )
        from bot.promotion import is_in_probation
        active, reason = is_in_probation(USER)
        self.assertFalse(active,
                         "probation must auto-graduate when probation_until is past, reason=%s" % reason)

    def test_is_in_probation_graduates_after_trade_budget_exhausted(self):
        """probation_trades_left=0 → graduated, even if time window is still open."""
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE trader_candidates SET auto_promoted_at=?, "
                "probation_until=?, probation_trades_left=0 WHERE address=?",
                (_ms_ago(2), _ms_future(12), ADDR),
            )
        from bot.promotion import is_in_probation
        active, _ = is_in_probation(USER)
        self.assertFalse(active,
                         "probation must graduate when trades_left reaches 0")

    def test_decrement_probation_trade(self):
        from bot.promotion import start_probation, decrement_probation_trade
        start_probation(ADDR)
        decrement_probation_trade(USER)

        row = self._get_cand()
        self.assertEqual(row["probation_trades_left"], 19)

    def test_decrement_never_goes_negative(self):
        from bot.promotion import start_probation, decrement_probation_trade
        start_probation(ADDR)
        for _ in range(25):
            decrement_probation_trade(USER)

        row = self._get_cand()
        self.assertEqual(row["probation_trades_left"], 0)

    def test_decrement_noop_for_non_probation_trader(self):
        """A manually-followed trader with no probation state must not
        get a bogus negative probation_trades_left."""
        from bot.promotion import decrement_probation_trade
        decrement_probation_trade(USER)

        row = self._get_cand()
        self.assertEqual(row["probation_trades_left"], 0,
                         "decrement on a non-probation trader must not bias the column")

    def test_probation_limits_returns_bet_multiplier_and_exposure_cap(self):
        """Caller-facing helper returns the two numbers copy_trader needs
        to apply during bet sizing. Not in probation → (1.0, None)."""
        from bot.promotion import probation_limits
        import config
        config.PROBATION_BET_SIZE_PCT = 0.5
        config.PROBATION_MAX_EXPOSURE_USD = 5.0

        mult, cap = probation_limits(USER)
        self.assertEqual(mult, 1.0)
        self.assertIsNone(cap)

        from bot.promotion import start_probation
        start_probation(ADDR)
        mult, cap = probation_limits(USER)
        self.assertEqual(mult, 0.5)
        self.assertEqual(cap, 5.0)


class TestCalculatePositionSizeProbationOverride(unittest.TestCase):
    """γ.5b wiring: `_calculate_position_size` must scale + cap bet sizes
    for traders in the probation window. Non-probation traders get the
    standard sizing unchanged."""

    def setUp(self):
        self.path = setup_temp_db()
        from database import db
        self.db = db
        import config
        # Patch copy_trader module globals (MAX_POSITION_SIZE and
        # BET_SIZE_PCT are captured at import time; setting the config
        # values does NOT propagate to the already-imported module).
        from bot import copy_trader as ct
        self._saved_max = ct.MAX_POSITION_SIZE
        self._saved_bet = ct.BET_SIZE_PCT
        ct.MAX_POSITION_SIZE = 50.0  # raise so probation cap can bind
        ct.BET_SIZE_PCT = 0.1  # 10% so base is clearly > 5 on $200 cash
        self._saved = {
            "pct": getattr(config, "PROBATION_BET_SIZE_PCT", 0.5),
            "cap": getattr(config, "PROBATION_MAX_EXPOSURE_USD", 5.0),
            "days": getattr(config, "PROBATION_DURATION_DAYS", 14),
            "trades": getattr(config, "PROBATION_MAX_TRADES", 20),
            "bet_pct": getattr(config, "BET_SIZE_PCT", 0.04),
        }
        config.PROBATION_BET_SIZE_PCT = 0.5
        config.PROBATION_MAX_EXPOSURE_USD = 5.0
        config.PROBATION_DURATION_DAYS = 14
        config.PROBATION_MAX_TRADES = 20
        config.BET_SIZE_PCT = 0.1  # 10% of cash so base is clearly > 5

    def tearDown(self):
        import config
        from bot import copy_trader as ct
        ct.MAX_POSITION_SIZE = self._saved_max
        ct.BET_SIZE_PCT = self._saved_bet
        for k, v in (("PROBATION_BET_SIZE_PCT", self._saved["pct"]),
                     ("PROBATION_MAX_EXPOSURE_USD", self._saved["cap"]),
                     ("PROBATION_DURATION_DAYS", self._saved["days"]),
                     ("PROBATION_MAX_TRADES", self._saved["trades"]),
                     ("BET_SIZE_PCT", self._saved["bet_pct"])):
            setattr(config, k, v)
        teardown_temp_db(self.path)

    def _seed_probation_trader(self, addr: str, username: str):
        from bot.promotion import start_probation
        with self.db.get_connection() as conn:
            conn.execute(
                "INSERT INTO trader_candidates (address, username, status) "
                "VALUES (?, ?, 'promoted')", (addr, username),
            )
        start_probation(addr)

    def test_non_probation_trader_gets_standard_size(self):
        """A manually-followed trader (no probation state) gets whatever
        the standard formula returns — not the probation cap."""
        from bot import copy_trader as ct
        size = ct._calculate_position_size(
            entry_price=0.55, cash=200.0, trader_ratio=1.0,
            portfolio_value=200.0, trader_name="manually_followed",
        )
        # With default BET_SIZE_PCT ~3% of 200 ≈ $6, not capped to $5
        self.assertGreater(size, 5.0,
                           "non-probation trader must not hit probation cap")

    def test_probation_trader_hits_exposure_cap(self):
        self._seed_probation_trader("0xprobsz", "probsz")
        from bot import copy_trader as ct
        size = ct._calculate_position_size(
            entry_price=0.55, cash=200.0, trader_ratio=1.0,
            portfolio_value=200.0, trader_name="probsz",
        )
        self.assertLessEqual(size, 5.0,
                             "probation trader must be capped at $5, got $%.2f" % size)

    def test_probation_trader_bet_multiplier_halves_size(self):
        """If the uncapped size would have been well below $5, the cap
        doesn't bind. But the 0.5 multiplier must still halve it."""
        import config
        config.PROBATION_BET_SIZE_PCT = 0.5
        config.PROBATION_MAX_EXPOSURE_USD = 100.0  # cap inactive

        self._seed_probation_trader("0xprobmul", "probmul")
        from bot import copy_trader as ct
        # Non-probation baseline for comparison
        base = ct._calculate_position_size(
            entry_price=0.55, cash=200.0, trader_ratio=1.0,
            portfolio_value=200.0, trader_name="not_in_probation",
        )
        probation = ct._calculate_position_size(
            entry_price=0.55, cash=200.0, trader_ratio=1.0,
            portfolio_value=200.0, trader_name="probmul",
        )
        # Probation must be ≈ 50% of base (within MIN_TRADE_SIZE floor)
        if probation > 0:
            self.assertLessEqual(probation, base * 0.55,
                                 "probation size must be ~50%% of base, got %.2f vs %.2f"
                                 % (probation, base))


if __name__ == "__main__":
    unittest.main()
