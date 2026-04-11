import logging
import time

import requests

import config

logger = logging.getLogger(__name__)

POLYMARKET_PROFILE_URL = "https://polymarket.com/profile"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# Zero-Drawdown-Schwelle: 100% WR + min. N Trades → verdächtig (Manipulation/Insider)
ZERO_DRAWDOWN_MIN_TRADES = 20

# Domain-Keywords für Trader-Spezialisierung
_DOMAIN_KEYWORDS = {
    "Sports":   ["nba", "nfl", "nhl", "mlb", "soccer", "football", "tennis", "basketball",
                 "baseball", "hockey", "match", "championship", "playoff", "super bowl",
                 "world cup", "tournament", "league", "season", "game", "score", "player",
                 "team", "coach", "transfer", "draft", "mvp"],
    "Crypto":   ["bitcoin", "btc", "eth", "ethereum", "crypto", "token", "blockchain",
                 "defi", "nft", "altcoin", "stablecoin", "exchange", "mining", "solana",
                 "price", "ath", "bear", "bull", "market cap", "doge", "xrp", "binance"],
    "Politics": ["president", "election", "vote", "senate", "congress", "trump", "biden",
                 "harris", "republican", "democrat", "government", "policy", "minister",
                 "parliament", "party", "candidate", "poll", "tariff", "war", "nato",
                 "supreme court", "impeach", "inauguration", "nominee"],
    "Finance":  ["stock", "fed", "interest rate", "gdp", "inflation", "earnings", "ipo",
                 "recession", "s&p", "nasdaq", "oil", "gold", "unemployment", "cpi",
                 "nasdaq", "dow jones", "treasury", "bond", "yield", "central bank"],
}


def _detect_domain(questions: list[str]) -> str:
    """Erkennt die Haupt-Kategorie eines Traders anhand seiner Trade-Fragen."""
    scores = {d: 0 for d in _DOMAIN_KEYWORDS}
    for q in questions:
        q_lower = q.lower()
        for domain, keywords in _DOMAIN_KEYWORDS.items():
            for kw in keywords:
                if kw in q_lower:
                    scores[domain] += 1
    best = max(scores, key=scores.get)
    total = sum(scores.values())
    if total == 0:
        return "General"
    if scores[best] / total >= 0.40:
        return best
    return "Mixed"


def fetch_wallet_positions(address: str) -> list[dict]:
    """Fetch ALL positions for a wallet via data-api (with pagination)."""
    try:
        all_positions = []
        offset = 0
        page_size = 500
        while True:
            response = requests.get(
                f"{DATA_API}/positions",
                params={
                    "user": address,
                    "limit": page_size,
                    "offset": offset,
                    "sizeThreshold": 0.1,
                    "sortBy": "CURRENT",
                    "sortDirection": "DESC",
                },
                timeout=15,
            )
            response.raise_for_status()
            page = response.json()
            if not page:
                break
            all_positions.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
        positions = all_positions

        results = []
        for p in positions:
            outcome = p.get("outcome", "")
            # Side: YES/NO for binary markets, otherwise the actual outcome name
            if outcome.lower() in ("yes", "y"):
                side = "YES"
            elif outcome.lower() in ("no", "n"):
                side = "NO"
            else:
                side = outcome or "YES"
            # Outcome-Label: Bei Multi-Outcome-Märkten ist der Titel der Option-Name
            # z.B. "Lakers", "Pistons (-3.5)" statt "Will Lakers win?"
            title = p.get("title") or p.get("question", "")
            if outcome.lower() not in ("yes", "no", "y", "n", ""):
                # Outcome ist direkt der Name (z.B. "Lakers")
                outcome_label = outcome
            elif len(title) < 50 and "?" not in title:
                # Kurzer Titel ohne Fragezeichen = Option-Name (z.B. "Lakers")
                outcome_label = title
            else:
                outcome_label = ""
            results.append({
                "market_question": p.get("title") or p.get("question", "Unknown"),
                "market_slug": p.get("slug") or p.get("eventSlug", ""),
                "event_slug": p.get("eventSlug") or "",
                "side": side,
                "outcome_label": outcome_label,
                "size": float(p.get("currentValue") or p.get("size") or 0),
                "avg_price": float(p.get("avgPrice") or p.get("averagePrice") or 0),
                "current_price": float(p.get("curPrice") or p.get("currentPrice") or 0),
                "pnl": float(p.get("cashPnl") or p.get("pnl") or 0),
                "end_date": p.get("endDate") or "",
                "redeemable": bool(p.get("redeemable", False)),
                "condition_id": p.get("conditionId", ""),
                "asset": p.get("asset", ""),
            })

        return results

    except requests.RequestException as e:
        logger.debug("Failed to fetch positions for %s: %s", address[:10], e)
        return []


def fetch_wallet_trades(address: str) -> dict:
    """Fetch closed positions + activity count for win rate and trade stats."""
    try:
        # Paginate closed positions to get accurate count (up to 500)
        all_closed = []
        offset = 0
        page_size = 50  # API max is 50
        while len(all_closed) < 500:
            response = requests.get(
                f"{DATA_API}/closed-positions",
                params={"user": address, "limit": page_size, "offset": offset},
                timeout=15,
            )
            response.raise_for_status()
            page = response.json()
            if not page:
                break
            all_closed.extend(page)
            if len(page) < page_size:
                break
            offset += page_size

        # Also count total activity (BUY+SELL) for trade count
        act_resp = requests.get(
            f"{DATA_API}/activity",
            params={"user": address, "type": "TRADE", "limit": 1},
            timeout=10,
        )
        # Use closed positions count as base
        closed = all_closed
        if not closed:
            return {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "avg_trade_size": 0}

        wins = sum(1 for t in closed if float(t.get("realizedPnl") or 0) > 0)
        losses = sum(1 for t in closed if float(t.get("realizedPnl") or 0) < 0)
        total = len(closed)
        sizes = [abs(float(t.get("totalBought") or 0)) for t in closed]

        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total * 100, 1) if total > 0 else 0,
            "avg_trade_size": round(sum(sizes) / len(sizes), 2) if sizes else 0,
        }

    except requests.RequestException as e:
        logger.debug("Failed to fetch closed positions for %s: %s", address[:10], e)
        return {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "avg_trade_size": 0}


def fetch_wallet_recent_trades(address: str, limit: int = 50) -> list[dict]:
    """Fetch recent trade activities for a wallet (newest first).

    Uses /activity?type=TRADE endpoint (same as CryptoVictormt/polymarket-copy-trading-bot).
    Key advantage: usdcSize field = exact dollar amount spent (no calculation needed).
    """
    try:
        response = requests.get(
            f"{DATA_API}/activity",
            params={"user": address, "type": "TRADE", "limit": limit},
            timeout=15,
        )
        response.raise_for_status()
        trades = response.json()

        # Fallback: /trades endpoint works for proxy wallets where /activity returns nothing
        if not trades:
            try:
                _fb = requests.get(
                    f"{DATA_API}/trades",
                    params={"user": address, "limit": limit},
                    timeout=15,
                )
                _fb.raise_for_status()
                _fb_trades = _fb.json()
                if _fb_trades:
                    result = []
                    for t in _fb_trades:
                        outcome = t.get("outcome", "")
                        if outcome.lower() in ("yes", "y"):
                            side = "YES"
                        elif outcome.lower() in ("no", "n"):
                            side = "NO"
                        else:
                            side = outcome or "YES"
                        outcome_label = outcome if side not in ("YES", "NO") else ""
                        _price = float(t.get("price") or 0)
                        _size = float(t.get("size") or 0)
                        result.append({
                            "transaction_hash": t.get("transactionHash", ""),
                            "condition_id": t.get("conditionId", ""),
                            "side": side,
                            "outcome_label": outcome_label,
                            "price": _price,
                            "usdc_size": round(_price * _size, 2),  # calculated from price * shares
                            "timestamp": int(t.get("timestamp") or 0),
                            "market_question": t.get("market") or "",
                            "market_slug": "",
                            "event_slug": "",
                            "trade_type": t.get("side", ""),  # "BUY" or "SELL"
                            "end_date": "",
                        })
                    logger.debug("Used /trades fallback for %s: %d trades", address[:10], len(result))
                    return result
            except Exception:
                pass
            return []

        result = []
        for t in trades:
            outcome = t.get("outcome", "")
            if outcome.lower() in ("yes", "y"):
                side = "YES"
            elif outcome.lower() in ("no", "n"):
                side = "NO"
            else:
                side = outcome or "YES"

            outcome_label = outcome if side not in ("YES", "NO") else ""

            result.append({
                "transaction_hash": t.get("transactionHash", ""),
                "condition_id": t.get("conditionId", ""),
                "side": side,
                "outcome_label": outcome_label,
                "price": float(t.get("price") or 0),
                "usdc_size": float(t.get("usdcSize") or 0),   # exact dollar amount
                "timestamp": int(t.get("timestamp") or 0),
                "market_question": t.get("title") or "",
                "market_slug": t.get("slug") or "",
                "event_slug": t.get("eventSlug") or "",
                "trade_type": t.get("side", ""),  # "BUY" or "SELL"
                "end_date": t.get("endDate") or t.get("end_date") or "",
            })
        return result
    except requests.RequestException as e:
        logger.debug("Failed to fetch recent trades for %s: %s", address[:10], e)
        return []


def fetch_wallet_closed_positions(address: str, limit: int = 500) -> list[dict]:
    """Fetch ALL closed positions with condition_id for smart trade matching.
    
    Returns list of closed positions that we can match against our copy trades.
    """
    try:
        all_closed = []
        offset = 0
        page_size = 50  # API max is 50

        while len(all_closed) < limit and offset <= 5000:
            response = requests.get(
                f"{DATA_API}/closed-positions",
                params={
                    "user": address,
                    "limit": page_size,
                    "offset": offset,
                },
                timeout=15,
            )
            response.raise_for_status()
            page = response.json()
            
            if not page:
                break
            
            # Parse closed positions with condition_id if available
            for pos in page:
                closed_item = {
                    "market_question": pos.get("title") or pos.get("question", ""),
                    "condition_id": pos.get("conditionId", ""),
                    "asset": pos.get("asset", ""),
                    "side": pos.get("outcome", ""),
                    "closed_price": float(pos.get("closePrice") or pos.get("settlementPrice") or 0),
                    "realized_pnl": float(pos.get("realizedPnl") or pos.get("pnl") or 0),
                    "closed_at": pos.get("closedAt") or pos.get("updatedAt", ""),
                }
                all_closed.append(closed_item)
            
            if len(page) < page_size:
                break
            
            offset += page_size
            import time
            time.sleep(0.2)
        
        logger.debug("Fetched %d closed positions for %s", len(all_closed), address[:10])
        return all_closed[:limit]
    
    except requests.RequestException as e:
        logger.debug("Failed to fetch closed positions for %s: %s", address[:10], e)
        return []


def fetch_wallet_profile(address: str) -> dict:
    """Fetch public profile for a wallet."""
    try:
        response = requests.get(
            f"{GAMMA_API}/public-profile",
            params={"address": address},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return {}


