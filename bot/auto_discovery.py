"""
Auto-Discovery — Findet profitable Trader auf dem Polymarket Leaderboard.
Paper-follows sie und promoted bei guter Performance.
"""
import logging
import time
import requests
from database import db
from bot.wallet_scanner import fetch_wallet_recent_trades
import config

logger = logging.getLogger(__name__)

LEADERBOARD_URL = "https://data-api.polymarket.com/v1/leaderboard"
MAX_CANDIDATES = 100
PROMOTE_MIN_TRADES = 50
PROMOTE_MIN_WINRATE = 55.0

_followed_addresses = set()

# --- PolymarketScan Agent API (free, no auth) ---
POLYSCAN_API = "https://gzydspfquuaudqeztorw.supabase.co/functions/v1/agent-api"
POLYSCAN_AGENT_ID = "maryyo-copybot"
MIN_WHALE_WIN_RATE = 55.0   # Min WR to consider a whale
MIN_WHALE_TRADES = 20       # Min trades for whale validation
MIN_WHALE_PNL = 200         # Min PnL in USD


def scan_polyscan_whales():
    """Find profitable traders from PolymarketScan whale feed + trader validation."""
    try:
        # 1. Get recent whale trades
        r = requests.get(POLYSCAN_API, params={
            "action": "whales", "limit": 20, "agent_id": POLYSCAN_AGENT_ID
        }, timeout=15)
        if not r.ok:
            return []
        whales = r.json().get("data", [])
        if not whales:
            return []

        # 2. Extract unique wallets from whale trades (BUY only, skip sells)
        wallets = set()
        for w in whales:
            if w.get("side", "").upper() == "BUY" and w.get("wallet"):
                wallets.add(w["wallet"])

        logger.info("[POLYSCAN] Found %d unique whale wallets from %d trades", len(wallets), len(whales))

        # 3. Validate each wallet via PnL endpoint
        candidates = []
        for wallet in list(wallets)[:10]:  # Max 10 per scan to stay under rate limit
            try:
                r2 = requests.get(POLYSCAN_API, params={
                    "action": "wallet_pnl", "wallet": wallet, "agent_id": POLYSCAN_AGENT_ID
                }, timeout=10)
                if not r2.ok:
                    continue
                data = r2.json().get("data", {})
                summary = data.get("summary", {})
                if not summary:
                    continue

                pnl = summary.get("total_pnl", 0) or 0
                wr = summary.get("win_rate", 0) or 0
                trades = summary.get("trade_count", 0) or 0
                volume = summary.get("total_volume_usd", 0) or 0
                last_trade = summary.get("last_trade_date", "")

                # Filter: profitable, good WR, enough trades
                if pnl >= MIN_WHALE_PNL and wr >= MIN_WHALE_WIN_RATE and trades >= MIN_WHALE_TRADES:
                    candidates.append({
                        "address": wallet,
                        "username": wallet[:12],  # Will be updated later
                        "pnl": pnl,
                        "win_rate": wr,
                        "trades": trades,
                        "volume": volume,
                        "source": "polyscan_whale",
                    })
                    db.upsert_lifecycle_trader(wallet, wallet[:12], "DISCOVERED", "polyscan_whale")
                    logger.info("[POLYSCAN] Good whale: %s | PnL=$%.0f | WR=%.1f%% | %d trades",
                                wallet[:12], pnl, wr, trades)
            except Exception as e:
                logger.debug("[POLYSCAN] Wallet check error for %s: %s", wallet[:10], e)
                continue

        return candidates
    except Exception as e:
        logger.warning("[POLYSCAN] Whale scan error: %s", e)
        return []


def scan_polyscan_traders():
    """Check if existing candidates have PolymarketScan data for better validation."""
    candidates = db.get_all_candidates("observing")
    updated = 0
    for cand in candidates[:20]:  # Rate limit friendly
        try:
            r = requests.get(POLYSCAN_API, params={
                "action": "wallet_pnl", "wallet": cand["address"], "agent_id": POLYSCAN_AGENT_ID
            }, timeout=10)
            if not r.ok:
                continue
            data = r.json().get("data", {})
            summary = data.get("summary", {})
            if not summary:
                continue

            real_pnl = summary.get("total_pnl", 0) or 0
            real_wr = summary.get("win_rate", 0) or 0
            real_trades = summary.get("trade_count", 0) or 0
            real_volume = summary.get("total_volume_usd", 0) or 0

            # Update candidate with real data
            with db.get_connection() as conn:
                conn.execute(
                    "UPDATE trader_candidates SET profit_total=?, winrate=?, "
                    "volume_total=?, markets_traded=? WHERE address=?",
                    (real_pnl, real_wr, real_volume, real_trades, cand["address"])
                )
            updated += 1
        except Exception:
            continue

    if updated:
        logger.info("[POLYSCAN] Updated %d candidate profiles with real stats", updated)


def _load_followed():
    global _followed_addresses
    import config
    for entry in config.FOLLOWED_TRADERS.split(","):
        entry = entry.strip()
        if ":" in entry:
            _followed_addresses.add(entry.split(":", 1)[1].strip().lower())


def scan_leaderboard():
    """Leaderboard scannen — multiple Zeitraeume fuer bessere Abdeckung."""
    _load_followed()
    try:
        all_leaders = []
        seen_addresses = set()
        # ALL-TIME + 30d + 7d + 1d — findet etablierte UND aufsteigende Trader
        for period in ["ALL"]:  # API only supports ALL now (30d/7d/1d return 400)
            for offset in range(0, 100, 50):
                resp = requests.get(LEADERBOARD_URL, params={
                    "limit": 50, "offset": offset,
                    "timePeriod": period,
                }, timeout=15)
                resp.raise_for_status()
                page = resp.json()
                if not page:
                    break
                for entry in page:
                    addr = (entry.get("proxyWallet") or entry.get("userAddress") or "").lower()
                    if addr and addr not in seen_addresses:
                        seen_addresses.add(addr)
                        all_leaders.append(entry)
        leaders = all_leaders
        logger.info("[DISCOVERY] Scanned ALL period, %d unique traders", len(leaders))
    except Exception as e:
        logger.error("[DISCOVERY] Leaderboard fetch failed: %s", e)
        return

    current_candidates = {c["address"].lower() for c in db.get_all_candidates()}
    new_count = 0

    for leader in leaders:
        address = (leader.get("proxyWallet") or leader.get("userAddress") or "").lower()
        if not address:
            continue
        if address in _followed_addresses:
            continue
        if len(current_candidates) >= MAX_CANDIDATES and address not in current_candidates:
            continue

        username = leader.get("userName") or leader.get("username") or address[:10]
        profit = float(leader.get("pnl") or leader.get("profit") or 0)
        volume = float(leader.get("vol") or leader.get("volume") or 0)
        winrate = 0
        markets = 0

        if profit <= 0:
            continue

        db.upsert_candidate(address, username, profit, volume, winrate, markets)
        if address not in current_candidates:
            new_count += 1
            current_candidates.add(address)
            db.upsert_lifecycle_trader(address, username, "DISCOVERED", "leaderboard")
            logger.info("[DISCOVERY] New candidate: %s (profit=$%.0f, vol=$%.0f)",
                        username, profit, volume)

    logger.info("[DISCOVERY] Scan complete: %d new candidates, %d total",
                new_count, len(current_candidates))



def _load_settings_filters():
    """Load filters from settings.env for realistic paper simulation."""
    filters = {
        "min_entry_price": config.MIN_ENTRY_PRICE,
        "max_entry_price": config.MAX_ENTRY_PRICE,
        "bet_size_pct": config.BET_SIZE_PCT,
        "min_trade_size": config.MIN_TRADE_SIZE,
        "max_position_size": config.MAX_POSITION_SIZE,
    }
    try:
        from bot.copy_trader import _detect_category
        filters["detect_category"] = _detect_category
    except ImportError:
        filters["detect_category"] = None
    return filters


def _paper_price_ok(price, filters):
    """Check if price is within our entry range."""
    return filters["min_entry_price"] <= price <= filters["max_entry_price"]


def _paper_bet_size(price, filters):
    """Calculate realistic bet size like copy_trader does."""
    portfolio = config.STARTING_BALANCE
    try:
        stats = db.get_copy_trade_stats()
        portfolio = config.STARTING_BALANCE + stats.get("total_pnl", 0)
    except Exception:
        pass
    size = portfolio * filters["bet_size_pct"]
    size = max(filters["min_trade_size"], min(filters["max_position_size"], size))
    return round(size, 2)


def paper_follow_candidates():
    """Paper-follow: beobachte Trades der Kandidaten mit echten Filtern."""
    try:
        close_paper_trades()
    except Exception as e:
        logger.debug("[DISCOVERY] Paper close error: %s", e)

    # PATCH-038c: Auto-kick candidates with paper_pnl < -100 or 200+ trades negative
    try:
        with db.get_connection() as conn:
            kicked = conn.execute(
                "UPDATE trader_candidates SET status='inactive' "
                "WHERE status='observing' AND (paper_pnl < -100)"
            ).rowcount
            if kicked:
                logger.info("[DISCOVERY] Auto-kicked %d bad candidates (paper PnL < -$100)", kicked)
    except Exception:
        pass

    filters = _load_settings_filters()
    # 2026-04-14: include promoted candidates too. Previously this was
    # `get_all_candidates("observing")` which stopped paper-tracking a
    # candidate at the exact moment we decided to promote them — so
    # promoted traders (our highest-confidence bucket) never accumulated
    # paper_trades post-promotion. Caught by piff on his side where
    # denizz was stuck at 0 new paper_trades after promotion.
    candidates = db.get_active_candidates()
    for cand in candidates[:20]:
        address = cand["address"]
        try:
            # Per-candidate watermark: only trades newer than what we've
            # already seen. Replaces the fixed ENTRY_TRADE_SEC=300 filter
            # which dropped ~97% of trades at the 3h scan cadence.
            last_ts = db.get_candidate_paper_scan_ts(address)
            newest_ts = last_ts
            trades = fetch_wallet_recent_trades(address, limit=50)
            for t in trades:
                # Watermark advance happens BEFORE the BUY/cid filter so that
                # a candidate whose most-recent activity is a SELL still has
                # newest_ts moved forward — otherwise the next scan would
                # re-evaluate the same SELL tail repeatedly.
                t_ts = int(t.get("timestamp", 0) or 0)
                if t_ts > newest_ts:
                    newest_ts = t_ts

                if t.get("trade_type", "").upper() != "BUY" or not t.get("condition_id"):
                    continue

                # Skip trades we already captured on prior scans.
                if t_ts <= last_ts:
                    continue

                price = t.get("price", 0)
                question = t.get("market_question", "")

                # Scenario-D Phase B1: paper-live filter symmetry.
                # One shared helper (`bot/trader_filters.apply_pre_score_filters_live`)
                # runs the same 6 decision filters + ML scorer here as
                # `copy_trader.copy_followed_wallets` applies at 1744-1820.
                # Before B1 this block had 5 global-only filters + no scorer,
                # so paper was testing a fundamentally different bot than live.
                # Now paper and live reject/accept IDENTICAL trades for the
                # same input — and both use per-trader MIN/MAX_ENTRY_PRICE_MAP,
                # CATEGORY_BLACKLIST, MIN_TRADER_USD_MAP, MIN_CONVICTION_MAP.
                try:
                    _buy_sizes = [
                        x.get("usdc_size", 0) for x in trades
                        if x.get("trade_type", "").upper() == "BUY"
                        and x.get("usdc_size", 0) > 0
                    ]
                    _avg_size = (sum(_buy_sizes) / len(_buy_sizes)) if _buy_sizes \
                        else config.DEFAULT_AVG_TRADER_SIZE
                    from bot.trader_filters import apply_pre_score_filters_live
                    _trader_name = (cand.get("username") or address[:12])
                    _passed, _reason, _meta = apply_pre_score_filters_live(
                        trade=t,
                        trader_name=_trader_name,
                        avg_trader_size=_avg_size,
                    )
                except Exception as _fe:
                    logger.debug("[PAPER] filter helper error for %s: %s — skipping",
                                 address[:10], _fe)
                    continue
                if not _passed:
                    logger.debug("[PAPER] filter reject %s: %s",
                                 address[:10], _reason)
                    continue

                bet_size = _paper_bet_size(price, filters)
                shares = bet_size / price if price > 0 else 0

                db.add_paper_trade(
                    address, t["condition_id"],
                    question, t.get("side", ""),
                    price
                )
                logger.debug("[PAPER] Track %s: %s @ %.0fc ($%.2f -> %.1f shares)",
                             address[:10], question[:30], price*100, bet_size, shares)

            if newest_ts > last_ts:
                db.set_candidate_paper_scan_ts(address, newest_ts)
        except Exception as e:
            logger.debug("[DISCOVERY] Paper-follow error for %s: %s", address[:10], e)
        finally:
            # Scenario-D Phase A3: always bump the rotation cursor so a
            # failing candidate can't permanently block other candidates
            # from their scan slot. Pairs with the oldest-first ORDER BY
            # in db.get_active_candidates.
            try:
                db.set_candidate_rotation_ts(address, int(time.time()))
            except Exception:
                pass


def close_paper_trades():
    """Close paper trades via time-budget + price availability.

    Scenario-D Phase B2 refactor. Semantics:

    - Rows with `is_resolved=1` are skipped (track_paper_outcomes already
      handled them via real Gamma resolution data).
    - Rows younger than `PAPER_EVAL_MAX_HOURS` are skipped (too early).
    - Rows older than the budget with a live price from ws_price_tracker
      are closed normally with pnl from shares × (price - entry) and
      close_reason='time_cutoff'. (No `side == 'NO'` inversion needed
      when the live ws price for the asset already reflects that side.)
    - Rows older than the budget but without a live price are LEFT OPEN
      and retried next cycle. The old `entry * 0.95` fake-loss fallback
      is REMOVED — it injected ~5% fabricated losses into every unresolved
      trade and poisoned paper_pnl across the pool.
    - Rows older than `PAPER_EVAL_MAX_HOURS * 3` are force-closed with
      pnl=0 and close_reason='abandoned' to prevent unbounded open-row
      accumulation.
    """
    from datetime import datetime, timedelta

    max_hours = float(getattr(config, "PAPER_EVAL_MAX_HOURS", 24))
    cutoff = (datetime.now() - timedelta(hours=max_hours)).strftime("%Y-%m-%d %H:%M:%S")
    abandon_cutoff = (datetime.now() - timedelta(hours=max_hours * 3)).strftime("%Y-%m-%d %H:%M:%S")

    with db.get_connection() as conn:
        old_papers = conn.execute(
            "SELECT id, candidate_address, condition_id, side, entry_price, created_at "
            "FROM paper_trades "
            "WHERE status = 'open' "
            "  AND COALESCE(is_resolved, 0) = 0 "
            "  AND created_at < ?",
            (cutoff,)
        ).fetchall()

    if not old_papers:
        return

    closed = 0
    abandoned = 0
    from bot.ws_price_tracker import price_tracker
    filters = _load_settings_filters()

    for p in old_papers:
        cid = p["condition_id"]
        entry = p["entry_price"] or 0.5
        side = (p["side"] or "YES").upper()
        created_at_str = p["created_at"] or ""

        price = None
        try:
            price = price_tracker.get_price(cid, side)
        except Exception:
            price = None
        if price is not None and price >= 0.99:
            price = 1.0
        elif price is not None and price <= 0.01:
            price = 0.0

        if price is None:
            # No live price. Two outcomes:
            #   (a) row is past the abandon threshold → force-close pnl=0
            #   (b) row is within the retry window → leave open, try again
            if created_at_str < abandon_cutoff:
                with db.get_connection() as conn:
                    cur = conn.execute(
                        "UPDATE paper_trades SET "
                        "  status = 'closed', pnl = 0, close_reason = 'abandoned', "
                        "  closed_at = datetime('now','localtime') "
                        "WHERE id = ? AND COALESCE(is_resolved, 0) = 0",
                        (p["id"],),
                    )
                    if cur.rowcount > 0:
                        # Rollup counts trade-count only; pnl=0 so pnl column unchanged.
                        conn.execute(
                            "UPDATE trader_candidates SET "
                            "  paper_trades = paper_trades + 1 "
                            "WHERE address = ?",
                            (p["candidate_address"],),
                        )
                        abandoned += 1
            continue

        # Price available — close with real pnl.
        bet_size = _paper_bet_size(entry, filters)
        shares = bet_size / entry if entry > 0 else 0

        # Side-inversion retained for the ws_price_tracker path because the
        # cached WS price may be the YES-side mid even when the trader bought
        # the opposite side. Paper_trades.side="NO" → flip the sign.
        if side == "NO":
            pnl = round(shares * (entry - price), 4)
        else:
            pnl = round(shares * (price - entry), 4)

        with db.get_connection() as conn:
            cur = conn.execute(
                "UPDATE paper_trades SET "
                "  status = 'closed', current_price = ?, pnl = ?, "
                "  close_reason = 'time_cutoff', "
                "  closed_at = datetime('now','localtime') "
                "WHERE id = ? AND COALESCE(is_resolved, 0) = 0",
                (price, pnl, p["id"]),
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
                closed += 1

    if closed > 0 or abandoned > 0:
        logger.info(
            "[DISCOVERY] Closed %d paper trades (time_cutoff), abandoned %d (past %dh*3 without price)",
            closed, abandoned, int(max_hours),
        )


def check_promotions():
    """Evaluate auto-promotion eligibility for observing candidates.

    Scenario D Phase γ/D rewrite: the gate is now a 6-check evaluator
    (Wilson LB + ROI + recency + absolute floor, not just WR). Two
    safety rails wrap it:

    1. `promotion_cooldown_active`: max 1 auto-promotion per N days
    2. `compute_circuit_breaker_state`: halts all auto-promotions if
       any recently-auto-promoted trader has lost more than $X in
       the first Y days of live trading.

    When a candidate passes, we also `start_probation` before the
    `_add_followed_trader` call so they begin with reduced bet size
    + hard exposure cap for the first 14 days or 20 trades.

    Master flag `AUTO_DISCOVERY_AUTO_PROMOTE` still gates the actual
    add-to-follow step — when false, this path only LOGS that a
    candidate would be promoted.
    """
    from bot.promotion import (
        evaluate_promotion,
        promotion_cooldown_active,
        compute_circuit_breaker_state,
        start_probation,
    )
    from datetime import datetime

    candidates = db.get_all_candidates("observing")
    if not candidates:
        return

    cooldown_on, cooldown_reason = promotion_cooldown_active()
    breaker_on, breaker_reason = compute_circuit_breaker_state()

    for cand in candidates:
        stats = db.get_candidate_stats(cand["address"])
        total = int(stats.get("total", 0) or 0)
        wins = int(stats.get("wins", 0) or 0)
        total_pnl = float(stats.get("total_pnl", 0) or 0)

        # Skip if candidate has no recent trades (inactive)
        try:
            from bot.wallet_scanner import fetch_wallet_recent_trades
            recent = fetch_wallet_recent_trades(cand["address"], limit=3)
            if recent:
                newest_ts = max(t.get("timestamp", 0) for t in recent)
                days_since = (time.time() - newest_ts) / 86400 if newest_ts > 0 else 999
                if days_since > 1:
                    continue
            else:
                continue
        except Exception:
            pass

        # Newest paper_trade age (for the recency gate)
        with db.get_connection() as _conn:
            _newest_row = _conn.execute(
                "SELECT MAX(created_at) AS newest "
                "FROM paper_trades WHERE candidate_address=? AND status='closed'",
                (cand["address"],),
            ).fetchone()
        newest_raw = _newest_row["newest"] if _newest_row else ""
        if newest_raw:
            try:
                newest_dt = datetime.strptime(newest_raw, "%Y-%m-%d %H:%M:%S")
                newest_age_days = (datetime.now() - newest_dt).total_seconds() / 86400.0
            except ValueError:
                newest_age_days = 9999.0
        else:
            newest_age_days = 9999.0

        try:
            passed, reason = evaluate_promotion(
                n_trades=total, wins=wins, total_pnl=total_pnl,
                newest_trade_age_days=newest_age_days,
            )
        except ValueError as _e:
            logger.warning("[DISCOVERY] evaluate_promotion error for %s: %s",
                           cand.get("username", "?"), _e)
            continue

        if not passed:
            logger.debug("[DISCOVERY] skip %s: %s",
                         cand.get("username", "?"), reason)
            continue

        if cooldown_on:
            logger.info("[DISCOVERY] %s would pass gate but %s",
                        cand.get("username", "?"), cooldown_reason)
            continue

        if breaker_on:
            logger.warning("[DISCOVERY] CIRCUIT BREAKER BLOCKS %s: %s",
                           cand.get("username", "?"), breaker_reason)
            try:
                db.log_activity(
                    "circuit_breaker", "",
                    "Circuit breaker blocked auto-promotion",
                    breaker_reason,
                )
            except Exception:
                pass
            continue

        winrate = (wins * 100.0 / total) if total else 0.0
        logger.info("[DISCOVERY] PROMOTE candidate: %s (wr=%.1f%%, pnl=$%.2f, trades=%d)",
                    cand["username"], winrate, total_pnl, total)
        # PATCH-038c: promoted -> PAPER_FOLLOW (reset stats, start fresh paper test)
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE trader_candidates SET status = 'promoted', paper_trades=0, paper_pnl=0, paper_wins=0, "
                "promoted_at = datetime('now','localtime') WHERE address = ?",
                (cand["address"],)
            )
            _lc = conn.execute("SELECT id FROM trader_lifecycle WHERE address=?", (cand["address"],)).fetchone()
            if _lc:
                conn.execute(
                    "UPDATE trader_lifecycle SET status='PAPER_FOLLOW', paper_trades=0, paper_pnl=0, paper_wr=0, "
                    "status_changed_at=datetime('now','localtime') WHERE address=?",
                    (cand["address"],))
            else:
                conn.execute(
                    "INSERT INTO trader_lifecycle (address, username, status, status_changed_at, paper_trades, paper_pnl, paper_wr) "
                    "VALUES (?, ?, 'PAPER_FOLLOW', datetime('now','localtime'), 0, 0, 0)",
                    (cand["address"], cand["username"]))
            conn.execute("DELETE FROM paper_trades WHERE candidate_address=?", (cand["address"],))
        logger.info("[DISCOVERY] %s -> PAPER_FOLLOW (fresh paper test)", cand["username"])

        # AUTO_DISCOVERY_AUTO_PROMOTE gates the actual add-to-follow step.
        # When false, this path only logs. When true, the sequence is:
        #   1. start_probation (sets auto_promoted_at + probation window)
        #   2. _add_followed_trader (writes settings.env)
        #   3. add_followed_wallet (writes wallets.followed=1)
        try:
            _auto_promote = getattr(config, "AUTO_DISCOVERY_AUTO_PROMOTE", False)
        except Exception:
            _auto_promote = False
        if _auto_promote:
            try:
                start_probation(cand["address"])
                from bot.trader_lifecycle import _add_followed_trader
                _add_followed_trader(cand["address"], cand["username"])
                db.add_followed_wallet(cand["address"], cand["username"])
                logger.info("[DISCOVERY] %s now LIVE — probation window opened + added to settings.env (AUTO_PROMOTE on)",
                            cand["username"])
            except Exception as _e:
                logger.warning("[DISCOVERY] Failed to add %s to FOLLOWED_TRADERS: %s",
                               cand["username"], _e)
        else:
            logger.info("[DISCOVERY] %s meets promote criteria but AUTO_PROMOTE=false — review manually",
                        cand["username"])
        try:
            db.log_activity("promotion", "",
                            "Trader %s promoted" % cand["username"],
                            "WR: %.1f%%, PnL: $%.2f, Trades: %d" % (winrate, total_pnl, total))
        except Exception:
            pass


INACTIVITY_HOURS = 24  # Pause candidates with no trades for 24h


def check_inactivity():
    """Pause candidates who haven't traded in 24h. They get reactivated when they trade again."""
    for status in ("observing", "promoted"):
        candidates = db.get_all_candidates(status)
        for cand in candidates:
            try:
                recent = fetch_wallet_recent_trades(cand["address"], limit=3)
                if recent:
                    newest_ts = max(t.get("timestamp", 0) for t in recent)
                    hours_since = (time.time() - newest_ts) / 3600 if newest_ts > 0 else 9999
                else:
                    hours_since = 9999

                if hours_since > INACTIVITY_HOURS:
                    with db.get_connection() as conn:
                        conn.execute(
                            "UPDATE trader_candidates SET status = 'inactive' WHERE address = ?",
                            (cand["address"],)
                        )
                    logger.info("[DISCOVERY] INACTIVE: %s (no trades for %.0fh)",
                                cand["username"] or cand["address"][:12], hours_since)
            except Exception as e:
                logger.debug("[DISCOVERY] Inactivity check error for %s: %s",
                             cand["address"][:10], e)


def check_reactivation():
    """Reactivate inactive candidates who started trading again."""
    candidates = db.get_all_candidates("inactive")
    for cand in candidates:
        try:
            recent = fetch_wallet_recent_trades(cand["address"], limit=3)
            if not recent:
                continue

            newest_ts = max(t.get("timestamp", 0) for t in recent)
            hours_since = (time.time() - newest_ts) / 3600 if newest_ts > 0 else 9999

            if hours_since <= INACTIVITY_HOURS:
                with db.get_connection() as conn:
                    conn.execute(
                        "UPDATE trader_candidates SET status = 'observing' WHERE address = ?",
                        (cand["address"],)
                    )
                logger.info("[DISCOVERY] REACTIVATED: %s (traded %.1fh ago)",
                            cand["username"] or cand["address"][:12], hours_since)
        except Exception as e:
            logger.debug("[DISCOVERY] Reactivation check error for %s: %s",
                         cand["address"][:10], e)



def scan_all_sources():
    """Scan all sources for new candidates: Polymarket Leaderboard + PolymarketScan Whales."""
    scan_leaderboard()

    try:
        whale_candidates = scan_polyscan_whales()
        for wc in whale_candidates:
            existing = None
            with db.get_connection() as conn:
                existing = conn.execute(
                    "SELECT address FROM trader_candidates WHERE address=?", (wc["address"],)
                ).fetchone()

            if not existing:
                with db.get_connection() as conn:
                    conn.execute(
                        "INSERT INTO trader_candidates (address, username, source, profit_total, "
                        "volume_total, winrate, markets_traded, status, discovered_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'observing', datetime('now','localtime'))",
                        (wc["address"], wc["username"], wc["source"],
                         wc["pnl"], wc["volume"], wc["win_rate"], wc["trades"])
                    )
                logger.info("[DISCOVERY] New from PolymarketScan: %s (PnL=$%.0f, WR=%.1f%%)",
                            wc["username"], wc["pnl"], wc["win_rate"])
    except Exception as e:
        logger.warning("[DISCOVERY] PolymarketScan scan error: %s", e)

    try:
        scan_polyscan_traders()
    except Exception as e:
        logger.debug("[DISCOVERY] PolymarketScan update error: %s", e)
