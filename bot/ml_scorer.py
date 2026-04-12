"""
ML Scorer — lernt aus historischen Trades welche gewinnen/verlieren.
Trainiert alle 6h automatisch. RandomForest auf echten Trade-Daten.
"""
import logging
import os
import pickle
import time
from datetime import datetime

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split

from database import db

logger = logging.getLogger(__name__)

MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ml_model.pkl")
MIN_TRAINING_SAMPLES = 50
CATEGORY_MAP = {"cs": 1, "lol": 2, "valorant": 3, "dota": 4, "nhl": 5, "nba": 6, "nfl": 7,
                "mlb": 8, "tennis": 9, "soccer": 10, "cricket": 11, "geopolitics": 12, "politics": 13}

_model = None
_model_loaded = False


def _get_features(trade: dict) -> list:
    """Extract feature vector from a trade dict."""
    entry = trade.get("actual_entry_price") or trade.get("entry_price") or 0.5
    cat = CATEGORY_MAP.get((trade.get("category") or "").lower(), 0)
    side = 1 if (trade.get("side") or "YES").upper() == "YES" else 0
    size = trade.get("actual_size") or trade.get("size") or 1.0
    fee = trade.get("fee_bps") or 0

    # Time features
    hour = 12
    dow = 3
    try:
        created = trade.get("created_at") or ""
        if created:
            dt = datetime.strptime(created[:19], "%Y-%m-%d %H:%M:%S")
            hour = dt.hour
            dow = dt.weekday()
    except Exception:
        pass

    return [entry, cat, side, size, fee, hour, dow]


def _build_training_data():
    """Build training set from BOTH copy_trades (real outcomes) AND
    blocked_trades (would_have_won from outcome tracker).

    This gives the ML model 6x more training data than copy_trades alone,
    and lets it learn from trades the filters blocked too.

    Returns (X, y, copy_count, blocked_count).
    """
    with db.get_connection() as conn:
        copy_rows = conn.execute(
            "SELECT actual_entry_price, entry_price, category, side, "
            "actual_size, size, fee_bps, created_at, pnl_realized "
            "FROM copy_trades WHERE status = 'closed' AND pnl_realized IS NOT NULL"
        ).fetchall()
        blocked_rows = conn.execute(
            "SELECT trader_price, category, side, created_at, would_have_won "
            "FROM blocked_trades WHERE would_have_won IS NOT NULL"
        ).fetchall()

    X, y = [], []

    for r in copy_rows:
        d = dict(r)
        features = _get_features(d)
        label = 1 if (d.get("pnl_realized") or 0) > 0 else 0
        X.append(features)
        y.append(label)

    for r in blocked_rows:
        # Map blocked_trades schema to the dict shape _get_features expects
        d = {
            "entry_price": r["trader_price"] or 0.5,
            "category": r["category"] or "",
            "side": r["side"] or "YES",
            "created_at": r["created_at"] or "",
            "size": 0,        # we did not execute, no real size
            "fee_bps": 0,     # we did not execute, no fee
        }
        features = _get_features(d)
        label = int(r["would_have_won"])
        X.append(features)
        y.append(label)

    return X, y, len(copy_rows), len(blocked_rows)


def train_model():
    """Train ML model on closed copy_trades + outcome-checked blocked_trades.
    Called every 6h.
    """
    global _model, _model_loaded

    X, y, copy_count, blocked_count = _build_training_data()
    total = copy_count + blocked_count

    if total < MIN_TRAINING_SAMPLES:
        logger.info("[ML] Not enough data (%d/%d), skipping training", total, MIN_TRAINING_SAMPLES)
        return

    X = np.array(X)
    y = np.array(y)

    if len(set(y.tolist())) < 2:
        logger.warning("[ML] Only one class in training data — skipping")
        return

    # Train/test split
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    model = RandomForestClassifier(n_estimators=100, max_depth=6, min_samples_leaf=5, random_state=42)
    model.fit(X_train, y_train)

    train_acc = model.score(X_train, y_train)
    test_acc = model.score(X_test, y_test)

    # Feature importance
    feature_names = ["entry_price", "category", "side", "size", "fee_bps", "hour", "day_of_week"]
    importances = sorted(zip(feature_names, model.feature_importances_), key=lambda x: -x[1])

    logger.info("[ML] Trained on %d samples (%d copy + %d blocked) | Train: %.1f%% | Test: %.1f%%",
                total, copy_count, blocked_count, train_acc * 100, test_acc * 100)
    logger.info("[ML] Top features: %s",
                ", ".join("%s=%.0f%%" % (n, v * 100) for n, v in importances[:4]))

    # Save model
    try:
        tmp = MODEL_PATH + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(model, f)
        os.replace(tmp, MODEL_PATH)
        _model = model
        _model_loaded = True
        logger.info("[ML] Model saved to %s", MODEL_PATH)
    except Exception as e:
        logger.warning("[ML] Failed to save model: %s", e)

    # Log to DB
    try:
        import json
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO ml_training_log (samples_count, accuracy, feature_importance, model_path) "
                "VALUES (?, ?, ?, ?)",
                (total, round(test_acc, 4),
                 json.dumps(dict(importances)), MODEL_PATH)
            )
    except Exception:
        pass


def _load_model():
    """Load model from disk if not loaded yet."""
    global _model, _model_loaded
    if _model_loaded:
        return _model is not None
    try:
        if os.path.exists(MODEL_PATH):
            with open(MODEL_PATH, "rb") as f:
                _model = pickle.load(f)
            _model_loaded = True
            return True
    except Exception as e:
        logger.warning("[ML] Failed to load model: %s", e)
    _model_loaded = True
    return False


def predict(trade_data: dict) -> float:
    """Predict win probability for a trade. Returns 0.0-1.0 or -1 if no model."""
    if not _load_model():
        return -1

    try:
        features = np.array([_get_features(trade_data)])
        proba = _model.predict_proba(features)[0]
        # proba[1] = probability of winning
        win_prob = proba[1] if len(proba) > 1 else 0.5
        return round(float(win_prob), 3)
    except Exception as e:
        logger.debug("[ML] Prediction error: %s", e)
        return -1
