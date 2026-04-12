"""
AI News Trading — Claude analysiert aktive Maerkte und findet Mispriced Opportunities.
Nutzt die bereits integrierte Anthropic API.
Laeuft alle 2 Stunden, analysiert Top-Maerkte mit hohem Volumen.
"""
import logging
import json
import time
import requests

import config
from database import db
from bot.order_executor import buy_shares, get_wallet_balance

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
MAX_MARKETS_PER_SCAN = 10
MIN_EDGE_PCT = 15  # Nur traden wenn >15% Abweichung
MAX_AI_TRADE_SIZE = 5.0  # Max $5 pro AI-Trade
AI_BUDGET_PCT = 0.10  # Max 10% vom Portfolio fuer AI-Trades


def _call_claude(prompt, max_tokens=500):
    """Claude API aufrufen fuer Markt-Analyse."""
    api_key = config.ANTHROPIC_API_KEY
    if not api_key:
        return None

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if resp.ok:
            data = resp.json()
            return data.get("content", [{}])[0].get("text", "")
    except Exception as e:
        logger.debug("[AI-NEWS] Claude API error: %s", e)
    return None


def scan_ai_opportunities():
    """Top-Volume Maerkte analysieren und Mispricing finden."""
    if not config.ANTHROPIC_API_KEY:
        logger.debug("[AI-NEWS] No Anthropic API key, skipping")
        return

    # Budget check
    balance = get_wallet_balance()
    total_budget = balance * AI_BUDGET_PCT

    # Aktuelle AI-Trade Exposure
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(size), 0) as total FROM autonomous_trades "
            "WHERE status = 'open' AND signal_type = 'ai_news'"
        ).fetchone()
        current_exposure = row["total"] if row else 0

    if current_exposure >= total_budget:
        return

    # Hole Top-Volume Maerkte die bald enden (mehr Info = bessere Analyse)
    try:
        resp = requests.get(GAMMA_API + "/markets",
                           params={"closed": "false", "limit": MAX_MARKETS_PER_SCAN,
                                   "order": "volume24hr", "ascending": "false"},
                           timeout=10)
        if not resp.ok:
            return
        markets = resp.json()
    except Exception as e:
        logger.debug("[AI-NEWS] Market fetch failed: %s", e)
        return

    # Filter: nur Maerkte mit genug Volumen und klarer Frage
    candidates = []
    for m in markets:
        vol24 = float(m.get("volume24hr", 0) or 0)
        question = m.get("question", "")
        cid = m.get("conditionId", "")
        description = m.get("description", "")[:500]

        if vol24 < 10000 or not question or not cid:
            continue

        # Aktueller Preis
        outcomes = m.get("outcomes", [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)

        tokens = m.get("clobTokenIds", [])
        if isinstance(tokens, str):
            tokens = json.loads(tokens)

        prices = {}
        for i, tid in enumerate(tokens):
            try:
                pr = requests.get("https://clob.polymarket.com/price",
                                 params={"token_id": tid, "side": "buy"},
                                 timeout=3)
                if pr.ok:
                    name = outcomes[i] if i < len(outcomes) else "YES" if i == 0 else "NO"
                    prices[name] = float(pr.json().get("price", 0))
            except Exception:
                pass
            time.sleep(0.1)

        if prices:
            candidates.append({
                "question": question,
                "description": description,
                "condition_id": cid,
                "prices": prices,
                "outcomes": outcomes,
                "volume24h": vol24,
            })

    if not candidates:
        return

    # Batch-Analyse: Claude analysiert alle Kandidaten auf einmal
    market_summary = "\n".join([
        "%d. %s\n   Current prices: %s\n   24h Volume: $%.0f\n   Description: %s" % (
            i+1, c["question"],
            ", ".join("%s=%.0fc" % (k, v*100) for k, v in c["prices"].items()),
            c["volume24h"],
            c["description"][:200]
        )
        for i, c in enumerate(candidates[:5])
    ])

    prompt = """You are a prediction market analyst. Analyze these Polymarket markets and estimate the TRUE probability for each.

MARKETS:
%s

For each market, respond in this EXACT JSON format (no other text):
[
  {"market": 1, "true_prob": 0.65, "confidence": "high", "reasoning": "short reason"},
  {"market": 2, "true_prob": 0.30, "confidence": "medium", "reasoning": "short reason"}
]

Rules:
- true_prob = your estimate of the real probability (0.0 to 1.0)
- confidence = "high", "medium", or "low"
- Only include markets where you have medium or high confidence
- Be honest - if you dont know, set confidence to "low"
- Consider current date: April 2026""" % market_summary

    response = _call_claude(prompt, max_tokens=800)
    if not response:
        return

    # Parse AI response
    try:
        # Extract JSON from response
        start = response.find("[")
        end = response.rfind("]") + 1
        if start < 0 or end <= start:
            return
        analyses = json.loads(response[start:end])
    except Exception as e:
        logger.debug("[AI-NEWS] Parse error: %s", e)
        return

    # Find mispriced markets
    for analysis in analyses:
        idx = analysis.get("market", 1) - 1
        if idx < 0 or idx >= len(candidates):
            continue

        confidence = analysis.get("confidence", "low")
        if confidence == "low":
            continue

        true_prob = analysis.get("true_prob", 0)
        candidate = candidates[idx]
        reasoning = analysis.get("reasoning", "")

        # Check each outcome for mispricing
        for outcome, market_price in candidate["prices"].items():
            if market_price <= 0.05 or market_price >= 0.95:
                continue

            # Edge = difference between AI estimate and market price
            if outcome in ("Yes", "YES") or outcome == candidate["outcomes"][0] if candidate["outcomes"] else False:
                ai_price = true_prob
            else:
                ai_price = 1.0 - true_prob

            edge = ai_price - market_price
            edge_pct = abs(edge) * 100

            if edge_pct >= MIN_EDGE_PCT and edge > 0:
                # AI thinks this outcome is UNDERPRICED -> BUY
                size = min(MAX_AI_TRADE_SIZE, total_budget - current_exposure)
                if size < config.MIN_TRADE_SIZE:
                    continue

                logger.info("[AI-NEWS] Mispricing: %s | %s @ %.0fc (AI says %.0fc, edge=%.0f%%) | %s",
                            candidate["question"][:40], outcome,
                            market_price*100, ai_price*100, edge_pct, reasoning[:50])

                if config.LIVE_MODE:
                    resp = buy_shares(candidate["condition_id"], outcome, size, market_price)
                    if resp:
                        actual_size = resp.get("usdc_spent", size)
                        with db.get_connection() as conn:
                            conn.execute(
                                "INSERT INTO autonomous_trades (signal_type, condition_id, "
                                "market_question, side, entry_price, size, score, status) "
                                "VALUES ('ai_news', ?, ?, ?, ?, ?, ?, 'open')",
                                (candidate["condition_id"], candidate["question"],
                                 outcome, resp.get("effective_price", market_price),
                                 actual_size, int(edge_pct))
                            )
                        current_exposure += actual_size
                        logger.info("[AI-NEWS] BOUGHT: $%.2f %s @ %.0fc | AI edge=%.0f%%",
                                    actual_size, outcome, market_price*100, edge_pct)
                        db.log_activity("ai_trade", "AI",
                                        "AI News Trade: %s" % candidate["question"][:40],
                                        "%s @ %.0fc (edge %.0f%%)" % (outcome, market_price*100, edge_pct))

                break  # One trade per market
