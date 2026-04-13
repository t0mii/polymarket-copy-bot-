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
    candidates = db.get_all_candidates("observing")
    for cand in candidates[:20]:
        address = cand["address"]
        try:
            trades = fetch_wallet_recent_trades(address, limit=10)
            for t in trades:
                if t.get("trade_type", "").upper() != "BUY" or not t.get("condition_id"):
                    continue

                price = t.get("price", 0)
                question = t.get("market_question", "")

                # Filter 1: Price range (same as copy_trader)
                if not _paper_price_ok(price, filters):
                    continue

                # Filter 2: Category blacklist (global)
                if filters.get("detect_category"):
                    cat = filters["detect_category"](question)
                    global_bl = getattr(config, "GLOBAL_CATEGORY_BLACKLIST", "")
                    if global_bl and cat and cat.lower() in global_bl.lower():
                        continue

                # PATCH-038: Additional filters for paper-trade realism
                # Filter 3: Min Trader USD
                usdc_size = t.get("usdc_size", 0) or 0
                if usdc_size < config.MIN_TRADER_USD:
                    continue

                # Filter 4: Conviction ratio
                if config.MIN_CONVICTION_RATIO > 0:
                    buy_sizes = [x.get("usdc_size", 0) for x in trades if x.get("trade_type", "").upper() == "BUY" and x.get("usdc_size", 0) > 0]
                    avg_size = (sum(buy_sizes) / len(buy_sizes)) if buy_sizes else config.DEFAULT_AVG_TRADER_SIZE
                    if avg_size > 0 and (usdc_size / avg_size) < config.MIN_CONVICTION_RATIO:
                        continue

                # Filter 5: Zero-risk block (esports underdogs)
                try:
                    from bot.copy_trader import _is_zero_risk_block, _detect_category
                    _paper_cat = _detect_category(question)
                    if _is_zero_risk_block(_paper_cat, price):
                        continue
                except Exception:
                    pass

                # Filter 6: Trade staleness
                trade_age = int(time.time()) - (t.get("timestamp", 0) or 0)
                if trade_age > config.ENTRY_TRADE_SEC:
                    continue

                # Filter 7: Fee check
                if config.MAX_FEE_BPS > 0:
                    try:
                        from bot.order_executor import get_fee_rate
                        _fee = get_fee_rate(t["condition_id"], t.get("side", ""))
                        if _fee > config.MAX_FEE_BPS:
                            continue
                    except Exception:
                        pass

                bet_size = _paper_bet_size(price, filters)
                shares = bet_size / price if price > 0 else 0

                db.add_paper_trade(
                    address, t["condition_id"],
                    question, t.get("side", ""),
                    price
                )
                logger.debug("[PAPER] Track %s: %s @ %.0fc ($%.2f -> %.1f shares)",
                             address[:10], question[:30], price*100, bet_size, shares)
        except Exception as e:
            logger.debug("[DISCOVERY] Paper-follow error for %s: %s", address[:10], e)


def close_paper_trades():
    """Close paper trades mit realistischer PnL-Berechnung (echte Bet-Sizes)."""
    from datetime import datetime, timedelta

    with db.get_connection() as conn:
        cutoff = (datetime.now() - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S")
        old_papers = conn.execute(
            "SELECT id, candidate_address, condition_id, side, entry_price, created_at "
            "FROM paper_trades WHERE status = 'open' AND created_at < ?",
            (cutoff,)
        ).fetchall()

    if not old_papers:
        return

    closed = 0
    from bot.ws_price_tracker import price_tracker
    filters = _load_settings_filters()

    for p in old_papers:
        cid = p["condition_id"]
        entry = p["entry_price"] or 0.5
        side = (p["side"] or "YES").upper()

        price = price_tracker.get_price(cid, side)
        # PATCH-038: close resolved markets instantly
        if price is not None and price >= 0.99:
            price = 1.0
        elif price is not None and price <= 0.01:
            price = 0.0
        elif price is None:
            price = entry * 0.95

        # Realistic PnL: shares * price_change
        bet_size = _paper_bet_size(entry, filters)
        shares = bet_size / entry if entry > 0 else 0

        if side == "NO":
            pnl = round(shares * (entry - price), 4)
        else:
            pnl = round(shares * (price - entry), 4)

        with db.get_connection() as conn:
            conn.execute(
                "UPDATE paper_trades SET status = 'closed', current_price = ?, pnl = ?, "
                "closed_at = datetime('now','localtime') WHERE id = ?",
                (price, pnl, p["id"])
            )

        with db.get_connection() as conn:
            if pnl > 0:
                conn.execute(
                    "UPDATE trader_candidates SET paper_trades = paper_trades + 1, "
                    "paper_wins = paper_wins + 1, paper_pnl = paper_pnl + ? WHERE address = ?",
                    (pnl, p["candidate_address"])
                )
            else:
                conn.execute(
                    "UPDATE trader_candidates SET paper_trades = paper_trades + 1, "
                    "paper_pnl = paper_pnl + ? WHERE address = ?",
                    (pnl, p["candidate_address"])
                )

        closed += 1

    if closed > 0:
        logger.info("[DISCOVERY] Closed %d paper trades (realistic PnL, 4h+ old)", closed)


def check_promotions():
    """Pruefe ob Kandidaten promoted werden koennen."""
    candidates = db.get_all_candidates("observing")
    for cand in candidates:
        stats = db.get_candidate_stats(cand["address"])
        total = stats.get("total", 0) or 0
        wins = stats.get("wins", 0) or 0
        total_pnl = stats.get("total_pnl", 0) or 0

        # Skip if candidate has no recent trades (inactive)
        try:
            from bot.wallet_scanner import fetch_wallet_recent_trades
            recent = fetch_wallet_recent_trades(cand["address"], limit=3)
            if recent:
                newest_ts = max(t.get("timestamp", 0) for t in recent)
                days_since = (time.time() - newest_ts) / 86400 if newest_ts > 0 else 999
                if days_since > 1:
                    continue  # Inactive, skip
            else:
                continue  # No trades at all
        except Exception:
            pass

        if total < PROMOTE_MIN_TRADES:
            continue

        winrate = round(wins / total * 100, 1) if total > 0 else 0
        if winrate >= PROMOTE_MIN_WINRATE and total_pnl > 0:
            logger.info("[DISCOVERY] PROMOTE candidate: %s (wr=%.1f%%, pnl=$%.2f, trades=%d)",
                        cand["username"], winrate, total_pnl, total)
            # PATCH-038c: promoted -> PAPER_FOLLOW (reset stats, start fresh paper test)
            with db.get_connection() as conn:
                conn.execute(
                    "UPDATE trader_candidates SET status = 'promoted', paper_trades=0, paper_pnl=0, paper_wins=0, "
                    "promoted_at = datetime('now','localtime') WHERE address = ?",
                    (cand["address"],)
                )
                # Move to PAPER_FOLLOW in lifecycle
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
                # Delete old paper trades for fresh start
                conn.execute("DELETE FROM paper_trades WHERE candidate_address=?", (cand["address"],))
            logger.info("[DISCOVERY] %s -> PAPER_FOLLOW (fresh paper test)", cand["username"])
            # Previously: promoted candidates were automatically added to
            # FOLLOWED_TRADERS and wallets table via _add_followed_trader +
            # add_followed_wallet. That caused WHALE_AUTO_COPY_PATH: overnight
            # 2 unapproved whales (0x3e5b23e9f7, 0x6bab41a0dc) were silently
            # auto-followed and created 4 real losing copy_trades totalling
            # -$0.81 without user consent. They also bypassed the tier
            # constraint because they were never in any per-trader MAP.
            #
            # Now: gated behind AUTO_DISCOVERY_AUTO_PROMOTE setting (default
            # FALSE). When false, auto_discovery still tracks candidates and
            # logs PROMOTE recommendations, but the actual add-to-follow step
            # requires manual opt-in (edit settings.env to add the trader).
            # The candidates stay in trader_candidates table with
            # status='promoted' so the dashboard can surface them for user
            # review.
            try:
                _auto_promote = getattr(config, "AUTO_DISCOVERY_AUTO_PROMOTE", False)
            except Exception:
                _auto_promote = False
            if _auto_promote:
                try:
                    from bot.trader_lifecycle import _add_followed_trader
                    _add_followed_trader(cand["address"], cand["username"])
                    db.add_followed_wallet(cand["address"], cand["username"])
                    logger.info("[DISCOVERY] %s now LIVE — added to settings.env + wallets DB (AUTO_PROMOTE on)",
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
