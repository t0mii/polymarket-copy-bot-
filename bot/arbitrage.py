"""
Complete-Set Arbitrage + Logische Arbitrage.
1. Complete-Set: YES + NO < $1.00 -> beide kaufen = risikoloser Profit
2. Logische Arb: Verbundene Maerkte mit widersprüchlichen Preisen
"""
import logging
import time
import requests

import config
from database import db
from bot.order_executor import buy_shares, get_wallet_balance

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Minimum edge nach Fees (2% fee = 200bps)
MIN_COMPLETE_SET_EDGE = 0.03  # 3 Cent minimum edge
MIN_LOGIC_ARB_EDGE = 0.04     # 5% minimum fuer logische Arb
MAX_ARB_SIZE = 10.0            # Max $10 pro Arb-Trade
SCAN_LIMIT = 50                # Maerkte pro Scan


def scan_complete_set_arb():
    """Scanne aktive Maerkte fuer YES+NO < $1 Opportunities."""
    try:
        # Hole aktive Maerkte
        resp = requests.get(GAMMA_API + "/markets",
                           params={"closed": "false", "limit": SCAN_LIMIT,
                                   "order": "volume24hr", "ascending": "false"},
                           timeout=10)
        if not resp.ok:
            return
        markets = resp.json()
    except Exception as e:
        logger.debug("[ARB] Market fetch failed: %s", e)
        return

    opportunities = []
    for market in markets:
        cid = market.get("conditionId", "")
        if not cid:
            continue

        # Hole Orderbook fuer YES und NO Preise
        try:
            tokens = market.get("clobTokenIds", [])
            if isinstance(tokens, str):
                import json
                tokens = json.loads(tokens)
            if len(tokens) < 2:
                continue

            outcomes = market.get("outcomes", [])
            if isinstance(outcomes, str):
                import json
                outcomes = json.loads(outcomes)

            # Best ask fuer YES und NO
            yes_price = None
            no_price = None

            for i, token_id in enumerate(tokens):
                try:
                    book_resp = requests.get(CLOB_API + "/book",
                                            params={"token_id": token_id},
                                            timeout=5)
                    if not book_resp.ok:
                        continue
                    book = book_resp.json()
                    asks = book.get("asks", [])
                    if asks:
                        best_ask = min(float(a["price"]) for a in asks)
                        if i == 0:
                            yes_price = best_ask
                        else:
                            no_price = best_ask
                except Exception:
                    continue

            if yes_price and no_price:
                total = yes_price + no_price
                edge = 1.0 - total

                if edge >= MIN_COMPLETE_SET_EDGE:
                    opportunities.append({
                        "condition_id": cid,
                        "question": market.get("question", "")[:60],
                        "yes_price": yes_price,
                        "no_price": no_price,
                        "total": total,
                        "edge": edge,
                        "tokens": tokens,
                        "outcomes": outcomes,
                    })

        except Exception:
            continue

        # Rate limiting
        time.sleep(0.2)

    if not opportunities:
        return

    # Sort by edge, take best
    opportunities.sort(key=lambda x: x["edge"], reverse=True)

    for opp in opportunities[:3]:  # Max 3 Arb-Trades pro Scan
        logger.info("[ARB] Complete-Set found: %s | YES=%.0fc NO=%.0fc | Edge=%.1f%%",
                    opp["question"], opp["yes_price"]*100, opp["no_price"]*100, opp["edge"]*100)

        # Execute: buy both sides
        balance = get_wallet_balance()
        size = min(MAX_ARB_SIZE, balance * 0.05)  # Max 5% vom Wallet

        if size < config.MIN_TRADE_SIZE * 2:
            continue

        half = round(size / 2, 2)

        if config.LIVE_MODE:
            yes_outcome = opp["outcomes"][0] if opp["outcomes"] else "Yes"
            no_outcome = opp["outcomes"][1] if len(opp["outcomes"]) > 1 else "No"

            resp1 = buy_shares(opp["condition_id"], yes_outcome, half, opp["yes_price"])
            if not resp1:
                logger.warning("[ARB] First leg failed, skipping: %s", opp["question"])
                continue
            resp2 = buy_shares(opp["condition_id"], no_outcome, half, opp["no_price"])

            if resp1 and resp2:
                profit = round(size * opp["edge"], 2)
                logger.info("[ARB] EXECUTED: %s | Invested=$%.2f | Guaranteed profit=$%.2f",
                            opp["question"], size, profit)
                db.log_activity("arbitrage", "ARB",
                                "Complete-Set Arb: %s" % opp["question"],
                                "Edge=%.1f%% Profit=$%.2f" % (opp["edge"]*100, profit),
                                profit)
            else:
                # UNWIND: sell the first leg back
                logger.warning("[ARB] Second leg failed — unwinding first leg: %s", opp["question"])
                try:
                    from bot.order_executor import sell_shares as _sell
                    _sell(opp["condition_id"], yes_outcome, opp["yes_price"])
                except Exception:
                    logger.error("[ARB] UNWIND FAILED — manual intervention needed: %s", opp["question"])


def scan_logic_arb():
    """Scanne verbundene Maerkte fuer logische Widersprueche."""
    try:
        # Hole Events (Groups of related markets)
        resp = requests.get(GAMMA_API + "/events",
                           params={"closed": "false", "limit": 30,
                                   "order": "volume24hr", "ascending": "false"},
                           timeout=10)
        if not resp.ok:
            return
        events = resp.json()
    except Exception as e:
        logger.debug("[LOGIC-ARB] Event fetch failed: %s", e)
        return

    for event in events:
        markets = event.get("markets", [])
        if len(markets) < 2 or len(markets) > 6:
            continue

        event_title = event.get("title", "")[:50]

        # Check: Summe aller YES-Preise in einem Multi-Outcome Event
        # sollte ~100% sein. Wenn < 95% oder > 105% = Opportunity
        total_yes = 0
        market_prices = []

        for m in markets:
            # Get current price
            price = 0
            try:
                tokens = m.get("clobTokenIds", [])
                if isinstance(tokens, str):
                    import json
                    tokens = json.loads(tokens)
                if tokens:
                    # Quick price check via CLOB
                    pr = requests.get(CLOB_API + "/price",
                                     params={"token_id": tokens[0], "side": "buy"},
                                     timeout=3)
                    if pr.ok:
                        price = float(pr.json().get("price", 0))
            except Exception:
                continue

            if price > 0:
                total_yes += price
                market_prices.append({
                    "question": m.get("question", "")[:40],
                    "price": price,
                    "condition_id": m.get("conditionId", ""),
                })

            time.sleep(0.1)

        if len(market_prices) < 2:
            continue

        # Multi-outcome: total should be ~1.0
        # If total > 1.0 + edge: sell overpriced / buy underpriced
        # If total < 1.0 - edge: buy all = guaranteed profit
        if total_yes < (1.0 - MIN_LOGIC_ARB_EDGE) and len(market_prices) >= 2:
            edge = 1.0 - total_yes
            logger.info("[LOGIC-ARB] Found: %s | Total=%.0f%% | Edge=%.1f%% | %d markets",
                        event_title, total_yes*100, edge*100, len(market_prices))
            for mp in market_prices:
                logger.info("[LOGIC-ARB]   %s @ %.0fc", mp["question"], mp["price"]*100)

            # Log but dont auto-trade multi-leg arbs (too risky without proper execution)
            db.log_activity("logic_arb", "ARB",
                            "Logic Arb found: %s" % event_title,
                            "Total=%.0f%% Edge=%.1f%%" % (total_yes*100, edge*100))
