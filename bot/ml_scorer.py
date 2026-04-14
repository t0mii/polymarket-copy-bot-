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

from database import db

logger = logging.getLogger(__name__)

MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ml_model.pkl")
MIN_TRAINING_SAMPLES = 50
CATEGORY_MAP = {"cs": 1, "lol": 2, "valorant": 3, "dota": 4, "nhl": 5, "nba": 6, "nfl": 7,
                "mlb": 8, "tennis": 9, "soccer": 10, "cricket": 11, "geopolitics": 12, "politics": 13}

_model = None
_model_loaded = False


def _get_features(trade: dict) -> list:
    """Extract feature vector from a trade dict.

    REMOVED size + fee_bps as features: they were data-source markers
    rather than predictive signal. copy_trades have size>0 and fee_bps
    set, blocked_trades have size=0 and fee_bps=0. The model trivially
    learned `size==0 → predict by category` (blocked_trades have 28%
    win rate). This inflated test accuracy to 92% while teaching nothing
    about WHY trades win or lose. With these features removed, accuracy
    will be lower but actually meaningful.
    """
    entry = trade.get("actual_entry_price") or trade.get("entry_price") or 0.5
    cat = CATEGORY_MAP.get((trade.get("category") or "").lower(), 0)
    side = 1 if (trade.get("side") or "YES").upper() == "YES" else 0

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

    return [entry, cat, side, hour, dow]


def _build_training_data():
    """Build training set from BOTH copy_trades (real outcomes) AND
    blocked_trades (would_have_won from outcome tracker).

    This gives the ML model 6x more training data than copy_trades alone,
    and lets it learn from trades the filters blocked too.

    The two sources are MERGED chronologically by created_at so the
    downstream time-split in train_model() produces a time-ordered
    train/test slice (previously they were concatenated, putting all
    copy_rows before all blocked_rows regardless of actual date).

    Returns (X, y, is_copy, copy_count, blocked_count):
      - X: feature matrix, sorted by created_at ASC
      - y: label vector aligned with X
      - is_copy: bool vector, True for rows sourced from copy_trades
      - copy_count, blocked_count: total counts
    """
    with db.get_connection() as conn:
        copy_rows = conn.execute(
            "SELECT actual_entry_price, entry_price, category, side, "
            "actual_size, size, fee_bps, created_at, pnl_realized "
            "FROM copy_trades WHERE status = 'closed' AND pnl_realized IS NOT NULL "
            "ORDER BY created_at ASC"
        ).fetchall()
        blocked_rows = conn.execute(
            "SELECT trader_price, category, side, created_at, would_have_won "
            "FROM blocked_trades WHERE would_have_won IS NOT NULL "
            "ORDER BY created_at ASC"
        ).fetchall()

    # Merge chronologically. Each entry is (created_at, is_copy, features, label).
    merged = []

    for r in copy_rows:
        d = dict(r)
        features = _get_features(d)
        label = 1 if (d.get("pnl_realized") or 0) > 0 else 0
        merged.append((d.get("created_at") or "", True, features, label))

    for r in blocked_rows:
        # Map blocked_trades schema to the dict shape _get_features expects.
        # size/fee_bps removed from features (was leakage marker).
        d = {
            "entry_price": r["trader_price"] or 0.5,
            "category": r["category"] or "",
            "side": r["side"] or "YES",
            "created_at": r["created_at"] or "",
        }
        features = _get_features(d)
        label = int(r["would_have_won"])
        merged.append((d["created_at"], False, features, label))

    # Sort by created_at (ISO timestamp strings compare chronologically)
    merged.sort(key=lambda t: t[0])

    X = [m[2] for m in merged]
    y = [m[3] for m in merged]
    is_copy = [m[1] for m in merged]

    return X, y, is_copy, len(copy_rows), len(blocked_rows)


def train_model():
    """Train ML model on closed copy_trades + outcome-checked blocked_trades.
    Called every 6h.
    """
    global _model, _model_loaded

    X, y, is_copy, copy_count, blocked_count = _build_training_data()
    total = copy_count + blocked_count

    if total < MIN_TRAINING_SAMPLES:
        logger.info("[ML] Not enough data (%d/%d), skipping training", total, MIN_TRAINING_SAMPLES)
        return

    X = np.array(X)
    y = np.array(y)
    is_copy_arr = np.array(is_copy, dtype=bool)

    if len(set(y.tolist())) < 2:
        logger.warning("[ML] Only one class in training data — skipping")
        return

    # Class balance — without this it's impossible to tell whether a high
    # accuracy number is "real" or just a consequence of predicting the
    # majority class.
    n_win = int((y == 1).sum())
    n_loss = int((y == 0).sum())
    win_frac = n_win / len(y) if len(y) > 0 else 0
    logger.info("[ML] Class balance: %d wins / %d losses (%.1f%% win rate)",
                n_win, n_loss, win_frac * 100)

    # Time-ordered split. _build_training_data returned rows merged and
    # sorted by created_at ASC across BOTH sources, so slicing by index
    # is a true chronological split.
    split_idx = int(len(X) * 0.8)
    X_train, X_test = X[:split_idx], X[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]
    is_copy_test = is_copy_arr[split_idx:]

    if len(set(y_train.tolist())) < 2 or len(set(y_test.tolist())) < 2:
        logger.warning("[ML] Time-split produced single-class train/test — skipping")
        return

    model = RandomForestClassifier(n_estimators=100, max_depth=6, min_samples_leaf=5, random_state=42)
    model.fit(X_train, y_train)

    train_acc = model.score(X_train, y_train)
    test_acc = model.score(X_test, y_test)

    # Majority-class baseline — what you get for free by always predicting
    # the more frequent class in the training set.
    majority = 1 if (y_train == 1).sum() >= (y_train == 0).sum() else 0
    baseline_acc = float((y_test == majority).sum()) / len(y_test) if len(y_test) > 0 else 0

    # COPY-ONLY test accuracy (real trades only, excluding the blocked
    # subset that often has trivial extreme-price → extreme-outcome
    # correlation and inflates overall accuracy). This is the number that
    # actually matters for live trading decisions.
    copy_test_acc = None  # populated below if we have ≥5 copy samples
    copy_test_mask = is_copy_test
    n_copy_test = int(copy_test_mask.sum())
    if n_copy_test >= 5:
        y_test_copy = y_test[copy_test_mask]
        X_test_copy = X_test[copy_test_mask]
        copy_test_acc = model.score(X_test_copy, y_test_copy)
        # Confusion matrix on copy-only subset
        preds = model.predict(X_test_copy)
        tp = int(((preds == 1) & (y_test_copy == 1)).sum())
        fp = int(((preds == 1) & (y_test_copy == 0)).sum())
        tn = int(((preds == 0) & (y_test_copy == 0)).sum())
        fn = int(((preds == 0) & (y_test_copy == 1)).sum())
        # Precision/recall for "predicted win" class
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        # Copy-only baseline (majority class within the copy subset)
        n_copy_win = int((y_test_copy == 1).sum())
        n_copy_loss = int((y_test_copy == 0).sum())
        copy_majority = 1 if n_copy_win >= n_copy_loss else 0
        copy_baseline = float((y_test_copy == copy_majority).sum()) / len(y_test_copy)
        logger.info("[ML] COPY-ONLY test subset (n=%d, %d win / %d loss): acc=%.1f%% baseline=%.1f%% | TP=%d FP=%d TN=%d FN=%d | prec=%.2f rec=%.2f",
                    n_copy_test, n_copy_win, n_copy_loss,
                    copy_test_acc * 100, copy_baseline * 100,
                    tp, fp, tn, fn, precision, recall)
    else:
        logger.info("[ML] COPY-ONLY test subset too small (n=%d < 5) to compute meaningful diagnostics", n_copy_test)

    feature_names = ["entry_price", "category", "side", "hour", "day_of_week"]
    importances = sorted(zip(feature_names, model.feature_importances_), key=lambda x: -x[1])

    logger.info("[ML] Trained on %d samples (%d copy + %d blocked) | Train: %.1f%% | Test: %.1f%% | Baseline: %.1f%%",
                total, copy_count, blocked_count,
                train_acc * 100, test_acc * 100, baseline_acc * 100)
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

    # Log to DB — three accuracies + sample sizes + per-run baseline so the
    # brain dashboard can compare ML vs. majority-class without hardcoding 79.1.
    try:
        import json
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO ml_training_log "
                "(samples_count, accuracy, train_accuracy, copy_only_accuracy, "
                "baseline_accuracy, train_n, test_n, feature_importance, model_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (total,
                 round(test_acc, 4),
                 round(train_acc, 4),
                 round(copy_test_acc, 4) if copy_test_acc is not None else None,
                 round(baseline_acc, 4),
                 len(X_train),
                 len(X_test),
                 json.dumps(dict(importances)),
                 MODEL_PATH)
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
