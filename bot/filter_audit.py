"""
Filter Precision Audit — per filter reason measure whether the bot's block
decisions are correct or are throwing away real winners.

For each `blocked_trades.block_reason` bucket:
  1. Build feature vectors for every verified row (resolved market outcome)
  2. Run the ml_block model to get a win-probability per row
  3. Among rows where the model is confident (proba >= CONFIDENCE), compute
     precision = (predicted_win AND actually_would_have_won) / predicted_win
  4. Classify each bucket:
       precision >= LOOSEN_THRESHOLD -> filter blocks too many winners
       precision <= KEEP_THRESHOLD   -> filter correctly blocks losers
       in between                    -> REVIEW manually

Output feeds a Brain dashboard panel so the user can decide which filters
to loosen. No auto-tuning — per piff-philosophy, the bot never changes its
own filter thresholds without explicit consent.
"""
import logging
from collections import defaultdict

import numpy as np

from bot.ml_scorer import _build_block_training_data, _load_block_model

logger = logging.getLogger(__name__)

LOOSEN_THRESHOLD = 0.70
KEEP_THRESHOLD = 0.30
CONFIDENCE = 0.70
MIN_SAMPLES = 100


def compute_filter_precision(min_samples: int = MIN_SAMPLES,
                             confidence: float = CONFIDENCE) -> dict:
    """Return {rows: [...], meta: {...}} where rows is one dict per
    block_reason bucket.

    Each row: reason, n, actual_win_rate, confident_n, confident_wins,
    precision_at_conf, recommendation. Buckets with n < min_samples get
    recommendation='INSUFFICIENT'. Buckets with no confident predictions
    get 'NO_CONFIDENT_PREDICTIONS'.

    Sorted so the most actionable entries (high precision = most likely
    over-blocking) come first.
    """
    if not _load_block_model():
        return {"rows": [], "meta": {"error": "ml_block model not trained yet",
                                     "total_rows": 0}}

    X, y, _, reasons = _build_block_training_data(verified_only=False)
    if not X:
        return {"rows": [], "meta": {"error": "no verified blocked_trades",
                                     "total_rows": 0}}

    import bot.ml_scorer as _ms
    model = _ms._model_block
    if model is None:
        return {"rows": [], "meta": {"error": "ml_block model failed to load",
                                     "total_rows": 0}}

    # CRITICAL: only audit the test slice, NOT the full dataset. Running
    # predict_proba on rows the model was trained on gives artificial
    # near-100% precision (the model memorized the labels). Matches the
    # 80/20 chronological split that train_block_model uses so the slice
    # here is the exact rows the model has never seen.
    split_idx = int(len(X) * 0.8)
    X_test = X[split_idx:]
    y_test = y[split_idx:]
    reasons_test = reasons[split_idx:]

    if not X_test:
        return {"rows": [], "meta": {"error": "no held-out test rows (dataset too small)",
                                     "total_rows": len(X)}}

    X_arr = np.array(X_test)
    try:
        probas = model.predict_proba(X_arr)[:, 1]
    except ValueError as e:
        return {"rows": [], "meta": {"error": "feature shape mismatch, retrain ml_block: %s" % e,
                                     "total_rows": len(X_test)}}
    except Exception as e:
        return {"rows": [], "meta": {"error": "predict_proba failed: %s" % e,
                                     "total_rows": len(X_test)}}

    y_arr = np.array(y_test)
    buckets = defaultdict(lambda: {"n": 0, "wins": 0, "conf_n": 0, "conf_wins": 0})
    for i, reason in enumerate(reasons_test):
        b = buckets[reason]
        b["n"] += 1
        if y_arr[i] == 1:
            b["wins"] += 1
        if probas[i] >= confidence:
            b["conf_n"] += 1
            if y_arr[i] == 1:
                b["conf_wins"] += 1

    out = []
    for reason, b in buckets.items():
        entry = {
            "reason": reason,
            "n": b["n"],
            "actual_win_rate": round(b["wins"] / b["n"] * 100.0, 1) if b["n"] else 0.0,
            "confident_n": b["conf_n"],
            "confident_wins": b["conf_wins"],
            "precision_at_conf": (
                round(b["conf_wins"] / b["conf_n"] * 100.0, 1) if b["conf_n"] else None
            ),
        }
        if b["n"] < min_samples:
            entry["recommendation"] = "INSUFFICIENT"
        elif entry["precision_at_conf"] is None:
            entry["recommendation"] = "NO_CONFIDENT_PREDICTIONS"
        elif entry["precision_at_conf"] / 100.0 >= LOOSEN_THRESHOLD:
            entry["recommendation"] = "LOOSEN"
        elif entry["precision_at_conf"] / 100.0 <= KEEP_THRESHOLD:
            entry["recommendation"] = "KEEP"
        else:
            entry["recommendation"] = "REVIEW"
        out.append(entry)

    def _sort_key(e):
        order = {"LOOSEN": 0, "REVIEW": 1, "KEEP": 2,
                 "NO_CONFIDENT_PREDICTIONS": 3, "INSUFFICIENT": 4}
        return (order.get(e["recommendation"], 9),
                -(e["precision_at_conf"] or 0),
                -e["n"])

    out.sort(key=_sort_key)

    # Pull ml_block model vitals for the header badge — so users can see
    # the audit tool's health at a glance (trust indicator for the rows).
    block_health = {}
    try:
        from bot.ml_scorer import get_model_health
        from database import db as _db
        h = get_model_health("ml_block")
        block_health = {
            "edge_pp": round(h.get("edge_vs_baseline", 0.0), 1),
            "trained_at": h.get("trained_at", ""),
        }
        with _db.get_connection() as conn:
            row = conn.execute(
                "SELECT samples_count FROM ml_training_log "
                "WHERE COALESCE(model_name,'ml_copy')='ml_block' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            block_health["samples"] = int(row["samples_count"]) if row and row["samples_count"] else 0
    except Exception:
        pass

    return {
        "rows": out,
        "meta": {
            "total_verified_rows": len(X),
            "test_rows": len(X_test),
            "buckets": len(out),
            "confidence_threshold": confidence,
            "loosen_threshold": LOOSEN_THRESHOLD,
            "keep_threshold": KEEP_THRESHOLD,
            "min_samples": min_samples,
            "block_model": block_health,
        },
    }
