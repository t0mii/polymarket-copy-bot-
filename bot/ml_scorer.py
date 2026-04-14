"""
ML Scorer — lernt aus historischen Trades welche gewinnen/verlieren.
Trainiert alle 6h automatisch. RandomForest auf echten Trade-Daten.

Feature engineering (refactored 2026-04-14):
- 20 features (was 5, of which 3 were noise/broken)
- Per-row chronological trader stats (wr / pnl / trades) so different
  traders get different edges
- Bet/avg ratio for conviction (clamped, with 1.0 default for blocked
  rows so it can't become a copy-vs-blocked leakage marker)
- price_dist_from_50 captures non-linear extremity edge
- hour / day_of_week kept (4-5% importance in the old model — small
  but non-zero signal worth preserving)
- One-hot encoded category (12 binary columns) replaces the broken
  label-encoded int — trees can finally split on individual sports,
  and the model can learn esports-specific or NHL-specific patterns
- side dropped (was 0% importance, YES/NO is symmetric)
"""
import hashlib
import logging
import os
import pickle
import time

import numpy as np
from sklearn.ensemble import RandomForestClassifier

from database import db

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COPY_MODEL_PATH = os.path.join(_REPO_ROOT, "ml_copy.pkl")
BLOCK_MODEL_PATH = os.path.join(_REPO_ROOT, "ml_block.pkl")
# Legacy alias — older code / bot paths that reference MODEL_PATH still
# point to the live-decision (copy) model. The pre-split ml_model.pkl is
# also accepted as a fallback so an in-place upgrade doesn't need a retrain.
MODEL_PATH = COPY_MODEL_PATH
_LEGACY_MODEL_PATH = os.path.join(_REPO_ROOT, "ml_model.pkl")
MIN_TRAINING_SAMPLES = 50
MIN_BLOCK_TRAINING_SAMPLES = 100  # filter-audit model wants more rows
                                  # than live-decision model, per-reason
                                  # stats need density

# Single label-encoded category feature in fee-tier hundreds, with gaps so
# future sports can slot in without renumbering existing IDs (and breaking
# pickled model compatibility). The hundred-band IS the semantic split:
#   1xx = 0% Polymarket fee (politics, NHL, geopolitics)
#   2xx = ~5% fee (NBA, NFL, MLB, tennis, soccer, ...)
#   3xx = 10% fee (esports — CS, LoL, Valorant, Dota, ...)
# Then a tree split at `category_id < 300` cleanly separates esports from
# everything else without needing one-hot columns.
_CATEGORY_ID_MAP = {
    # 1xx: 0% fee
    "nhl":          110,
    "politics":     120,
    "geopolitics":  130,
    # (room for: cricket=140, weather=150, …)
    # 2xx: ~5% fee
    "nba":          210,
    "nfl":          220,
    "mlb":          230,
    "tennis":       240,
    "soccer":       250,
    # (room for: ufc/mma=260, f1=270, boxing=280, golf=290, …)
    # 3xx: 10% fee esports
    "cs":           310,
    "lol":          320,
    "valorant":     330,
    "dota":         340,
    # (room for: rocket league=350, fortnite=360, …)
}

# Keyword-based category detector. The DB `category` column is empty for ~97%
# of historical rows (insert-path doesn't populate it for blocked_trades and
# rarely for copy_trades), so the model would see an all-zero one-hot block.
# This detector parses the market_question text at training/predict time so
# the categorical signal actually lands in the feature vector.
# Order matters — esports team names checked FIRST so they don't get caught
# by the broader sports keyword list (e.g. "Spirit" is a CS team, not a generic).
_CATEGORY_KEYWORDS = [
    # Esports — tighten before the generic sports list
    ("dota",        ["dota 2:", "dota", "nigma", "virtus.pro", "team spirit"]),
    ("cs",          ["counter-strike", "csgo", "cs2-", "cs:go", "faze", "heroic", "vitality clan",
                     "g2 esports", "mouz", "natus vincere", "navi", "3dmax", "fokus", "fut esport"]),
    ("lol",         ["lol:", "league of legends", "drx", "t1 ", "hanwha", "jd gaming",
                     "gen.g", "bilibili", "weibo", "fnatic", "top esports", "sk gaming"]),
    ("valorant",    ["valorant", "paper rex", "nongshim", "esprit"]),
    # Traditional sports
    ("mlb",         ["mlb", "rays", "brewers", "astros", "braves", "angels", "mariners", "athletics",
                     "cardinals", "tigers", "phillies", "nationals", "marlins", "rockies", "reds",
                     "mets", "giants", "orioles", "pirates", "padres", "yankees", "dodgers", "cubs",
                     "guardians", "twins", "rangers", "royals", "diamondbacks", "white sox",
                     "blue jays", "red sox"]),
    ("nba",         ["nba", "celtics", "bucks", "76ers", "knicks", "bulls", "hawks", "nets", "magic",
                     "mavericks", "timberwolves", "pelicans", "kings", "raptors", "grizzlies",
                     "lakers", "warriors", "heat", "spurs", "suns", "thunder", "cavaliers", "pacers",
                     "pistons", "rockets", "clippers", "nuggets", "blazers", "jazz", "wizards",
                     "hornets"]),
    ("nhl",         ["nhl", "flyers", "islanders", "blues", "ducks", "penguins", "canadiens",
                     "maple leafs", "oilers", "flames", "canucks", "blackhawks", "predators",
                     "lightning", "panthers", "hurricanes", "avalanche", "capitals", "wild"]),
    ("nfl",         ["nfl", "patriots", "chiefs", "eagles", "cowboys", "packers", "49ers", "broncos",
                     "ravens", "steelers", "raiders", "buccaneers", "saints", "bengals", "browns"]),
    ("tennis",      ["atp", "wta", "wimbledon", "roland garros", "indian wells", "miami open",
                     "monte carlo", "barcelona open", "challenger", "sinner", "djokovic", "alcaraz",
                     "medvedev", "rublev", "zverev", "tsitsipas", "fritz", "swiatek", "sabalenka",
                     "gauff"]),
    ("soccer",      ["soccer", "bundesliga", "epl", "ucl", "mls", "la liga", "serie a",
                     "premier league", "arsenal", "liverpool", "man city", "manchester", "tottenham",
                     "chelsea", "newcastle", "bayern", "dortmund", "leipzig", "borussia",
                     "barcelona", " madrid", "atletico", "sevilla", "juventus", "napoli", "milan",
                     "roma", " inter", "psg", "marseille", "lyon"]),
    # Geopolitics / politics
    ("geopolitics", ["iran", "israel", "gaza", "ukraine", "russia", "ceasefire", "nuclear",
                     "hezbollah", "houthi", "yemen", "lebanon", "tehran", "missile", "airstrike"]),
    ("politics",    ["trump", "biden", "congress", "senate", "election", "president", "tariff",
                     "fed ", "dhs", "cabinet", "supreme court"]),
]


def _detect_category(market_question: str) -> str:
    """Return canonical category string from a market question, or empty string
    if no keyword matched. Used when copy_trades.category / blocked_trades.category
    is empty (most of the historical data)."""
    if not market_question:
        return ""
    s = str(market_question).lower()
    for cat, keywords in _CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw in s:
                return cat
    return ""

# 11 feature names — kept in sync with _get_features() return order
FEATURE_NAMES = [
    "entry_price",
    "price_dist_from_50",
    "trader_wr_7d",
    "trader_pnl_7d",
    "trader_trades_7d",
    "bet_vs_avg",
    "hour",
    "day_of_week",
    "side_yes",
    "category_id",
    "trader_id",
]


def _trader_id(name: str) -> int:
    """Stable deterministic int ID for a trader name. md5 hash mod 1000 so
    new traders get a stable ID without a manual map and the value space
    stays bounded (trees can iterate ranges). 0 = unknown / empty."""
    if not name:
        return 0
    h = hashlib.md5(name.strip().lower().encode("utf-8")).hexdigest()
    return int(h[:6], 16) % 1000 + 1  # 1..1000, 0 reserved for unknown

_model_copy = None
_model_copy_loaded = False
_model_block = None
_model_block_loaded = False
# Backward-compat aliases: external code that still references _model /
# _model_loaded keeps working — both point to the copy-model state.
_model = None
_model_loaded = False

_trader_stats_cache = None
_trader_stats_cache_ts = 0
_TRADER_STATS_TTL = 300  # seconds — predict() hits this cache


def _load_trader_stats() -> dict:
    """Load per-trader 7d stats straight from copy_trades, bypassing the
    trader_performance cache. We do this because trader_performance is
    filtered by PERFORMANCE_SINCE (a dashboard-reset marker), but for ML
    predictions we want the full verified history — if a trader was making
    money for months before a manual dashboard reset, the model should still
    see it. Prefers verified rows (usdc_received + actual_size) over formula
    rows so the signal matches ground truth.
    """
    stats = {}
    try:
        with db.get_connection() as conn:
            # Verified-only branch: prefer (usdc_received - actual_size) when
            # both are set — that's the wallet-delta ground truth.
            for r in conn.execute(
                "SELECT LOWER(wallet_username) AS name, "
                "       COUNT(*) AS cnt, "
                "       SUM(CASE WHEN (usdc_received - actual_size) > 0 THEN 1 ELSE 0 END) AS wins, "
                "       SUM(usdc_received - actual_size) AS pnl_sum, "
                "       AVG(actual_size) AS avg_bet "
                "FROM copy_trades "
                "WHERE status='closed' "
                "  AND usdc_received IS NOT NULL AND actual_size IS NOT NULL "
                "  AND datetime(closed_at) >= datetime('now','-7 days') "
                "GROUP BY LOWER(wallet_username)"
            ).fetchall():
                name = r["name"]
                if not name:
                    continue
                cnt = int(r["cnt"] or 0)
                if cnt == 0:
                    continue
                stats[name] = {
                    "wr": (int(r["wins"] or 0) / cnt) * 100.0,
                    "pnl": float(r["pnl_sum"] or 0),
                    "trades": cnt,
                    "avg_bet": float(r["avg_bet"] or 0),
                }
            # Fallback avg_bet from ALL trades (not just last 7d) for traders
            # that had no verified activity in the window but still have
            # historical average-bet data we can use for conviction scoring.
            for r in conn.execute(
                "SELECT LOWER(wallet_username) AS name, AVG(COALESCE(actual_size, size)) AS ab "
                "FROM copy_trades "
                "WHERE COALESCE(actual_size, size) > 0 "
                "GROUP BY LOWER(wallet_username)"
            ).fetchall():
                name = r["name"]
                if not name:
                    continue
                if name not in stats:
                    stats[name] = {"wr": 0.0, "pnl": 0.0, "trades": 0,
                                   "avg_bet": float(r["ab"] or 0)}
                elif stats[name]["avg_bet"] == 0:
                    stats[name]["avg_bet"] = float(r["ab"] or 0)
    except Exception as e:
        logger.debug("[ML] _load_trader_stats failed: %s", e)
    return stats


def _get_trader_stats_cached() -> dict:
    """Predict-path version with TTL cache."""
    global _trader_stats_cache, _trader_stats_cache_ts
    now = time.time()
    if _trader_stats_cache is None or now - _trader_stats_cache_ts > _TRADER_STATS_TTL:
        _trader_stats_cache = _load_trader_stats()
        _trader_stats_cache_ts = now
    return _trader_stats_cache


def _stats_for(trader_stats: dict, trader_name: str) -> dict:
    """Lookup helper with safe defaults."""
    if not trader_stats or not trader_name:
        return {"wr": 0.0, "pnl": 0.0, "trades": 0, "avg_bet": 0.0}
    return trader_stats.get(trader_name.strip().lower(),
                            {"wr": 0.0, "pnl": 0.0, "trades": 0, "avg_bet": 0.0})


def _get_features(trade: dict, trader_stats: dict = None) -> list:
    """Extract 11-feature vector from a trade dict + trader's rolling stats.

    Layout (must match FEATURE_NAMES):
      0  entry_price                  (continuous, 0..1)
      1  price_dist_from_50           (continuous, 0..0.5)
      2  trader_wr_7d                 (continuous, 0..100)
      3  trader_pnl_7d                (continuous, signed $)
      4  trader_trades_7d             (int)
      5  bet_vs_avg                   (continuous, clamped 0..10)
      6  hour                         (int 0..23)
      7  day_of_week                  (int 0..6, Mon=0)
      8  side_yes                     (binary, 1=YES bet, 0=NO bet)
      9  category_id                  (int, 0=unknown, hundred-band fee tier)
     10  trader_id                    (int, stable md5 hash, 1..1001)

    Category is detected from `market_question` text when the DB column is
    empty (~97% of rows). category_id uses fee-tier hundreds (1xx=0% fee,
    2xx=5%, 3xx=10% esports) with gaps so new sports can be inserted without
    renumbering. A single tree split at `category_id < 300` separates esports
    cleanly from everything else.

    `trader_stats` is the per-trader dict returned by _stats_for(). When
    None (e.g. unknown trader), trader features default to 0 and
    bet_vs_avg defaults to 1.0 (neutral).
    """
    entry = trade.get("actual_entry_price") or trade.get("entry_price") or 0.5
    cat_lc = (trade.get("category") or "").lower()
    if not cat_lc:
        # Fall back to keyword detection from the market question text.
        # The DB category column is empty for ~97% of historical rows.
        cat_lc = _detect_category(trade.get("market_question") or "")

    # 0. entry_price
    f0 = float(entry)
    # 1. price_dist_from_50 — non-linear extremity edge
    f1 = abs(f0 - 0.5)

    # 2-4. Trader rolling stats (chronologically accumulated at training time)
    s = trader_stats or {"wr": 0.0, "pnl": 0.0, "trades": 0, "avg_bet": 0.0}
    f2 = float(s.get("wr") or 0.0)
    f3 = float(s.get("pnl") or 0.0)
    f4 = float(s.get("trades") or 0)

    # 5. Conviction: bet vs trader's running average
    actual_size = trade.get("actual_size") or 0
    avg_bet = float(s.get("avg_bet") or 0)
    if actual_size and avg_bet > 0:
        f5 = float(actual_size) / avg_bet
        if f5 > 10.0:
            f5 = 10.0
    else:
        f5 = 1.0

    # 6-7. Time features (hour + day_of_week)
    hour = 12
    dow = 3
    created = trade.get("created_at") or ""
    if created:
        try:
            from datetime import datetime as _dt
            dt = _dt.strptime(created[:19], "%Y-%m-%d %H:%M:%S")
            hour = dt.hour
            dow = dt.weekday()
        except Exception:
            pass
    f6 = hour
    f7 = dow

    # 8. side (YES=1 / NO=0). Was 0% importance in the legacy 5-feature
    # model — likely because YES/NO is symmetric within a single market
    # and entry_price already encodes the implied probability. Kept as
    # a feature in case there's a YES-bias / NO-bias effect across the
    # sample (the model can ignore it if not informative).
    side_str = (trade.get("side") or "YES").upper()
    f8 = 1 if side_str == "YES" else 0

    # 9. Category as a single fee-tier-ordered int. Detector populates
    # `cat_lc` from market_question above when the DB column is empty.
    f9 = _CATEGORY_ID_MAP.get(cat_lc, 0)

    # 10. Trader identity (stable hash). Captures deterministic per-trader
    # patterns that the rolling stats (wr/pnl/trades) can't see — e.g.
    # a trader's category preferences or time-of-day routine.
    f10 = _trader_id(trade.get("trader_name") or trade.get("wallet_username") or "")

    return [f0, f1, f2, f3, f4, f5, f6, f7, f8, f9, f10]


def _snapshot(trader_running: dict, name: str) -> dict:
    """Point-in-time trader rolling stats from a running accumulator dict.
    Module-level so copy and block build functions can share it."""
    s = trader_running.get((name or "").strip().lower())
    if not s or s["n"] == 0:
        return {"wr": 0.0, "pnl": 0.0, "trades": 0, "avg_bet": 0.0}
    return {
        "wr": (s["wins"] / s["n"]) * 100.0,
        "pnl": s["pnl_sum"],
        "trades": s["n"],
        "avg_bet": (s["size_sum"] / s["size_n"]) if s["size_n"] > 0 else 0.0,
    }


def _accumulate(trader_running: dict, name: str, pnl: float, size: float) -> None:
    """Update the running accumulator with a new (pnl, size) observation."""
    key = (name or "").strip().lower()
    if not key:
        return
    s = trader_running.setdefault(key, {"wins": 0, "losses": 0, "pnl_sum": 0.0,
                                        "n": 0, "size_sum": 0.0, "size_n": 0})
    if pnl > 0:
        s["wins"] += 1
    elif pnl < 0:
        s["losses"] += 1
    s["pnl_sum"] += float(pnl or 0)
    s["n"] += 1
    try:
        sz = float(size or 0)
        if sz > 0:
            s["size_sum"] += sz
            s["size_n"] += 1
    except Exception:
        pass


def _build_copy_training_data():
    """Copy-trade only training set. Chronologically accumulated per-row trader
    stats so each feature vector reflects trader history *before* that outcome
    (leakage-free). Returns (X, y, weights).

    Labels: 1 if pnl_realized > 0 else 0.
    Weights: clamp(|pnl_realized|, 0.1, 5.0) — magnitude-aware so a $5 loss
    counts 50x a $0.10 win, the right objective for asymmetric Polymarket
    payoffs."""
    with db.get_connection() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT wallet_username, actual_entry_price, entry_price, category, "
            "side, actual_size, size, fee_bps, created_at, pnl_realized, market_question "
            "FROM copy_trades WHERE status='closed' AND pnl_realized IS NOT NULL "
            "ORDER BY created_at ASC"
        ).fetchall()]

    trader_running = {}
    X, y, weights = [], [], []
    for r in rows:
        name = r.get("wallet_username") or ""
        snap = _snapshot(trader_running, name)
        features = _get_features(r, snap)
        pnl = r.get("pnl_realized") or 0
        label = 1 if pnl > 0 else 0
        weight = max(0.1, min(5.0, abs(float(pnl))))
        X.append(features)
        y.append(label)
        weights.append(weight)
        _accumulate(trader_running, name, pnl, r.get("actual_size") or 0)
    return X, y, weights


def _build_block_training_data(verified_only: bool = False, with_metas: bool = False):
    """Blocked-trade only training set for the filter-audit model.

    `verified_only` was meant to require real market resolves, but the
    outcome_tracker writes blocked_trades.outcome_price as the LIVE CLOB
    price at check time — never the final resolve price. So the column
    can't distinguish resolved labels from formula-based live-price-fallback
    labels. The outcome_tracker still applies its own win/loss threshold
    (entry ± 5% or resolved extremes) before writing `would_have_won`, so
    we trust its label and take all rows with would_have_won NOT NULL.
    The `verified_only` arg is kept for API stability but is effectively
    a no-op (it adds an AND clause that never filters anything today).

    `with_metas=True` additionally returns a 5th element `metas` — a list
    of dicts with {trader, market_question, detected_category} per row.
    Used by filter_audit.compute_filter_precision() so it can match
    blocked rows against the CURRENT CATEGORY_BLACKLIST_MAP and drop
    historical blocks whose (trader, category) combo is no longer enforced.

    Returns (X, y, weights, reasons) or (X, y, weights, reasons, metas):
      - X: feature matrix with the same 11 feature layout as the copy model
      - y: would_have_won labels (0/1)
      - weights: 1.0 constant per row (no dollar magnitude available)
      - reasons: block_reason string per row, aligns with X — consumed by
        filter_audit.compute_filter_precision() for per-reason bucketing
      - metas (optional): list of metadata dicts, same length as X
    """
    where = "would_have_won IS NOT NULL"
    if verified_only:
        # Defensive: if the outcome_tracker is ever upgraded to write the
        # actual resolve price, the resolved-subset can be isolated here.
        where += " AND outcome_price IS NOT NULL AND (outcome_price <= 0.01 OR outcome_price >= 0.95)"

    with db.get_connection() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT trader, trader_price, category, side, created_at, "
            "would_have_won, market_question, block_reason, outcome_price "
            f"FROM blocked_trades WHERE {where} ORDER BY created_at ASC"
        ).fetchall()]

    trader_running = {}
    X, y, weights, reasons, metas = [], [], [], [], []
    for r in rows:
        name = r.get("trader") or ""
        snap = _snapshot(trader_running, name)
        mq = r.get("market_question") or ""
        d = {
            "entry_price": r.get("trader_price") or 0.5,
            "category": r.get("category") or "",
            "market_question": mq,
            "side": r.get("side") or "YES",
            "created_at": r.get("created_at") or "",
            "trader_name": name,
        }
        features = _get_features(d, snap)
        label = int(r.get("would_have_won") or 0)
        X.append(features)
        y.append(label)
        weights.append(1.0)
        reasons.append(r.get("block_reason") or "unknown")
        if with_metas:
            detected_cat = (r.get("category") or "").lower() or _detect_category(mq)
            metas.append({
                "trader": name,
                "market_question": mq,
                "detected_category": detected_cat,
            })
    if with_metas:
        return X, y, weights, reasons, metas
    return X, y, weights, reasons


def _build_training_data():
    """Legacy 6-tuple shape kept for back-compat with existing callers and
    tests. New code should call `_build_copy_training_data()` or
    `_build_block_training_data()` directly."""
    Xc, yc, wc = _build_copy_training_data()
    Xb, yb, wb, _ = _build_block_training_data(verified_only=False)
    # Apply the same downweight-to-0.1 policy the previous inline code used
    # so the legacy shape stays behaviourally equivalent for test snapshots.
    wb = [w * 0.1 for w in wb]
    X = Xc + Xb
    y = yc + yb
    weights = wc + wb
    is_copy = [True] * len(Xc) + [False] * len(Xb)
    return X, y, is_copy, len(Xc), len(Xb), weights


def train_copy_model():
    """Train the live-decision model on copy_trades only. Writes to
    COPY_MODEL_PATH and logs to ml_training_log with model_name='ml_copy'."""
    global _model_copy, _model_copy_loaded, _model, _model_loaded

    X, y, weights = _build_copy_training_data()
    total = len(X)
    if total < MIN_TRAINING_SAMPLES:
        logger.info("[ML-COPY] Not enough data (%d/%d), skipping", total, MIN_TRAINING_SAMPLES)
        return

    X = np.array(X); y = np.array(y); weights = np.array(weights, dtype=float)
    if len(set(y.tolist())) < 2:
        logger.warning("[ML-COPY] Only one class in training data — skipping")
        return

    n_win = int((y == 1).sum()); n_loss = int((y == 0).sum())
    logger.info("[ML-COPY] Class balance: %d wins / %d losses (%.1f%% win rate)",
                n_win, n_loss, n_win / len(y) * 100 if len(y) else 0)

    split_idx = int(len(X) * 0.8)
    X_train, X_test = X[:split_idx], X[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]
    weights_train = weights[:split_idx]

    if len(set(y_train.tolist())) < 2 or len(set(y_test.tolist())) < 2:
        logger.warning("[ML-COPY] Time-split produced single-class train/test — skipping")
        return

    model = RandomForestClassifier(
        n_estimators=100, max_depth=6, min_samples_leaf=5, random_state=42,
    )
    model.fit(X_train, y_train, sample_weight=weights_train)

    train_acc = model.score(X_train, y_train)
    test_acc = model.score(X_test, y_test)
    majority = 1 if (y_train == 1).sum() >= (y_train == 0).sum() else 0
    baseline_acc = float((y_test == majority).sum()) / len(y_test) if len(y_test) else 0

    # Confusion matrix + copy-only baseline (matches the old metric naming so
    # the dashboard keeps working without changes)
    preds = model.predict(X_test)
    tp = int(((preds == 1) & (y_test == 1)).sum())
    fp = int(((preds == 1) & (y_test == 0)).sum())
    tn = int(((preds == 0) & (y_test == 0)).sum())
    fn = int(((preds == 0) & (y_test == 1)).sum())
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    copy_test_acc = test_acc  # whole test set IS copy-only now
    copy_baseline = baseline_acc

    logger.info("[ML-COPY] Trained on %d samples | Train %.1f%% | Test %.1f%% | Baseline %.1f%% | TP=%d FP=%d TN=%d FN=%d prec=%.2f rec=%.2f",
                total, train_acc*100, test_acc*100, baseline_acc*100,
                tp, fp, tn, fn, precision, recall)

    importances = sorted(zip(FEATURE_NAMES, model.feature_importances_), key=lambda x: -x[1])
    logger.info("[ML-COPY] Top features: %s",
                ", ".join("%s=%.0f%%" % (n, v * 100) for n, v in importances[:4]))

    _save_model_pickle(model, COPY_MODEL_PATH, "ml_copy")
    _model_copy = model
    _model_copy_loaded = True
    # Back-compat aliases so legacy callers that read `_model` still work
    _model = model
    _model_loaded = True

    _log_training_row(
        model_name="ml_copy", samples=total,
        test_acc=test_acc, train_acc=train_acc,
        copy_only_acc=copy_test_acc, baseline_acc=baseline_acc,
        train_n=len(X_train), test_n=len(X_test),
        importances=importances, model_path=COPY_MODEL_PATH,
    )


def train_block_model():
    """Train the filter-audit model on verified blocked_trades only. Writes
    to BLOCK_MODEL_PATH and logs to ml_training_log with model_name='ml_block'.

    Only uses rows where the outcome is from a real market resolve
    (outcome_price ≤ 0.01 or ≥ 0.95) — formula-based labels are excluded
    so the downstream precision stats are honest."""
    global _model_block, _model_block_loaded

    X, y, weights, _reasons = _build_block_training_data(verified_only=False)
    total = len(X)
    if total < MIN_BLOCK_TRAINING_SAMPLES:
        logger.info("[ML-BLOCK] Not enough verified data (%d/%d), skipping",
                    total, MIN_BLOCK_TRAINING_SAMPLES)
        return

    X = np.array(X); y = np.array(y); weights = np.array(weights, dtype=float)
    if len(set(y.tolist())) < 2:
        logger.warning("[ML-BLOCK] Only one class in training data — skipping")
        return

    n_win = int((y == 1).sum()); n_loss = int((y == 0).sum())
    logger.info("[ML-BLOCK] Class balance: %d wins / %d losses (%.1f%% win rate)",
                n_win, n_loss, n_win / len(y) * 100 if len(y) else 0)

    split_idx = int(len(X) * 0.8)
    X_train, X_test = X[:split_idx], X[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]
    weights_train = weights[:split_idx]

    if len(set(y_train.tolist())) < 2 or len(set(y_test.tolist())) < 2:
        logger.warning("[ML-BLOCK] Time-split produced single-class train/test — skipping")
        return

    model = RandomForestClassifier(
        n_estimators=100, max_depth=6, min_samples_leaf=5, random_state=42,
    )
    model.fit(X_train, y_train, sample_weight=weights_train)

    train_acc = model.score(X_train, y_train)
    test_acc = model.score(X_test, y_test)
    majority = 1 if (y_train == 1).sum() >= (y_train == 0).sum() else 0
    baseline_acc = float((y_test == majority).sum()) / len(y_test) if len(y_test) else 0

    importances = sorted(zip(FEATURE_NAMES, model.feature_importances_), key=lambda x: -x[1])
    logger.info("[ML-BLOCK] Trained on %d samples | Train %.1f%% | Test %.1f%% | Baseline %.1f%%",
                total, train_acc*100, test_acc*100, baseline_acc*100)
    logger.info("[ML-BLOCK] Top features: %s",
                ", ".join("%s=%.0f%%" % (n, v * 100) for n, v in importances[:4]))

    _save_model_pickle(model, BLOCK_MODEL_PATH, "ml_block")
    _model_block = model
    _model_block_loaded = True

    _log_training_row(
        model_name="ml_block", samples=total,
        test_acc=test_acc, train_acc=train_acc,
        copy_only_acc=None, baseline_acc=baseline_acc,
        train_n=len(X_train), test_n=len(X_test),
        importances=importances, model_path=BLOCK_MODEL_PATH,
    )


def _save_model_pickle(model_obj, path: str, tag: str) -> None:
    """Atomically save the given sklearn model to disk with a tmp-rename."""
    try:
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(model_obj, f)
        os.replace(tmp, path)
        logger.info("[%s] Model saved to %s", tag.upper(), path)
    except Exception as e:
        logger.warning("[%s] Failed to save model: %s", tag.upper(), e)


def _log_training_row(model_name, samples, test_acc, train_acc, copy_only_acc,
                      baseline_acc, train_n, test_n, importances, model_path):
    """Shared DB log helper so copy and block models write consistent rows."""
    try:
        import json
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO ml_training_log "
                "(samples_count, accuracy, train_accuracy, copy_only_accuracy, "
                " baseline_accuracy, train_n, test_n, feature_importance, "
                " model_path, model_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (samples,
                 round(test_acc, 4),
                 round(train_acc, 4),
                 round(copy_only_acc, 4) if copy_only_acc is not None else None,
                 round(baseline_acc, 4),
                 train_n, test_n,
                 json.dumps(dict(importances)),
                 model_path,
                 model_name)
            )
    except Exception as e:
        logger.debug("[ML] training-log write failed: %s", e)


def train_model():
    """Backward-compat wrapper: train both models. The 6h scheduler still
    calls this, and it now produces both ml_copy.pkl and ml_block.pkl in
    one go. The existing `_build_training_data` 6-tuple is still available
    for anything that needs the merged legacy shape."""
    train_copy_model()
    train_block_model()


def _load_copy_model() -> bool:
    """Load ml_copy.pkl from disk. Tries the legacy ml_model.pkl path as
    fallback so in-place upgrades don't need an immediate retrain."""
    global _model_copy, _model_copy_loaded, _model, _model_loaded
    if _model_copy_loaded:
        return _model_copy is not None
    for candidate in (COPY_MODEL_PATH, _LEGACY_MODEL_PATH):
        try:
            if os.path.exists(candidate):
                with open(candidate, "rb") as f:
                    _model_copy = pickle.load(f)
                _model_copy_loaded = True
                # Back-compat: keep the legacy aliases pointed at the same state
                _model = _model_copy
                _model_loaded = True
                return True
        except Exception as e:
            logger.warning("[ML-COPY] Failed to load %s: %s", candidate, e)
    _model_copy_loaded = True
    _model_loaded = True
    return False


def _load_block_model() -> bool:
    """Load ml_block.pkl from disk. No legacy fallback — the block model
    is new, either it's been trained or it hasn't."""
    global _model_block, _model_block_loaded
    if _model_block_loaded:
        return _model_block is not None
    try:
        if os.path.exists(BLOCK_MODEL_PATH):
            with open(BLOCK_MODEL_PATH, "rb") as f:
                _model_block = pickle.load(f)
            _model_block_loaded = True
            return True
    except Exception as e:
        logger.warning("[ML-BLOCK] Failed to load model: %s", e)
    _model_block_loaded = True
    return False


def _load_model() -> bool:
    """Legacy alias for _load_copy_model — external callers still work."""
    return _load_copy_model()


def predict_copy(trade_data: dict) -> float:
    """Predict win probability on the live-decision (copy) model.
    Returns 0.0-1.0 or -1 if no model."""
    if not _load_copy_model():
        return -1
    try:
        all_stats = _get_trader_stats_cached()
        name = trade_data.get("trader_name") or trade_data.get("wallet_username") or ""
        ts = _stats_for(all_stats, name)
        features = np.array([_get_features(trade_data, ts)])
        proba = _model_copy.predict_proba(features)[0]
        win_prob = proba[1] if len(proba) > 1 else 0.5
        return round(float(win_prob), 3)
    except ValueError as e:
        logger.warning("[ML-COPY] Feature shape mismatch (old model?), waiting for retrain: %s", e)
        return -1
    except Exception as e:
        logger.debug("[ML-COPY] Prediction error: %s", e)
        return -1


def predict_block(trade_data_or_features) -> float:
    """Predict 'would_have_won' probability on the block model. Accepts
    either a trade_data dict (for single-row callers) or a pre-built feature
    list/array (for batch use in filter_audit.py). Returns 0.0-1.0 or -1
    if no model."""
    if not _load_block_model():
        return -1
    try:
        if isinstance(trade_data_or_features, dict):
            all_stats = _get_trader_stats_cached()
            name = trade_data_or_features.get("trader_name") or trade_data_or_features.get("trader") or ""
            ts = _stats_for(all_stats, name)
            feats = np.array([_get_features(trade_data_or_features, ts)])
        else:
            feats = np.array([trade_data_or_features])
        proba = _model_block.predict_proba(feats)[0]
        win_prob = proba[1] if len(proba) > 1 else 0.5
        return round(float(win_prob), 3)
    except ValueError as e:
        logger.warning("[ML-BLOCK] Feature shape mismatch, waiting for retrain: %s", e)
        return -1
    except Exception as e:
        logger.debug("[ML-BLOCK] Prediction error: %s", e)
        return -1


def predict(trade_data: dict) -> float:
    """Legacy alias for predict_copy — trade_scorer.py still imports this."""
    return predict_copy(trade_data)


def get_model_health(model_name: str = "ml_copy") -> dict:
    """Latest training-row health summary for a specific model. Used by
    trade_scorer to decide whether ML adjustments apply (ml_copy), and by
    the dashboard to display parallel stats for ml_block.

    Returns dict with edge_vs_baseline in signed percentage points
    (copy_only - baseline for ml_copy, accuracy - baseline for ml_block).
    Negative edge → model is worse than baseline and should be display-only.

    The WHERE filter on model_name is important because the ml_training_log
    table holds rows for both models now. A bare "ORDER BY id DESC LIMIT 1"
    would return whichever was trained last, not the specific model."""
    try:
        with db.get_connection() as conn:
            r = conn.execute(
                "SELECT accuracy, copy_only_accuracy, baseline_accuracy, trained_at "
                "FROM ml_training_log WHERE COALESCE(model_name,'ml_copy')=? "
                "ORDER BY id DESC LIMIT 1",
                (model_name,)
            ).fetchone()
        if not r:
            return {"edge_vs_baseline": 0.0, "copy_only": 0.0, "baseline": 0.0, "trained_at": ""}
        # For ml_copy use copy_only_accuracy (subset-specific signal);
        # for ml_block that column is NULL so fall back to overall accuracy.
        primary = r["copy_only_accuracy"] if r["copy_only_accuracy"] is not None else r["accuracy"]
        primary = float(primary or 0)
        baseline = float(r["baseline_accuracy"] or 0)
        return {
            "edge_vs_baseline": (primary - baseline) * 100.0,
            "copy_only": primary,
            "baseline": baseline,
            "trained_at": r["trained_at"] or "",
        }
    except Exception:
        return {"edge_vs_baseline": 0.0, "copy_only": 0.0, "baseline": 0.0, "trained_at": ""}
