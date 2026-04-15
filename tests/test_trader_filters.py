"""TDD for Scenario D Phase B1.1 — shared trader_filters helper.

The helper `apply_pre_score_filters(trade, trader_name, avg_trader_size, maps)`
implements the 6 decision filters that `copy_trader.copy_followed_wallets`
applies at lines 1744-1820, plus the trade_scorer call. The whole point
is paper-live symmetry: `bot.auto_discovery.paper_follow_candidates`
calls the same helper so paper trades are accepted / rejected by the
same rules live trades are.

Filter order mirrors copy_trader exactly:

    0. category_blacklist     (per-trader map)
    1. min_trader_usd         (per-trader map with global fallback)
    2. conviction_ratio       (per-trader map with global fallback)
    3. max_fee_bps            (global, calls order_executor.get_fee_rate)
    4. price_range            (per-trader map with global fallback)
    5. zero_risk_block        (global category blacklist for underdogs)
    6. trade_scorer           (ML-based EXECUTE / BOOST / BLOCK / QUEUE)

Returns `(passed: bool, reason: str, metadata: dict)` where:
- passed=True iff scorer action is EXECUTE or BOOST
- reason is "ok" on pass, or "<filter_name>: <detail>" on reject
- metadata carries category, fee_bps, ml_score, score_action for the caller
"""
import unittest
from unittest.mock import patch, MagicMock


def _trade(
    side="YES", price=0.55, usdc_size=10.0, condition_id="0xcid_abc",
    question="Will the Ducks win?", event_slug="nhl",
):
    return {
        "side": side,
        "price": price,
        "usdc_size": usdc_size,
        "condition_id": condition_id,
        "market_question": question,
        "event_slug": event_slug,
    }


def _empty_maps() -> dict:
    """Default maps: no per-trader overrides — forces global fallback."""
    return {
        "category_blacklist": {},
        "min_entry_price":    {},
        "max_entry_price":    {},
        "min_trader_usd":     {},
        "min_conviction":     {},
    }


def _permissive_config():
    """Config defaults that let a clean trade through. Tests patch
    specific fields to trigger specific rejection branches."""
    cfg = MagicMock()
    cfg.MIN_ENTRY_PRICE = 0.10
    cfg.MAX_ENTRY_PRICE = 0.95
    cfg.MIN_TRADER_USD = 1.0
    cfg.MIN_CONVICTION_RATIO = 0.0
    cfg.MAX_FEE_BPS = 0
    cfg.ZERO_RISK_CATEGORIES = ""
    cfg.ZERO_RISK_MIN_PRICE = 0.30
    cfg.DEFAULT_AVG_TRADER_SIZE = 100.0
    return cfg


def _execute_scorer():
    return {"action": "EXECUTE", "score": 72, "components": {}, "reason": "ok"}


class TestApplyPreScoreFilters(unittest.TestCase):
    def setUp(self):
        patcher = patch("bot.order_executor.get_fee_rate", return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_happy_path_returns_passed_true(self):
        from bot.trader_filters import apply_pre_score_filters
        with patch("bot.trader_filters.score_trade", return_value=_execute_scorer()):
            passed, reason, meta = apply_pre_score_filters(
                trade=_trade(),
                trader_name="xsaghav",
                avg_trader_size=50.0,
                maps=_empty_maps(),
                config_module=_permissive_config(),
            )
        self.assertTrue(passed, "clean trade must pass, reason=%s" % reason)
        self.assertEqual(reason, "ok")
        self.assertIn("category", meta)
        self.assertIn("score_action", meta)

    def test_category_blacklist_blocks(self):
        from bot.trader_filters import apply_pre_score_filters
        maps = _empty_maps()
        maps["category_blacklist"] = {"xsaghav": {"nhl"}}
        with patch("bot.trader_filters._detect_category", return_value="nhl"):
            passed, reason, meta = apply_pre_score_filters(
                trade=_trade(), trader_name="xsaghav", avg_trader_size=50.0,
                maps=maps, config_module=_permissive_config(),
            )
        self.assertFalse(passed)
        self.assertTrue(reason.startswith("category_blacklist"),
                        "expected category_blacklist, got: %s" % reason)

    def test_min_trader_usd_global_blocks(self):
        from bot.trader_filters import apply_pre_score_filters
        cfg = _permissive_config()
        cfg.MIN_TRADER_USD = 20.0
        passed, reason, _ = apply_pre_score_filters(
            trade=_trade(usdc_size=10.0), trader_name="xsaghav",
            avg_trader_size=50.0, maps=_empty_maps(), config_module=cfg,
        )
        self.assertFalse(passed)
        self.assertTrue(reason.startswith("min_trader_usd"),
                        "expected min_trader_usd, got: %s" % reason)

    def test_min_trader_usd_per_trader_map_overrides_global(self):
        from bot.trader_filters import apply_pre_score_filters
        maps = _empty_maps()
        maps["min_trader_usd"] = {"xsaghav": 25.0}
        passed, reason, _ = apply_pre_score_filters(
            trade=_trade(usdc_size=15.0), trader_name="xsaghav",
            avg_trader_size=50.0, maps=maps, config_module=_permissive_config(),
        )
        self.assertFalse(passed)
        self.assertTrue(reason.startswith("min_trader_usd"),
                        "expected min_trader_usd (per-trader=25), got: %s" % reason)

    def test_conviction_ratio_blocks(self):
        from bot.trader_filters import apply_pre_score_filters
        cfg = _permissive_config()
        cfg.MIN_CONVICTION_RATIO = 2.0
        passed, reason, _ = apply_pre_score_filters(
            trade=_trade(usdc_size=50.0),
            trader_name="king7777777",
            avg_trader_size=100.0,  # ratio = 0.5, needs 2.0
            maps=_empty_maps(), config_module=cfg,
        )
        self.assertFalse(passed)
        self.assertTrue(reason.startswith("conviction_ratio"),
                        "expected conviction_ratio, got: %s" % reason)

    def test_max_fee_bps_blocks(self):
        from bot.trader_filters import apply_pre_score_filters
        cfg = _permissive_config()
        cfg.MAX_FEE_BPS = 500  # 5%
        with patch("bot.order_executor.get_fee_rate", return_value=1000):  # 10%
            passed, reason, meta = apply_pre_score_filters(
                trade=_trade(), trader_name="xsaghav", avg_trader_size=50.0,
                maps=_empty_maps(), config_module=cfg,
            )
        self.assertFalse(passed)
        self.assertTrue(reason.startswith("max_fee"),
                        "expected max_fee, got: %s" % reason)
        self.assertEqual(meta.get("fee_bps"), 1000)

    def test_price_range_per_trader_map_blocks_high(self):
        from bot.trader_filters import apply_pre_score_filters
        maps = _empty_maps()
        maps["max_entry_price"] = {"xsaghav": 0.70}
        passed, reason, _ = apply_pre_score_filters(
            trade=_trade(price=0.80), trader_name="xsaghav",
            avg_trader_size=50.0, maps=maps, config_module=_permissive_config(),
        )
        self.assertFalse(passed)
        self.assertTrue(reason.startswith("price_range"),
                        "expected price_range, got: %s" % reason)

    def test_price_range_per_trader_map_blocks_low(self):
        from bot.trader_filters import apply_pre_score_filters
        maps = _empty_maps()
        maps["min_entry_price"] = {"xsaghav": 0.40}
        passed, reason, _ = apply_pre_score_filters(
            trade=_trade(price=0.30), trader_name="xsaghav",
            avg_trader_size=50.0, maps=maps, config_module=_permissive_config(),
        )
        self.assertFalse(passed)
        self.assertTrue(reason.startswith("price_range"))

    def test_zero_risk_blocks_esports_underdog(self):
        from bot.trader_filters import apply_pre_score_filters
        cfg = _permissive_config()
        cfg.ZERO_RISK_CATEGORIES = "cs,lol"
        cfg.ZERO_RISK_MIN_PRICE = 0.30
        with patch("bot.trader_filters._detect_category", return_value="cs"):
            passed, reason, _ = apply_pre_score_filters(
                trade=_trade(price=0.20),  # below 0.30
                trader_name="xsaghav", avg_trader_size=50.0,
                maps=_empty_maps(), config_module=cfg,
            )
        self.assertFalse(passed)
        self.assertTrue(reason.startswith("zero_risk"),
                        "expected zero_risk, got: %s" % reason)

    def test_scorer_block_action_rejects_trade(self):
        from bot.trader_filters import apply_pre_score_filters
        with patch("bot.trader_filters.score_trade",
                   return_value={"action": "BLOCK", "score": 25,
                                 "components": {}, "reason": "low_edge"}):
            passed, reason, meta = apply_pre_score_filters(
                trade=_trade(), trader_name="xsaghav", avg_trader_size=50.0,
                maps=_empty_maps(), config_module=_permissive_config(),
            )
        self.assertFalse(passed)
        self.assertTrue(reason.startswith("score_block"),
                        "expected score_block, got: %s" % reason)
        self.assertEqual(meta["score_action"], "BLOCK")
        self.assertEqual(meta["ml_score"], 25)

    def test_scorer_queue_action_rejects_trade_but_flags_queue(self):
        """QUEUE means 'wait for more signal' — paper treats it as reject,
        live callers can inspect metadata.score_action=='QUEUE' and enqueue
        instead of discarding."""
        from bot.trader_filters import apply_pre_score_filters
        with patch("bot.trader_filters.score_trade",
                   return_value={"action": "QUEUE", "score": 55,
                                 "components": {}, "reason": "marginal"}):
            passed, reason, meta = apply_pre_score_filters(
                trade=_trade(), trader_name="xsaghav", avg_trader_size=50.0,
                maps=_empty_maps(), config_module=_permissive_config(),
            )
        self.assertFalse(passed)
        self.assertEqual(meta["score_action"], "QUEUE")
        self.assertTrue(reason.startswith("score_queue"),
                        "expected score_queue, got: %s" % reason)

    def test_scorer_boost_action_passes(self):
        from bot.trader_filters import apply_pre_score_filters
        with patch("bot.trader_filters.score_trade",
                   return_value={"action": "BOOST", "score": 88,
                                 "components": {}, "reason": "high_edge"}):
            passed, reason, meta = apply_pre_score_filters(
                trade=_trade(), trader_name="xsaghav", avg_trader_size=50.0,
                maps=_empty_maps(), config_module=_permissive_config(),
            )
        self.assertTrue(passed)
        self.assertEqual(meta["score_action"], "BOOST")
        self.assertEqual(meta["ml_score"], 88)

    def test_scorer_error_defaults_to_execute(self):
        """If the scorer raises, we default to EXECUTE (fail-open). This
        matches the existing copy_trader.py:932-934 exception handling."""
        from bot.trader_filters import apply_pre_score_filters
        with patch("bot.trader_filters.score_trade",
                   side_effect=RuntimeError("model not loaded")):
            passed, reason, meta = apply_pre_score_filters(
                trade=_trade(), trader_name="xsaghav", avg_trader_size=50.0,
                maps=_empty_maps(), config_module=_permissive_config(),
            )
        self.assertTrue(passed, "scorer error must fail-open, got: %s" % reason)
        self.assertEqual(meta["score_action"], "EXECUTE")

    def test_run_scorer_false_skips_ml_check_entirely(self):
        """When called with run_scorer=False, the helper returns passed=True
        after the 6 base filters without calling the ML scorer at all.

        This is the live path mode: live copy_trader keeps its inline
        scorer call at line 2152, so the helper just needs to verify the
        base filters match the old inline block. Adding the scorer
        inside the helper would have moved it BEFORE state-dependent
        filters, which is a semantic change we don't want for live.
        """
        from bot.trader_filters import apply_pre_score_filters
        # A mock that would return BLOCK if called — test asserts it's NOT called
        from unittest.mock import MagicMock
        fake_scorer = MagicMock(return_value={
            "action": "BLOCK", "score": 0, "components": {}, "reason": "test",
        })
        with patch("bot.trader_filters.score_trade", new=fake_scorer):
            passed, reason, meta = apply_pre_score_filters(
                trade=_trade(),
                trader_name="xsaghav",
                avg_trader_size=50.0,
                maps=_empty_maps(),
                config_module=_permissive_config(),
                run_scorer=False,
            )
        self.assertTrue(passed, "run_scorer=False + clean trade must pass")
        self.assertEqual(reason, "ok")
        fake_scorer.assert_not_called()
        self.assertIsNone(meta["score_action"],
                          "score_action must stay None when scorer is skipped")

    def test_run_scorer_false_still_applies_base_filters(self):
        """run_scorer=False skips the scorer but NOT the 6 base filters."""
        from bot.trader_filters import apply_pre_score_filters
        cfg = _permissive_config()
        cfg.MIN_TRADER_USD = 50.0
        passed, reason, _ = apply_pre_score_filters(
            trade=_trade(usdc_size=10), trader_name="xsaghav",
            avg_trader_size=50.0, maps=_empty_maps(), config_module=cfg,
            run_scorer=False,
        )
        self.assertFalse(passed,
                         "base filters must still run when run_scorer=False")
        self.assertTrue(reason.startswith("min_trader_usd"))

    def test_filters_run_in_canonical_order(self):
        """Category blacklist is filter 0 — it must fire before min_trader_usd
        even when both would independently reject."""
        from bot.trader_filters import apply_pre_score_filters
        cfg = _permissive_config()
        cfg.MIN_TRADER_USD = 999.0  # would block usdc_size=10
        maps = _empty_maps()
        maps["category_blacklist"] = {"xsaghav": {"nhl"}}
        with patch("bot.trader_filters._detect_category", return_value="nhl"):
            passed, reason, _ = apply_pre_score_filters(
                trade=_trade(usdc_size=10.0), trader_name="xsaghav",
                avg_trader_size=50.0, maps=maps, config_module=cfg,
            )
        self.assertFalse(passed)
        self.assertTrue(reason.startswith("category_blacklist"),
                        "filter order wrong — category must fire first, got: %s" % reason)


if __name__ == "__main__":
    unittest.main()
