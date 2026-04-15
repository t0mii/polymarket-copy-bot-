"""Regression tests for `bot.outcome_tracker.get_market_price`.

Covers three bug-classes we have already hit in prod:

1. Gamma /markets silently ignores `conditionId`, `condition_id` (singular)
   and `conditionIds`. Only `condition_ids` (plural snake) actually filters.
   Previous code fell back to `markets[0]` on the default unfiltered list
   and assigned a RANDOM market's prices to the target cid. Silent data
   corruption affecting blocked_trades outcome labels + ML training data.

2. Resolved markets are excluded from the Gamma default `/markets` response
   and only re-appear with `archived=true`. Two-tier fallback required.

3. Even with `condition_ids`, Gamma can return stale/unrelated entries if
   the parameter name ever breaks silently again. Defense-in-depth: validate
   the returned `conditionId` matches what we requested, otherwise treat as
   "no data" (return None).
"""
import unittest
from unittest.mock import patch, MagicMock


def _ok_resp(payload):
    r = MagicMock()
    r.ok = True
    r.json = MagicMock(return_value=payload)
    return r


class TestGammaMarketsFilterParam(unittest.TestCase):
    def test_get_market_price_uses_condition_ids_plural(self):
        """Gamma call must use `condition_ids` (plural+snake), not any of the
        silently-ignored variants."""
        from bot import outcome_tracker
        resp = _ok_resp([
            {"conditionId": "0xabc", "resolved": False, "closed": False,
             "outcomePrices": '["0.55", "0.45"]'}
        ])
        with patch("bot.outcome_tracker.requests.get",
                   return_value=resp) as mock_get:
            outcome_tracker.get_market_price("0xabc", asset="")

            gamma_calls = [
                c for c in mock_get.call_args_list
                if "gamma-api" in str(c).lower() or "/markets" in str(c)
            ]
            self.assertTrue(gamma_calls, "must call Gamma /markets endpoint")
            _, kwargs = gamma_calls[0]
            params = kwargs.get("params", {})
            self.assertIn("condition_ids", params,
                          "Gamma filter param must be condition_ids (plural+snake)")
            self.assertEqual(params["condition_ids"], "0xabc")
            for bad in ("conditionId", "condition_id", "conditionIds"):
                self.assertNotIn(bad, params)

    def test_archived_fallback_fires_when_default_returns_empty(self):
        """If the default Gamma query returns an empty list (resolved market
        excluded), the code must retry with archived=true."""
        from bot import outcome_tracker

        empty = _ok_resp([])
        archived = _ok_resp([
            {"conditionId": "0xresolved", "resolved": True, "closed": True,
             "outcomePrices": '["1.0", "0.0"]'}
        ])
        with patch("bot.outcome_tracker.requests.get",
                   side_effect=[empty, archived]) as mock_get:
            price, is_resolved = outcome_tracker.get_market_price(
                "0xresolved", asset="")

            self.assertEqual(mock_get.call_count, 2,
                             "archived fallback must fire a second call")
            # First call: no archived param
            _, kw1 = mock_get.call_args_list[0]
            self.assertNotIn("archived", kw1.get("params", {}),
                             "first call must be the default active-only query")
            # Second call: archived=true
            _, kw2 = mock_get.call_args_list[1]
            self.assertEqual(kw2.get("params", {}).get("archived"), "true",
                             "second call must add archived=true")
            self.assertEqual(price, 1.0)
            self.assertTrue(is_resolved)

    def test_condition_id_validation_rejects_mismatched_response(self):
        """If Gamma returns a market whose conditionId does NOT match our
        request, treat as no data. Defense against silent param regression."""
        from bot import outcome_tracker

        wrong = _ok_resp([
            {"conditionId": "0xDIFFERENT_MARKET", "resolved": False,
             "closed": False, "outcomePrices": '["0.77", "0.23"]'}
        ])
        # archived call returns the same wrong market
        with patch("bot.outcome_tracker.requests.get",
                   return_value=wrong) as mock_get:
            price, is_resolved = outcome_tracker.get_market_price(
                "0xTARGET", asset="")

            self.assertIsNone(price,
                              "mismatched conditionId must yield None — do NOT "
                              "return the wrong market's prices")
            self.assertFalse(is_resolved)

    def test_condition_id_match_is_case_insensitive(self):
        """Polymarket returns conditionIds mixed-case sometimes. Don't fail
        a match just because 0xABC != 0xabc."""
        from bot import outcome_tracker
        resp = _ok_resp([
            {"conditionId": "0xABC123",
             "resolved": False, "closed": False,
             "outcomePrices": '["0.6", "0.4"]'}
        ])
        with patch("bot.outcome_tracker.requests.get", return_value=resp):
            price, is_resolved = outcome_tracker.get_market_price(
                "0xabc123", asset="")
            self.assertEqual(price, 0.6)

    def test_side_selects_correct_outcome_price(self):
        """Multi-outcome markets (team names, Over/Under): `side` must
        pick the matching index in `outcomes` / `outcomePrices`.

        Without this fix, `get_market_price` always returned outcomePrices[0]
        which is correct ONLY for the first outcome. Any paper_trade or
        blocked_trade on the second outcome got the WRONG price, silently
        contaminating ML labels."""
        from bot import outcome_tracker

        market = {
            "conditionId": "0xSTARSABRES",
            "resolved": False, "closed": False,
            "outcomes": '["Stars", "Sabres"]',
            "outcomePrices": '["0.58", "0.42"]',
        }
        with patch("bot.outcome_tracker.requests.get",
                   return_value=_ok_resp([market])):
            stars_price, _ = outcome_tracker.get_market_price(
                "0xSTARSABRES", side="Stars")
            sabres_price, _ = outcome_tracker.get_market_price(
                "0xSTARSABRES", side="Sabres")
            sabres_upper, _ = outcome_tracker.get_market_price(
                "0xSTARSABRES", side="SABRES")
            unknown, _ = outcome_tracker.get_market_price(
                "0xSTARSABRES", side="NotATeam")
            default, _ = outcome_tracker.get_market_price(
                "0xSTARSABRES")  # no side → backward-compat first outcome

        self.assertEqual(stars_price, 0.58)
        self.assertEqual(sabres_price, 0.42)
        self.assertEqual(sabres_upper, 0.42,
                         "side matching must be case-insensitive")
        self.assertIsNone(unknown,
                          "a side that does not match any outcome returns None")
        self.assertEqual(default, 0.58,
                         "default (no side) returns first outcome price (blocked_trades compat)")


if __name__ == "__main__":
    unittest.main()
