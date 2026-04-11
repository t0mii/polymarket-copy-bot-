"""
ML Trade Scoring Engine — bewertet jeden Trade vor Ausfuehrung.
Gradient Boosted Tree trainiert auf historischen Copy-Trades.
Score 0-100: 70+ = voller Einsatz, 40-70 = halber Einsatz, <40 = skip.
"""
import logging
import os
import pickle
import time
from datetime import datetime, timedelta

import numpy as np

from database import db

logger = logging.getLogger(__name__)

MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "ml_model.pkl")
MIN_TRAINING_SAMPLES = 50

SCORE_FULL = 60
SCORE_HALF = 30


def _extract_features(td: dict) -> list:
    """Feature-Vektor aus Trade-Daten."""
    return [
        td.get("trader_winrate_7d", 50),
        td.get("trader_winrate_30d", 50),
        td.get("trader_pnl_7d", 0),
        td.get("category_winrate_30d", 50),
        td.get("category_pnl_30d", 0),
        td.get("entry_price", 0.5),
        td.get("conviction_ratio", 1.0),
        td.get("hour_of_day", 12),
        td.get("day_of_week", 3),
        td.get("spread", 0.03),
        td.get("trader_avg_pnl_30d", 0),
    ]


def _build_training_data() -> tuple:
    """Historische Trades in Features + Labels umwandeln."""
    from bot.copy_trader import _detect_category

    with db.get_connection() as conn:
        trades = conn.execute(
            "SELECT wallet_username, market_question, entry_price, size, "
            "pnl_realized, created_at, closed_at, condition_id "
            "FROM copy_trades WHERE status = 'closed' AND pnl_realized IS NOT NULL"
        ).fetchall()

    if len(trades) < MIN_TRAINING_SAMPLES:
        logger.warning("[ML] Nicht genug Daten: %d < %d", len(trades), MIN_TRAINING_SAMPLES)
        return None, None

    X, y = [], []
    for t in trades:
        trader = t["wallet_username"] or ""
        question = t["market_question"] or ""
        created = t["created_at"] or ""
        category = _detect_category(question) or "other"
        pnl = t["pnl_realized"] or 0

        hour, dow = 12, 3
        try:
            dt = datetime.strptime(created[:19], "%Y-%m-%d %H:%M:%S")
            hour = dt.hour
            dow = dt.weekday()
        except Exception:
            pass

        t_stats_7d = db.get_trader_rolling_pnl(trader, 7)
        t_stats_30d = db.get_trader_rolling_pnl(trader, 30)
        c_stats = db.get_category_rolling_pnl(category, 30)

        cnt_7d = max(t_stats_7d.get("cnt", 1) or 1, 1)
        cnt_30d = max(t_stats_30d.get("cnt", 1) or 1, 1)

        features = _extract_features({
            "trader_winrate_7d": round((t_stats_7d.get("wins", 0) or 0) / cnt_7d * 100, 1),
            "trader_winrate_30d": round((t_stats_30d.get("wins", 0) or 0) / cnt_30d * 100, 1),
            "trader_pnl_7d": t_stats_7d.get("total_pnl", 0) or 0,
            "category_winrate_30d": c_stats.get("winrate", 50) or 50,
            "category_pnl_30d": c_stats.get("total_pnl", 0) or 0,
            "entry_price": t["entry_price"] or 0.5,
            "conviction_ratio": 1.0,
            "hour_of_day": hour,
            "day_of_week": dow,
            "spread": 0.03,
            "trader_avg_pnl_30d": (t_stats_30d.get("total_pnl", 0) or 0) / cnt_30d,
        })

        X.append(features)
        y.append(1 if pnl > 0 else 0)

    X_arr, y_arr = np.array(X), np.array(y)
    # Check for at least 2 classes (sklearn needs both)
    if len(set(y_arr)) < 2:
        logger.warning("[ML] Only one class in training data, skipping")
        return None, None
    return X_arr, y_arr


def train_model():
    """Modell trainieren auf allen geschlossenen Trades."""
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.model_selection import cross_val_score

    X, y = _build_training_data()
    if X is None:
        return False

    model = GradientBoostingClassifier(
        n_estimators=100,
        max_depth=4,
        learning_rate=0.1,
        min_samples_leaf=5,
        random_state=42,
    )

    cv_folds = min(5, max(2, len(X) // 20))
    scores = cross_val_score(model, X, y, cv=cv_folds, scoring="accuracy")
    accuracy = round(scores.mean() * 100, 1)
    logger.info("[ML] Cross-val accuracy: %.1f%% (+/- %.1f%%)", accuracy, scores.std() * 100)

    model.fit(X, y)

    feature_names = ["wr_7d", "wr_30d", "pnl_7d", "cat_wr_30d", "cat_pnl_30d",
                     "entry_price", "conviction", "hour", "dow", "spread", "avg_pnl_30d"]
    importances = {n: round(float(v), 3) for n, v in zip(feature_names, model.feature_importances_)}
    logger.info("[ML] Feature importance: %s", importances)

    os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(model, f)

    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO ml_training_log (samples_count, accuracy, feature_importance, model_path) "
            "VALUES (?, ?, ?, ?)",
            (len(X), accuracy, str(importances), MODEL_PATH)
        )

    logger.info("[ML] Model trained: %d samples, %.1f%% accuracy", len(X), accuracy)
    return True


_model = None

def _load_model():
    global _model
    if _model is not None:
        return _model
    if os.path.exists(MODEL_PATH):
        try:
            with open(MODEL_PATH, "rb") as f:
                _model = pickle.load(f)
            logger.info("[ML] Model loaded from %s", MODEL_PATH)
        except Exception as e:
            logger.warning("[ML] Could not load model: %s", e)
    return _model


def score_trade(trade_data: dict) -> int:
    """Trade bewerten. Returns Score 0-100."""
    model = _load_model()
    if model is None:
        return 50

    features = np.array([_extract_features(trade_data)])
    try:
        prob = model.predict_proba(features)[0]
        return int(round(prob[1] * 100))
    except Exception as e:
        logger.debug("[ML] Score error: %s", e)
        return 50


def get_score_multiplier(score: int) -> float:
    """Score in Bet-Multiplier umwandeln."""
    if score >= SCORE_FULL:
        return 1.0
    elif score >= SCORE_HALF:
        return 0.5
    else:
        return 0.0
