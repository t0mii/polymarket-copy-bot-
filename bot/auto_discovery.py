"""
Auto-Discovery — Findet profitable Trader auf dem Polymarket Leaderboard.
Paper-follows sie und promoted bei guter Performance.
"""
import logging
import time
import requests
from database import db
from bot.wallet_scanner import fetch_wallet_recent_trades

logger = logging.getLogger(__name__)

LEADERBOARD_URL = "https://data-api.polymarket.com/v1/leaderboard"
MAX_CANDIDATES = 50
PROMOTE_MIN_TRADES = 50
PROMOTE_MIN_WINRATE = 55.0

_followed_addresses = set()

# --- PolymarketScan Agent API (free, no auth) ---
POLYSCAN_API = "https://gzydspfquuaudqeztorw.supabase.co/functions/v1/agent-api"
POLYSCAN_AGENT_ID = "maryyo-copybot"
MIN_WHALE_WIN_RATE = 55.0   # Min WR to consider a whale
MIN_WHALE_TRADES = 30       # Min trades for whale validation
MIN_WHALE_PNL = 500         # Min PnL in USD


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
    """Leaderboard scannen und neue Kandidaten finden."""
    _load_followed()
    try:
        all_leaders = []
        for offset in range(0, 100, 50):
            resp = requests.get(LEADERBOARD_URL, params={
                "limit": 50, "offset": offset,
                "timePeriod": "ALL", "orderBy": "PNL"
            }, timeout=15)
            resp.raise_for_status()
            page = resp.json()
            if not page:
                break
            all_leaders.extend(page)
        leaders = all_leaders
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


def paper_follow_candidates():
    """Paper-follow: beobachte Trades der Kandidaten ohne echtes Geld."""
    # First close resolved paper trades
    try:
        close_paper_trades()
    except Exception as e:
        logger.debug("[DISCOVERY] Paper close error: %s", e)

    candidates = db.get_all_candidates("observing")
    for cand in candidates[:20]:
        address = cand["address"]
        try:
            trades = fetch_wallet_recent_trades(address, limit=10)
            for t in trades:
                if t.get("trade_type", "").upper() == "BUY" and t.get("condition_id"):
                    db.add_paper_trade(
                        address, t["condition_id"],
                        t.get("market_question", ""), t.get("side", ""),
                        t.get("price", 0)
                    )
        except Exception as e:
            logger.debug("[DISCOVERY] Paper-follow error for %s: %s", address[:10], e)


def close_paper_trades():
    """Close paper trades: resolved markets OR older than 12h."""
    from datetime import datetime, timedelta

    with db.get_connection() as conn:
        # Get all open paper trades older than 12h
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

    for p in old_papers:
        cid = p["condition_id"]
        entry = p["entry_price"] or 0.5
        side = (p["side"] or "YES").upper()

        # Try to get current price
        price = price_tracker.get_price(cid, side)

        if price is None:
            # No price available — estimate based on time passed (assume small loss)
            price = entry * 0.95  # Assume 5% loss if no data

        # Calculate P&L
        if side == "NO":
            pnl = round((entry - price) * 1.0, 4)
        else:
            pnl = round((price - entry) * 1.0, 4)

        # Close it
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE paper_trades SET status = 'closed', current_price = ?, pnl = ?, "
                "closed_at = datetime('now','localtime') WHERE id = ?",
                (price, pnl, p["id"])
            )

        # Update candidate stats
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
        logger.info("[DISCOVERY] Closed %d paper trades (4h+ old)", closed)


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
            with db.get_connection() as conn:
                conn.execute(
                    "UPDATE trader_candidates SET status = 'promoted', "
                    "promoted_at = datetime('now','localtime') WHERE address = ?",
                    (cand["address"],)
                )
            # CRITICAL: actually add them to FOLLOWED_TRADERS so the bot copies their trades.
            # Without this, "promoted" was just a DB flag with no real effect.
            # _add_followed_trader() also seeds NEUTRAL tier defaults in all per-trader maps
            # via the cold-start fix, so the new trader doesn't fall through to globals.
            #
            # Plus: write to wallets DB directly so the bot picks them up on the next
            # copy_followed_wallets() scan WITHOUT needing a restart. The bot reads
            # followed wallets from DB at runtime (db.get_followed_wallets()), so this
            # makes promotion truly automatic.
            try:
                from bot.trader_lifecycle import _add_followed_trader
                _add_followed_trader(cand["address"], cand["username"])
                # Also insert into wallets DB so the running bot sees them immediately.
                db.add_followed_wallet(cand["address"], cand["username"])
                logger.info("[DISCOVERY] %s now LIVE — added to settings.env + wallets DB (no restart needed)",
                            cand["username"])
            except Exception as _e:
                logger.warning("[DISCOVERY] Failed to add %s to FOLLOWED_TRADERS: %s",
                               cand["username"], _e)
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
