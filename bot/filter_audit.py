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


def _verified_pnl_per_trader_category() -> dict:
    """Load all closed copy_trades with verified fills, group by
    (trader_lower, detected_category). Returns dict[(trader,cat)] -> {n, pnl}.

    Uses bot.ml_scorer._detect_category to keyword-detect from the
    market_question since copy_trades.category is empty for ~97% of rows.
    This is the GROUND TRUTH signal — real wallet-verified dollar outcomes
    from trades we actually executed in each (trader, category) pair.
    """
    from database import db as _db
    from bot.ml_scorer import _detect_category
    result = {}
    with _db.get_connection() as conn:
        for r in conn.execute(
            "SELECT wallet_username, market_question, actual_size, size, usdc_received "
            "FROM copy_trades "
            "WHERE status='closed' AND usdc_received IS NOT NULL"
        ).fetchall():
            trader = (r["wallet_username"] or "").strip().lower()
            cat = _detect_category(r["market_question"] or "")
            if not trader or not cat:
                continue
            cost = r["actual_size"] or r["size"] or 0
            if not cost or cost <= 0:
                continue
            pnl = (r["usdc_received"] or 0) - cost
            key = (trader, cat)
            b = result.setdefault(key, {"n": 0, "pnl": 0.0, "wins": 0})
            b["n"] += 1
            b["pnl"] += pnl
            if pnl > 0:
                b["wins"] += 1
    return result


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

    # Drop historical category_blacklist rows that would no longer fire
    # under the CURRENT CATEGORY_BLACKLIST_MAP. Robust text check: for each
    # row's (trader, market_question), ask "would ANY of this trader's
    # currently-enforced blacklist categories match the market text?". If
    # not, the row is stale and gets dropped. This handles the case where
    # the detector can't extract a category from the text (e.g. "Sarasota:
    # Yibing Wu vs Daniel Dutra" is tennis but no keyword matches) — we
    # fall back to checking every enforced category's keyword list against
    # the text directly.
    current_bl = _current_category_blacklist_map()
    # Build a trader -> list[keyword] lookup for enforced categories.
    from bot.ml_scorer import _CATEGORY_KEYWORDS
    cat_kw = {cat: kws for cat, kws in _CATEGORY_KEYWORDS}
    kept_X, kept_y, kept_reasons, kept_metas, stale_dropped = [], [], [], [], 0
    for i in range(len(X)):
        r = reasons[i]
        meta = metas[i] if i < len(metas) else {}
        if r == "category_blacklist":
            trader = (meta.get("trader") or "").strip().lower()
            enforced = current_bl.get(trader, set())
            if not enforced:
                # Trader has no blacklist at all → any historical row is stale
                stale_dropped += 1
                continue
            mq = (meta.get("market_question") or "").lower()
            still_enforced = False
            for cat in enforced:
                for kw in cat_kw.get(cat, []):
                    if kw in mq:
                        still_enforced = True
                        break
                if still_enforced:
                    break
            if not still_enforced:
                stale_dropped += 1
                continue
        kept_X.append(X[i])
        kept_y.append(y[i])
        kept_reasons.append(r)
        kept_metas.append(meta)
    X, y, reasons, metas = kept_X, kept_y, kept_reasons, kept_metas
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

    metas_test = metas[split_idx:]

    # Load verified PnL per (trader, category) from copy_trades once —
    # ground-truth dollar outcomes for each still-enforced combo. This
    # is the magnitude signal that ml_block's binary would_have_won
    # labels miss. For asymmetric Polymarket payoffs (small wins, big
    # losses) a 51% WR can still be a net loss, and this lookup tells
    # us the truth.
    verified_pnl = _verified_pnl_per_trader_category()

    y_arr = np.array(y_test)
    buckets = defaultdict(lambda: {"n": 0, "wins": 0, "conf_n": 0, "conf_wins": 0,
                                   "combos": set()})
    for i, reason in enumerate(reasons_test):
        b = buckets[reason]
        b["n"] += 1
        if y_arr[i] == 1:
            b["wins"] += 1
        if probas[i] >= confidence:
            b["conf_n"] += 1
            if y_arr[i] == 1:
                b["conf_wins"] += 1
        # Track which (trader, ENFORCED_cat) combo the row matches. We
        # deliberately DO NOT trust meta.detected_category here because the
        # detector has priority ordering (e.g. "nba" is checked before "nhl"
        # so a "Raptors vs Lightning" row detects as "nba" even though it
        # also matches NHL's "lightning" keyword). The stale filter kept the
        # row because SOMETHING in the trader's enforced blacklist matched
        # the text — find which enforced cat, and use that as the combo key.
        if reason == "category_blacklist":
            meta = metas_test[i] if i < len(metas_test) else {}
            t = (meta.get("trader") or "").strip().lower()
            enforced = current_bl.get(t, set())
            if t and enforced:
                mq = (meta.get("market_question") or "").lower()
                matched_cat = None
                for cat in enforced:
                    for kw in cat_kw.get(cat, []):
                        if kw in mq:
                            matched_cat = cat
                            break
                    if matched_cat:
                        break
                if matched_cat:
                    b["combos"].add((t, matched_cat))

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
            "verified_pnl_usd": None,
        }
        # For category_blacklist, sum the verified pnl across all combos
        # that are in this bucket (= still-enforced combos with ≥1 test row).
        if reason == "category_blacklist" and b["combos"]:
            pnl_sum = 0.0
            pnl_n = 0
            for combo in b["combos"]:
                v = verified_pnl.get(combo)
                if v and v["n"] >= 3:  # need meaningful sample
                    pnl_sum += v["pnl"]
                    pnl_n += v["n"]
            if pnl_n > 0:
                entry["verified_pnl_usd"] = round(pnl_sum, 2)
                entry["verified_n"] = pnl_n

        # Recommendation: verified PnL is ground-truth and overrides
        # precision-based logic when signal is clear.
        vp = entry["verified_pnl_usd"]
        if b["n"] < min_samples:
            entry["recommendation"] = "INSUFFICIENT"
        elif vp is not None and vp <= -5.0:
            # Ground truth shows these combos are net-losing → keep blocked
            # regardless of what ml_block's WR-based precision says.
            entry["recommendation"] = "KEEP"
        elif vp is not None and vp >= 5.0:
            entry["recommendation"] = "LOOSEN"
        elif entry["precision_at_conf"] is None:
            entry["recommendation"] = "NO_CONFIDENT_PREDICTIONS"
        elif entry["precision_at_conf"] / 100.0 >= LOOSEN_THRESHOLD:
            entry["recommendation"] = "LOOSEN"
        elif entry["precision_at_conf"] / 100.0 <= KEEP_THRESHOLD:
            entry["recommendation"] = "KEEP"
        else:
            entry["recommendation"] = "REVIEW"
        # Drop internal sets from the output dict (not JSON serializable)
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
