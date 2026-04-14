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


def _current_category_blacklist_map() -> dict:
    """Parse CATEGORY_BLACKLIST_MAP from settings.env into {trader_lower: set(categories)}.
    Used by the audit to ignore historical blocks whose (trader, category)
    combo is no longer enforced, so LOOSEN recommendations reflect the
    CURRENT policy not accumulated history.
    """
    import os, re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "settings.env")
    try:
        with open(path) as f:
            content = f.read()
    except Exception:
        return {}
    m = re.search(r"^CATEGORY_BLACKLIST_MAP=([^\n#]*)", content, re.MULTILINE)
    if not m:
        return {}
    out = {}
    for entry in m.group(1).split(","):
        entry = entry.strip()
        if ":" in entry:
            t, cats = entry.split(":", 1)
            out[t.strip().lower()] = set(
                c.strip().lower() for c in cats.split("|") if c.strip()
            )
    return out


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

    X, y, _, reasons, metas = _build_block_training_data(verified_only=False, with_metas=True)
    if not X:
        return {"rows": [], "meta": {"error": "no verified blocked_trades",
                                     "total_rows": 0}}

    # Drop historical category_blacklist rows whose (trader, category) combo
    # is no longer in the current CATEGORY_BLACKLIST_MAP. After a manual
    # cleanup (e.g. removing sovereign2013:tennis because backfilled data
    # showed it was profitable), the old tennis-blocked rows stay in DB
    # forever and would keep pushing the LOOSEN recommendation even though
    # the rule has already been loosened. This filter collapses the audit
    # to only the rules that are STILL enforced today.
    current_bl = _current_category_blacklist_map()
    kept_X, kept_y, kept_reasons, stale_dropped = [], [], [], 0
    for i in range(len(X)):
        r = reasons[i]
        if r == "category_blacklist":
            meta = metas[i] if i < len(metas) else {}
            trader = (meta.get("trader") or "").strip().lower()
            cat = (meta.get("detected_category") or "").strip().lower()
            if trader and cat:
                if cat not in current_bl.get(trader, set()):
                    # This (trader, category) combo is no longer blacklisted —
                    # drop the historical row so the audit reflects current state.
                    stale_dropped += 1
                    continue
            # If trader or cat missing, keep the row (can't decide)
        kept_X.append(X[i])
        kept_y.append(y[i])
        kept_reasons.append(r)
    X, y, reasons = kept_X, kept_y, kept_reasons
    if stale_dropped:
        logger.info("[FILTER-AUDIT] dropped %d stale category_blacklist rows (no longer enforced)", stale_dropped)

    if not X:
        return {"rows": [], "meta": {"error": "no rows after stale filter",
                                     "total_rows": 0,
                                     "stale_dropped": stale_dropped}}

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
            "stale_dropped": stale_dropped,
        },
    }
