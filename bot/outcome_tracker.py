"""Outcome Tracker — checks what would have happened with blocked trades.

Periodically queries Polymarket API for final/current prices of markets
where we blocked a trade, and records whether it would have been a winner.

Uses the Gamma Markets API (condition_id based) for price lookups,
falling back to CLOB book (token_id/asset based) for live markets.
"""
import logging
import time
import requests

import config
from database import db

logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


def _gamma_market_row(condition_id: str, extra_params: dict | None = None):
    """Low-level helper: single GET to Gamma /markets, return the first
    market dict whose conditionId matches `condition_id` (case-insensitive)
    or None if no match / error. Validates the response to defend against
    silent param regressions.
    """
    params = {"condition_ids": condition_id, "limit": 1}
    if extra_params:
        params.update(extra_params)
    try:
        r = requests.get(f"{GAMMA_API}/markets", params=params,
                         timeout=config.API_TIMEOUT)
    except Exception:
        return None
    if not r.ok:
        return None
    try:
        markets = r.json()
    except Exception:
        return None
    if not isinstance(markets, list) or not markets:
        return None
    target = condition_id.lower()
    for m in markets:
        cid = (m.get("conditionId") or "").lower()
        if cid == target:
            return m
    return None


def _parse_market_price(m: dict, side: str = "") -> tuple:
    """Extract (price, is_resolved) from a validated Gamma market dict.

    If `side` is provided, return the price for THAT outcome (matched
    case-insensitively against the `outcomes` list). For multi-outcome
    markets like "Team A vs Team B", `side='Team A'` returns the price
    for Team A, not just outcomes[0]. If side does not match any outcome,
    returns (None, resolved) — the caller must not use a stale fallback
    because a wrong price silently contaminates downstream ML training.

    If `side` is empty, returns outcomePrices[0] (backward compatibility
    for `track_outcomes` / blocked_trades which previously did not pass
    side).
    """
    resolved = bool(m.get("resolved", False) or m.get("closed", False))
    price_str = m.get("outcomePrices", "")
    outcomes_raw = m.get("outcomes", "")
    if price_str:
        try:
            import json
            prices = json.loads(price_str) if isinstance(price_str, str) else price_str
            if not prices:
                prices = None
        except (json.JSONDecodeError, ValueError, TypeError):
            prices = None
        if prices is not None:
            if side:
                try:
                    outcomes = (json.loads(outcomes_raw)
                                if isinstance(outcomes_raw, str)
                                else outcomes_raw) or []
                    target = side.strip().lower()
                    for i, name in enumerate(outcomes):
                        if str(name).strip().lower() == target and i < len(prices):
                            return float(prices[i]), resolved
                    return None, resolved  # side specified but no match
                except (json.JSONDecodeError, ValueError, TypeError):
                    return None, resolved
            # No side → first outcome (backward compat for blocked_trades path)
            return float(prices[0]), resolved
    best_ask = float(m.get("bestAsk", 0) or 0)
    best_bid = float(m.get("bestBid", 0) or 0)
    if best_ask > 0 and best_bid > 0:
        return (best_bid + best_ask) / 2, resolved
    if best_ask > 0:
        return best_ask, resolved
    return None, resolved


def get_market_price(condition_id: str, asset: str = "", side: str = "") -> tuple:
    """Return (price, is_resolved) for a Polymarket market.

    Strategy order:
      1. CLOB book via asset/token_id — best for active markets w/ live quotes.
         Ignored for multi-outcome markets when `side` is provided because
         CLOB returns a single book price that may not correspond to `side`.
      2. Gamma /markets?condition_ids=<cid> — default active-market query.
      3. Gamma /markets?condition_ids=<cid>&archived=true — fallback for
         resolved/archived markets that the default query excludes.

    If `side` is passed, the returned price is the one matching that
    outcome name (e.g. "Stars" in "Stars vs Sabres"). If side does not
    match any outcome, returns (None, _). Without side, returns
    outcomePrices[0] (backward compat).

    Safety: the Gamma response is always validated by comparing the
    returned conditionId to what we requested (case-insensitive).
    Mismatched rows are rejected. This defends against silent param
    regressions — we have already been bitten once today (conditionId
    vs condition_ids).

    Returns (None, False) if no reliable price is available. Never
    invents or extrapolates a price — a missing price must bubble up.
    """
    if asset and not side:
        # CLOB book path is only safe without `side` because the book
        # is keyed on token_id and returns a single number. With side,
        # we want the full outcomes/outcomePrices array from Gamma so
        # we can pick the right index.
        try:
            r = requests.get(f"{CLOB_API}/book", params={"token_id": asset},
                             timeout=config.API_TIMEOUT)
            if r.ok:
                book = r.json()
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                if bids and asks:
                    best_bid = float(bids[0].get("price", 0))
                    best_ask = float(asks[0].get("price", 0))
                    return (best_bid + best_ask) / 2, False
                # Empty book = likely resolved → fall through to Gamma.
        except Exception:
            pass

    if condition_id:
        m = _gamma_market_row(condition_id)
        if m is None:
            m = _gamma_market_row(condition_id, {"archived": "true"})
        if m is not None:
            price, resolved = _parse_market_price(m, side=side)
            if price is not None:
                return price, resolved

    return None, False


def _would_trade_have_won(side: str, trader_price: float, outcome_price: float,
                          is_resolved: bool = False) -> bool:
    """Determine if a blocked trade would have been profitable.

    Handles all side formats:
    - "YES"/"NO" — standard binary
    - Team names, "Over"/"Under" — treated as YES-equivalent (bought that outcome)

    For resolved markets: price near 1.0 = this outcome won.
    For live markets: check if price moved favorably from entry.
    """
    if outcome_price is None:
        return False

    if is_resolved:
        # Resolved: price >= 0.95 means this outcome won
        if side.upper() in ("NO", "N"):
            return outcome_price <= 0.05  # NO wins when price → 0
        else:
            return outcome_price >= 0.95  # YES/team/Over wins when price → 1

    # Live market: check if price moved favorably
    if side.upper() in ("NO", "N"):
        return outcome_price < trader_price - 0.05
    else:
        # YES, team name, Over, Under — all are "bought this outcome"
        return outcome_price > trader_price + 0.05


def track_outcomes():
    """Check outcomes for blocked trades that haven't been checked yet.

    Only checks trades older than 2 hours (give markets time to develop).
    Marks resolved markets definitively, live markets tentatively (if >4h old).
    """
    # Backfill: fill trade_scores.outcome_pnl from closed copy_trades
    try:
        n = db.backfill_trade_score_outcomes(days=30)
        if n > 0:
            logger.info("[OUTCOME] Backfilled %d trade_scores.outcome_pnl rows", n)
    except Exception as e:
        logger.debug("[OUTCOME] backfill error: %s", e)

    # limit=500 gives ~24k labels/day at the 30min schedule, vs 4800
    # at the old 100-limit. Combined with the DESC ordering in db.py,
    # Filter Precision Audit gets enough labeled samples per block_reason
    # (min_samples=100) within 1-2 hours to show new buckets. The 0.2s
    # sleep per row means 500 rows take ~100s per run — still 94% idle
    # on the 30min interval, so no risk of overlap.
    unchecked = db.get_blocked_trades_unchecked(limit=500)
    if not unchecked:
        return 0

    checked = 0
    errors = 0
    for bt in unchecked:
        cid = bt["condition_id"]
        if not cid:
            continue

        asset = bt.get("asset", "") or ""
        price, is_resolved = get_market_price(cid, asset)
        if price is None:
            errors += 1
            continue

        if is_resolved:
            # Resolved: definitive outcome
            won = 1 if _would_trade_have_won(bt["side"], bt["trader_price"], price, True) else 0
            db.update_blocked_trade_outcome(bt["id"], round(price, 4), won)
            checked += 1
        elif price >= 0.99 or price <= 0.01:
            # Clearly resolved even if API doesn't flag it
            won = 1 if _would_trade_have_won(bt["side"], bt["trader_price"], price, True) else 0
            db.update_blocked_trade_outcome(bt["id"], round(price, 4), won)
            checked += 1
        else:
            # Live market — only update if trade is old enough (>4h)
            try:
                from datetime import datetime
                created = datetime.strptime(bt["created_at"], "%Y-%m-%d %H:%M:%S")
                age_hours = (datetime.now() - created).total_seconds() / 3600
                if age_hours > 4:
                    won = 1 if _would_trade_have_won(bt["side"], bt["trader_price"], price, False) else 0
                    db.update_blocked_trade_outcome(bt["id"], round(price, 4), won)
                    checked += 1
            except Exception:
                pass

        # Rate limit: don't hammer the API
        time.sleep(0.2)

    if checked > 0 or errors > 0:
        logger.info("[OUTCOME] Checked %d/%d blocked trade outcomes (%d API errors)",
                    checked, len(unchecked), errors)
    return checked


def track_paper_outcomes():
    """Check outcomes for open paper_trades that haven't been resolution-checked.

    For each row with status='open' AND is_resolved=0:
    - Call get_market_price(cid, side=row.side) — side-aware, so multi-outcome
      markets return the price for THE trader's side, not outcomes[0].
    - If price is None: leave the row alone (no data yet, retry next cycle).
    - If is_resolved=True from Gamma: close the row with the real resolved
      price, compute pnl via the standard paper bet-size helper, update
      trader_candidates rollups, and mark close_reason 'resolved_yes' or
      'resolved_no' based on pnl sign.
    - If not resolved (live market): update current_price only; row stays open.

    This replaces the 4h `entry * 0.95` fake-loss fallback that
    `bot/auto_discovery.py::close_paper_trades` used — paper pnl is now
    derived from real market outcomes instead of an artificial 5% haircut.

    Gated by `config.PAPER_RESOLUTION_TRACKING_ENABLED` (default True).
    Idempotent — the `WHERE is_resolved=0` guard in both the SELECT and
    the UPDATE prevents double-counting on consecutive cycles.
    """
    if not getattr(config, "PAPER_RESOLUTION_TRACKING_ENABLED", True):
        return 0

    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT id, candidate_address, condition_id, side, entry_price "
            "FROM paper_trades "
            "WHERE status = 'open' AND COALESCE(is_resolved, 0) = 0 "
            "LIMIT 250"
        ).fetchall()

    if not rows:
        return 0

    updated = 0
    resolved_count = 0
    for p in rows:
        cid = p["condition_id"]
        if not cid:
            continue
        side = (p["side"] or "YES")
        entry = float(p["entry_price"] or 0)
        if entry <= 0:
            continue

        try:
            price, is_resolved = get_market_price(cid, asset="", side=side)
        except Exception as e:
            logger.debug("[PAPER_OUTCOME] get_market_price error for %s: %s", cid[:14], e)
            price, is_resolved = None, False

        if price is None:
            # No data — try again next cycle. Do NOT inject a fake price.
            time.sleep(0.2)
            continue

        if is_resolved:
            try:
                from bot.auto_discovery import _paper_bet_size, _load_settings_filters
                filters = _load_settings_filters()
                bet_size = _paper_bet_size(entry, filters)
            except Exception:
                bet_size = 1.0

            shares = bet_size / entry if entry > 0 else 0
            pnl = round(shares * (price - entry), 4)
            close_reason = "resolved_yes" if pnl > 0 else "resolved_no"

            with db.get_connection() as conn:
                cur = conn.execute(
                    "UPDATE paper_trades SET "
                    "  status='closed', current_price=?, resolved_price=?, "
                    "  is_resolved=1, close_reason=?, pnl=?, "
                    "  closed_at=datetime('now','localtime') "
                    "WHERE id = ? AND COALESCE(is_resolved, 0) = 0",
                    (price, price, close_reason, pnl, p["id"]),
                )
                if cur.rowcount > 0:
                    if pnl > 0:
                        conn.execute(
                            "UPDATE trader_candidates SET "
                            "  paper_trades = paper_trades + 1, "
                            "  paper_wins = paper_wins + 1, "
                            "  paper_pnl = paper_pnl + ? "
                            "WHERE address = ?",
                            (pnl, p["candidate_address"]),
                        )
                    else:
                        conn.execute(
                            "UPDATE trader_candidates SET "
                            "  paper_trades = paper_trades + 1, "
                            "  paper_pnl = paper_pnl + ? "
                            "WHERE address = ?",
                            (pnl, p["candidate_address"]),
                        )
                    updated += 1
                    resolved_count += 1
        else:
            # Live market — just refresh current_price so the dashboard view
            # has a real unrealized-pnl number. Status stays 'open'.
            with db.get_connection() as conn:
                cur = conn.execute(
                    "UPDATE paper_trades SET current_price = ? "
                    "WHERE id = ? AND COALESCE(is_resolved, 0) = 0",
                    (price, p["id"]),
                )
                if cur.rowcount > 0:
                    updated += 1

        time.sleep(0.2)

    if updated > 0:
        logger.info("[PAPER_OUTCOME] Updated %d rows (%d resolved, %d still-open price-refresh)",
                    updated, resolved_count, updated - resolved_count)
    return updated
