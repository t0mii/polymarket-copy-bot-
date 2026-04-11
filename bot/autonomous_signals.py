"""
Autonomous Trading Signals — eigene Trades basierend auf Marktdaten.
Signale: Momentum, Volume-Spike, Orderbook-Imbalance.
Separates Budget: max 20% Portfolio, 2% Einsatz pro Trade, SL -30%.
Startet im PAPER-Modus bis manuell auf Live geschaltet wird.
"""
import logging
import time
import requests

import os
import config
from database import db
from bot.ws_price_tracker import price_tracker

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"

AUTONOMOUS_BUDGET_PCT = 0.20
AUTONOMOUS_BET_PCT = 0.02
AUTONOMOUS_SL_PCT = 0.30
PAPER_MODE = os.getenv('AUTONOMOUS_PAPER_MODE', 'true').lower() in ('true', '1', 'yes')

MOMENTUM_THRESHOLD = 0.05
MOMENTUM_MIN_PRICE = 0.15
MOMENTUM_MAX_PRICE = 0.85


def _get_current_balance() -> float:
    stats = db.get_copy_trade_stats()
    return config.STARTING_BALANCE + stats["total_pnl"]


def _get_autonomous_budget() -> float:
    return _get_current_balance() * AUTONOMOUS_BUDGET_PCT


def _get_autonomous_exposure() -> float:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(size), 0) as total FROM autonomous_trades WHERE status = 'open'"
        ).fetchone()
        return row["total"] if row else 0


def scan_momentum_signals():
    """Scanne alle subscribed Maerkte auf Momentum-Signale."""
    if _get_autonomous_exposure() >= _get_autonomous_budget():
        return

    if not hasattr(price_tracker, "get_momentum"):
        return

    with price_tracker._lock:
        conditions = list(price_tracker._condition_map.keys())

    signals = []
    for cid in conditions:
        for side in ["YES", "NO"]:
            momentum = price_tracker.get_momentum(cid, side, window_secs=300)
            if momentum is None:
                continue
            price = price_tracker.get_price(cid, side)
            if price is None or price < MOMENTUM_MIN_PRICE or price > MOMENTUM_MAX_PRICE:
                continue

            if abs(momentum) >= MOMENTUM_THRESHOLD:
                if momentum > 0 and side == "YES":
                    signals.append({
                        "type": "momentum",
                        "condition_id": cid,
                        "side": "YES",
                        "price": price,
                        "magnitude": abs(momentum),
                    })
                elif momentum < 0 and side == "NO":
                    # Negative momentum on NO = price dropping = good for NO
                    no_price = 1.0 - price if price < 1 else 0.05
                    if no_price >= MOMENTUM_MIN_PRICE and no_price <= MOMENTUM_MAX_PRICE:
                        signals.append({
                            "type": "momentum",
                            "condition_id": cid,
                            "side": "NO",
                            "price": no_price,
                            "magnitude": abs(momentum),
                        })

    signals.sort(key=lambda s: s["magnitude"], reverse=True)
    for signal in signals[:2]:
        _execute_signal(signal)


def _execute_signal(signal: dict):
    cid = signal["condition_id"]
    side = signal["side"]
    price = signal["price"]

    budget = _get_autonomous_budget()
    exposure = _get_autonomous_exposure()
    if exposure >= budget:
        return

    balance = _get_current_balance()
    amount = round(balance * AUTONOMOUS_BET_PCT, 2)
    amount = min(amount, budget - exposure, config.MAX_POSITION_SIZE)

    if amount < config.MIN_TRADE_SIZE:
        return

    question = ""
    try:
        resp = requests.get("%s/markets" % GAMMA_API,
                            params={"conditionId": cid}, timeout=5)
        if resp.ok and resp.json():
            question = resp.json()[0].get("question", "")
    except Exception:
        pass

    if PAPER_MODE:
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO autonomous_trades (signal_type, condition_id, market_question, "
                "side, entry_price, size, score, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
                (signal["type"], cid, question, side, price, amount,
                 int(signal.get("magnitude", 0) * 100))
            )
        logger.info("[AUTO-PAPER] %s signal: %s @ %.0fc | $%.2f | %s",
                    signal["type"].upper(), side, price * 100, amount, question[:50])
    else:
        from bot.order_executor import buy_shares
        result = buy_shares(cid, side, amount, price)
        if result:
            actual_size = result.get("usdc_spent", amount)
            with db.get_connection() as conn:
                conn.execute(
                    "INSERT INTO autonomous_trades (signal_type, condition_id, market_question, "
                    "side, entry_price, size, score, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
                    (signal["type"], cid, question, side,
                     result.get("effective_price", price), actual_size,
                     int(signal.get("magnitude", 0) * 100))
                )
            logger.info("[AUTO-LIVE] %s: %s @ %.0fc | $%.2f | %s",
                        signal["type"].upper(), side, price * 100, actual_size, question[:50])


def update_autonomous_positions():
    """Preis-Update und Stop-Loss fuer autonome Trades."""
    with db.get_connection() as conn:
        trades = conn.execute(
            "SELECT * FROM autonomous_trades WHERE status = 'open'"
        ).fetchall()

    for trade in trades:
        cid = trade["condition_id"]
        side = trade["side"]
        price = price_tracker.get_price(cid, side)
        if price is None:
            continue

        entry = trade["entry_price"] or 0
        if entry <= 0:
            continue
        side = trade["side"] or "YES"
        # NO-Positionen profitieren wenn Preis faellt
        # NO loses when NO-token price drops, YES loses when YES-token price drops
        if side.upper() == "NO":
            pnl_pct = (entry - price) / entry
        else:
            pnl_pct = (price - entry) / entry

        with db.get_connection() as conn:
            conn.execute(
                "UPDATE autonomous_trades SET current_price = ? WHERE id = ?",
                (price, trade["id"])
            )

        if pnl_pct <= -AUTONOMOUS_SL_PCT:
            size = trade["size"] or 0
            if side.upper() == "NO":
                pnl = round((entry - price) * (size / entry), 2) if entry > 0 else 0
            else:
                pnl = round((price - entry) * (size / entry), 2) if entry > 0 else 0
            with db.get_connection() as conn:
                conn.execute(
                    "UPDATE autonomous_trades SET status = 'closed', pnl_realized = ?, "
                    "closed_at = datetime('now','localtime') WHERE id = ?",
                    (pnl, trade["id"])
                )
            logger.info("[AUTO-SL] Closed: %s @ %.0fc -> %.0fc | P&L $%.2f",
                        trade["market_question"][:40] if trade["market_question"] else "?",
                        entry * 100, price * 100, pnl)


# --- AI vs Humans Divergence Signal (via PolymarketScan) ---
POLYSCAN_API = "https://gzydspfquuaudqeztorw.supabase.co/functions/v1/agent-api"
DIVERGENCE_THRESHOLD = 0.10  # 10%+ difference between AI and market odds
DIVERGENCE_MIN_VOLUME = 50000  # Min $50K volume for reliability


def scan_ai_divergence_signals():
    """Find markets where AI consensus strongly disagrees with market odds."""
    if _get_autonomous_exposure() >= _get_autonomous_budget():
        return

    try:
        r = requests.get(POLYSCAN_API, params={
            "action": "ai-vs-humans", "limit": 20, "agent_id": "maryyo-copybot"
        }, timeout=15)
        if not r.ok:
            return
        markets = r.json().get("data", [])
        if not markets:
            return

        for m in markets:
            divergence = abs(float(m.get("divergence", 0) or 0))
            volume = float(m.get("volume_usd", 0) or 0)
            ai_prob = float(m.get("ai_probability", 0) or 0)
            market_prob = float(m.get("market_probability", 0) or 0)

            if divergence < DIVERGENCE_THRESHOLD or volume < DIVERGENCE_MIN_VOLUME:
                continue

            # AI thinks YES is more likely than market
            if ai_prob > market_prob + DIVERGENCE_THRESHOLD:
                side = "YES"
                price = market_prob
            elif ai_prob < market_prob - DIVERGENCE_THRESHOLD:
                side = "NO"
                price = 1.0 - market_prob
            else:
                continue

            if price < MOMENTUM_MIN_PRICE or price > MOMENTUM_MAX_PRICE:
                continue

            cid = m.get("condition_id") or m.get("market_id", "")
            if not cid:
                continue

            signal = {
                "type": "ai_divergence",
                "condition_id": cid,
                "side": side,
                "price": price,
                "magnitude": divergence,
            }
            _execute_signal(signal)
            logger.info("[AUTO-AI] Divergence signal: %s %s @ %.0fc | AI=%.0f%% Market=%.0f%% | %s",
                        side, m.get("question", "")[:40], price * 100,
                        ai_prob * 100, market_prob * 100, m.get("question", "")[:40])

    except Exception as e:
        logger.debug("[AUTO-AI] Divergence scan error: %s", e)
