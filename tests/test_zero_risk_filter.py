"""Regression tests for the zero-risk category filter added 2026-04-13.

Context: KING7777777 repeatedly bought underdog sides on esports maps
(CS/LoL/Valorant/Dota) at prices below ~0.35, and those markets have a
disproportionate rate of resolving to zero (not just losing, but total
stake loss). Concrete recent examples:
- #3035 NBA Spurs spread entry 0.51 -> resolved 0 (NBA already covered
  by _ts_thin_book trailing-stop disable but still bought)
- #3128 CS Phantom vs HEROIC Academy Map 1 entry 0.266 -> resolved 0
- #3129 CS Phantom vs HEROIC Academy Map 2 entry 0.266 -> resolved 0

Filter: if category in ZERO_RISK_CATEGORIES and trader_price <
ZERO_RISK_MIN_PRICE, block the copy. Both the diff-scan and
activity-scan buy paths must honor this.
"""
import unittest
from unittest.mock import patch


class TestZeroRiskHelper(unittest.TestCase):
    def setUp(self):
        import config
        self.config = config
        self._orig_cats = config.ZERO_RISK_CATEGORIES
        self._orig_min = config.ZERO_RISK_MIN_PRICE
        config.ZERO_RISK_CATEGORIES = "cs,lol,valorant,dota"
        config.ZERO_RISK_MIN_PRICE = 0.40

    def tearDown(self):
        self.config.ZERO_RISK_CATEGORIES = self._orig_cats
        self.config.ZERO_RISK_MIN_PRICE = self._orig_min

    def _helper(self):
        from bot.copy_trader import _is_zero_risk_block
        return _is_zero_risk_block

    def test_cs_underdog_blocked(self):
        f = self._helper()
        self.assertTrue(f("cs", 0.266))

    def test_cs_at_threshold_allowed(self):
        f = self._helper()
        self.assertFalse(f("cs", 0.40))

    def test_cs_above_threshold_allowed(self):
        f = self._helper()
        self.assertFalse(f("cs", 0.55))

    def test_lol_underdog_blocked(self):
        f = self._helper()
        self.assertTrue(f("lol", 0.30))

    def test_valorant_underdog_blocked(self):
        f = self._helper()
        self.assertTrue(f("valorant", 0.38))

    def test_dota_underdog_blocked(self):
        f = self._helper()
        self.assertTrue(f("dota", 0.15))

    def test_nba_underdog_NOT_blocked_by_this_filter(self):
        """NBA thin-book is already handled by _ts_thin_book trailing-stop
        disable. This filter is esports-only."""
        f = self._helper()
        self.assertFalse(f("nba", 0.25))

    def test_case_insensitive(self):
        f = self._helper()
        self.assertTrue(f("CS", 0.30))
        self.assertTrue(f("Valorant", 0.30))

    def test_empty_category_not_blocked(self):
        f = self._helper()
        self.assertFalse(f("", 0.20))
        self.assertFalse(f(None, 0.20))

    def test_unknown_category_not_blocked(self):
        f = self._helper()
        self.assertFalse(f("tennis", 0.20))

    def test_feature_disabled_when_list_empty(self):
        self.config.ZERO_RISK_CATEGORIES = ""
        f = self._helper()
        self.assertFalse(f("cs", 0.10))

    def test_reproduces_3128_and_3129(self):
        """#3128 and #3129 both had category=cs entry_price=0.266.
        Filter must block both at threshold 0.40."""
        f = self._helper()
        self.assertTrue(f("cs", 0.266))


if __name__ == "__main__":
    unittest.main()
