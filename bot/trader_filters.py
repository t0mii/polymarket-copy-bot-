"""Scenario D Phase B1 — shared paper/live filter chain.

`apply_pre_score_filters` implements the 6 decision filters that
`bot/copy_trader.py::copy_followed_wallets` applies at lines
1744-1820, plus the trade_scorer ML check. Both the live path
(`copy_trader`) and the paper path (`auto_discovery`) call this
helper so their accept/reject verdicts are BYTE-IDENTICAL for the
same input trade.

This is the fix for the Phase-1 root cause we identified: paper had
5 global filters + no scorer, live had 13 per-trader filters + ML.
Paper was testing a fundamentally different bot than live. B1 aligns
the 6 decision filters between them; the state-dependent filters
(max_copies, hedge, exposure, event_full, market_too_long, etc.)
remain inline in copy_trader because they cannot meaningfully run
in paper (paper has no portfolio, no hedge queue, no event state).

Filter order mirrors copy_trader.copy_followed_wallets:

    0. category_blacklist   (per-trader map from _CATEGORY_BLACKLIST)
    1. min_trader_usd       (per-trader map with global fallback)
    2. conviction_ratio     (per-trader map with global fallback)
    3. max_fee_bps          (global, via order_executor.get_fee_rate)
    4. price_range          (per-trader MIN/MAX_ENTRY_PRICE_MAP + global)
    5. zero_risk_block      (global esports-underdog guard)
    6. trade_scorer         (ML: EXECUTE / BOOST / BLOCK / QUEUE)

Returns (passed, reason, metadata):
    - passed=True iff the scorer action is EXECUTE or BOOST
    - reason='ok' on pass; '<filter_name>: <detail>' on reject
    - metadata always carries: category, detected values, score_action,
      ml_score, fee_bps (whenever computed)

The helper is fully isolated from module-level state: callers pass
the maps dict + config module explicitly. This makes it trivially
testable and eliminates import cycles between copy_trader and
auto_discovery.
"""
from bot.trade_scorer import score as score_trade


def _detect_category(question: str) -> str:
    """Thin wrapper so tests can patch without importing copy_trader's
    internals directly (avoids import cycles + keeps patching simple)."""
    from bot.copy_trader import _detect_category as _det
    return _det(question)


def _is_zero_risk_block(category: str, trader_price: float, config_module) -> bool:
    """Local copy of copy_trader._is_zero_risk_block that uses the passed
    config_module (so tests can override without monkey-patching the
    global `config` namespace)."""
    raw = getattr(config_module, "ZERO_RISK_CATEGORIES", "") or ""
    cats = {c.strip().lower() for c in raw.split(",") if c.strip()}
    if not cats:
        return False
    cat = (category or "").lower()
    if cat not in cats:
        return False
    try:
        return float(trader_price) < float(
            getattr(config_module, "ZERO_RISK_MIN_PRICE", 0.30))
    except (TypeError, ValueError):
        return False


def apply_pre_score_filters(
    trade: dict,
    trader_name: str,
    avg_trader_size: float,
    maps: dict,
    config_module=None,
) -> tuple:
    """Run the 6 base decision filters + trade_scorer on a single trade."""
    if config_module is None:
        import config as config_module

    tn_lower = (trader_name or "").lower()
    side = trade.get("side", "YES")
    price = float(trade.get("price", 0))
    usdc_size = float(trade.get("usdc_size", 0) or 0)
    cid = trade.get("condition_id", "")
    question = trade.get("market_question", "") or ""
    event_slug = trade.get("event_slug", "") or ""

    category = _detect_category(question)
    metadata = {
        "category": category,
        "score_action": None,
        "ml_score": None,
        "fee_bps": None,
    }

    # Filter 0: category blacklist (per-trader)
    cat_bl_map = maps.get("category_blacklist", {}) or {}
    blocked_cats = cat_bl_map.get(tn_lower, set())
    if blocked_cats and category and category in blocked_cats:
        return (False,
                "category_blacklist: %s blocked for %s" % (category, trader_name),
                metadata)

    # Filter 1: min_trader_usd
    min_usd_map = maps.get("min_trader_usd", {}) or {}
    min_usd = float(min_usd_map.get(tn_lower, getattr(config_module, "MIN_TRADER_USD", 0)))
    if usdc_size < min_usd:
        return (False,
                "min_trader_usd: $%.1f < $%.0f min" % (usdc_size, min_usd),
                metadata)

    # Filter 2: conviction_ratio
    min_conv_map = maps.get("min_conviction", {}) or {}
    min_conv = float(min_conv_map.get(
        tn_lower, getattr(config_module, "MIN_CONVICTION_RATIO", 0)))
    if min_conv > 0 and avg_trader_size and avg_trader_size > 0:
        conv = usdc_size / avg_trader_size
        if conv < min_conv:
            return (False,
                    "conviction_ratio: %.2fx < %.2fx min" % (conv, min_conv),
                    metadata)

    # Filter 3: max_fee_bps
    max_fee = int(getattr(config_module, "MAX_FEE_BPS", 0) or 0)
    if cid:
        try:
            from bot.order_executor import get_fee_rate
            fee_bps = int(get_fee_rate(cid, side) or 0)
            metadata["fee_bps"] = fee_bps
            if max_fee > 0 and fee_bps > max_fee:
                return (False,
                        "max_fee: %dbps > %dbps max" % (fee_bps, max_fee),
                        metadata)
        except Exception:
            # fee lookup failed — never block on this (matches
            # copy_trader.py:1787-1788 existing behavior)
            pass

    # Filter 4: price_range (per-trader)
    min_price_map = maps.get("min_entry_price", {}) or {}
    max_price_map = maps.get("max_entry_price", {}) or {}
    min_price = float(min_price_map.get(
        tn_lower, getattr(config_module, "MIN_ENTRY_PRICE", 0.0)))
    max_price = float(max_price_map.get(
        tn_lower, getattr(config_module, "MAX_ENTRY_PRICE", 1.0)))
    if price < min_price or price > max_price:
        return (False,
                "price_range: %.0fc outside %.0f-%.0fc" % (
                    price * 100, min_price * 100, max_price * 100),
                metadata)

    # Filter 5: zero_risk_block
    if _is_zero_risk_block(category, price, config_module):
        return (False,
                "zero_risk: %s underdog at %.0fc" % (category, price * 100),
                metadata)

    # Filter 6: trade_scorer
    try:
        score_result = score_trade(
            trader_name=trader_name,
            condition_id=cid,
            side=side,
            entry_price=price,
            market_question=question,
            category=category,
            event_slug=event_slug,
            trader_size_usd=usdc_size,
            spread=0.03,
            hours_until_event=12,
        )
    except Exception as _e:
        # Fail-open — matches copy_trader.py:932-934 behavior
        score_result = {
            "action": "EXECUTE",
            "score": 50,
            "components": {},
            "reason": "scorer_error: %s" % _e,
        }

    action = score_result.get("action", "EXECUTE")
    metadata["score_action"] = action
    metadata["ml_score"] = score_result.get("score")
    metadata["score_components"] = score_result.get("components", {})

    if action == "BLOCK":
        return (False,
                "score_block: %s" % score_result.get("reason", "low_score"),
                metadata)
    if action == "QUEUE":
        # Paper treats QUEUE as reject. Live callers inspect
        # metadata.score_action == 'QUEUE' and enqueue instead of skipping.
        return (False,
                "score_queue: %s (paper rejects, live enqueues)" %
                score_result.get("reason", "marginal"),
                metadata)

    return (True, "ok", metadata)


def apply_pre_score_filters_live(trade: dict, trader_name: str,
                                 avg_trader_size: float) -> tuple:
    """Thin wrapper: reads per-trader maps from copy_trader module globals."""
    from bot import copy_trader as ct
    import config as _config
    maps = {
        "category_blacklist": ct._CATEGORY_BLACKLIST,
        "min_entry_price":    ct._MIN_ENTRY_PRICE_MAP,
        "max_entry_price":    ct._MAX_ENTRY_PRICE_MAP,
        "min_trader_usd":     ct._MIN_TRADER_USD_MAP,
        "min_conviction":     ct._MIN_CONVICTION_MAP,
    }
    return apply_pre_score_filters(
        trade=trade,
        trader_name=trader_name,
        avg_trader_size=avg_trader_size,
        maps=maps,
        config_module=_config,
    )
