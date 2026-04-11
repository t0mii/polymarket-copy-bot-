"""
Trade Scorer — bewertet jeden Trade vor Ausfuehrung mit Score 0-100.
Scores unter dem Schwellenwert werden geblockt.
Score-Gewichte und Schwellenwerte werden von der Brain Engine optimiert.
"""
import logging
import json
import os
from database import db

logger = logging.getLogger(__name__)

_WEIGHTS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scorer_weights.json")

DEFAULT_WEIGHTS = {
    "trader_edge": 0.30,
    "category_wr": 0.20,
    "price_signal": 0.15,
    "conviction": 0.15,
    "market_quality": 0.10,
    "correlation": 0.10,
}

DEFAULT_THRESHOLDS = {
    "block": 40,
    "queue": 60,
    "boost": 80,
}


def _load_weights() -> tuple:
    try:
        if os.path.exists(_WEIGHTS_PATH):
            with open(_WEIGHTS_PATH) as f:
                data = json.load(f)
                return data.get("weights", DEFAULT_WEIGHTS), data.get("thresholds", DEFAULT_THRESHOLDS)
    except Exception:
        pass
    return DEFAULT_WEIGHTS.copy(), DEFAULT_THRESHOLDS.copy()


def save_weights(weights: dict, thresholds: dict):
    try:
        tmp = _WEIGHTS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"weights": weights, "thresholds": thresholds}, f, indent=2)
        os.replace(tmp, _WEIGHTS_PATH)
        logger.info("[SCORER] Weights updated: %s", weights)
    except Exception as e:
        logger.warning("[SCORER] Failed to save weights: %s", e)


def _score_trader_edge(trader_name: str) -> int:
    stats = db.get_trader_rolling_pnl(trader_name, 7)
    cnt = stats.get("cnt", 0) or 0
    if cnt < 3:
        return 50
    wins = stats.get("wins", 0) or 0
    wr = wins / cnt * 100 if cnt > 0 else 50
    pnl = stats.get("total_pnl", 0) or 0
    wr_score = max(0, min(100, (wr - 40) * 5))
    if pnl > 5:
        pnl_mod = 10
    elif pnl > 0:
        pnl_mod = 5
    elif pnl > -5:
        pnl_mod = 0
    elif pnl > -10:
        pnl_mod = -10
    else:
        pnl_mod = -20
    return max(0, min(100, int(wr_score + pnl_mod)))


def _score_category_wr(trader_name: str, category: str) -> int:
    if not category:
        return 50
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt, "
            "SUM(CASE WHEN pnl_realized > 0 THEN 1 ELSE 0 END) as wins "
            "FROM copy_trades WHERE wallet_username = ? AND category = ? AND status = 'closed'",
            (trader_name, category)
        ).fetchone()
    cnt = row["cnt"] or 0
    if cnt < 3:
        return 50
    wr = (row["wins"] or 0) / cnt * 100
    return max(0, min(100, int((wr - 35) * 100 / 30)))


def _score_price_signal(entry_price: float) -> int:
    if entry_price <= 0 or entry_price >= 1:
        return 0
    if 0.30 <= entry_price <= 0.65:
        return 100
    elif 0.25 <= entry_price < 0.30 or 0.65 < entry_price <= 0.75:
        return 70
    elif 0.20 <= entry_price < 0.25 or 0.75 < entry_price <= 0.85:
        return 40
    else:
        return 15


def _score_conviction(trader_size_usd: float, trader_name: str) -> int:
    import config
    avg_map = {}
    for entry in config.AVG_TRADER_SIZE_MAP.split(","):
        entry = entry.strip()
        if ":" in entry:
            parts = entry.split(":", 1)
            try:
                avg_map[parts[0].strip().lower()] = float(parts[1].strip())
            except ValueError:
                pass
    avg = avg_map.get(trader_name.lower(), config.DEFAULT_AVG_TRADER_SIZE)
    if avg <= 0:
        return 50
    ratio = trader_size_usd / avg
    if ratio >= 2.0:
        return 100
    elif ratio >= 1.5:
        return 80
    elif ratio >= 1.0:
        return 50
    elif ratio >= 0.5:
        return 30
    else:
        return 15


def _score_market_quality(spread: float, hours_until_event: float) -> int:
    if spread <= 0.02:
        spread_score = 50
    elif spread <= 0.03:
        spread_score = 35
    elif spread <= 0.05:
        spread_score = 15
    else:
        spread_score = 0
    if hours_until_event <= 0:
        time_score = 25
    elif hours_until_event < 1:
        time_score = 50
    elif hours_until_event < 24:
        time_score = 40
    elif hours_until_event < 72:
        time_score = 30
    else:
        time_score = 15
    return spread_score + time_score


def _score_correlation(condition_id: str, event_slug: str, category: str) -> int:
    open_trades = db.get_open_copy_trades()
    same_market = sum(1 for t in open_trades if t["condition_id"] == condition_id)
    if same_market > 0:
        return 0
    same_event = 0
    if event_slug:
        same_event = sum(1 for t in open_trades if (t.get("event_slug") or "") == event_slug)
    same_cat = 0
    if category:
        same_cat = sum(1 for t in open_trades if (t.get("category") or "") == category)
    if same_event >= 3:
        return 0
    elif same_event == 2:
        return 30
    elif same_event == 1:
        return 60
    if same_cat >= 5:
        return 40
    elif same_cat >= 3:
        return 70
    return 100


def score(trader_name: str, condition_id: str, side: str, entry_price: float,
          market_question: str, category: str, event_slug: str = "",
          trader_size_usd: float = 0, spread: float = 0.03,
          hours_until_event: float = 12) -> dict:
    weights, thresholds = _load_weights()
    components = {
        "trader_edge": _score_trader_edge(trader_name),
        "category_wr": _score_category_wr(trader_name, category),
        "price_signal": _score_price_signal(entry_price),
        "conviction": _score_conviction(trader_size_usd, trader_name),
        "market_quality": _score_market_quality(spread, hours_until_event),
        "correlation": _score_correlation(condition_id, event_slug, category),
    }
    total = 0
    for key, raw_score in components.items():
        total += raw_score * weights.get(key, 0)
    total = int(round(total))
    if total < thresholds["block"]:
        action = "BLOCK"
    elif total < thresholds["queue"]:
        action = "QUEUE"
    elif total < thresholds["boost"]:
        action = "EXECUTE"
    else:
        action = "BOOST"
    worst = min(components, key=components.get)
    best = max(components, key=components.get)
    reason = "score=%d (%s=%d best, %s=%d worst)" % (
        total, best, components[best], worst, components[worst])
    try:
        db.log_trade_score(
            condition_id=condition_id, trader_name=trader_name, side=side,
            entry_price=entry_price, market_question=market_question,
            score_total=total, components=components, action=action
        )
    except Exception as _dbe:
        logger.warning("[SCORER] DB log failed: %s", _dbe)
    logger.info("[SCORE] %s %s: %d -> %s | %s", trader_name, market_question[:30], total, action, reason)
    return {"score": total, "action": action, "components": components, "reason": reason}
