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

MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ml_model.pkl")
MIN_TRAINING_SAMPLES = 50

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

_model = None
_model_loaded = False
_trader_stats_cache = None
_trader_stats_cache_ts = 0
_TRADER_STATS_TTL = 300  # seconds — predict() hits this cache


def _load_trader_stats() -> dict:
    """Load per-trader rolling 7d stats once. Returns dict keyed by lowercase
    trader_name. Used by training (one call per train_model run) and by
    predict (cached for _TRADER_STATS_TTL seconds via _get_trader_stats_cached)."""
    stats = {}
    try:
        with db.get_connection() as conn:
            for r in conn.execute(
                "SELECT trader_name, winrate, total_pnl, trades_count "
                "FROM trader_performance WHERE period='7d'"
            ).fetchall():
                name = (r["trader_name"] or "").strip().lower()
                if not name:
                    continue
                stats[name] = {
                    "wr": float(r["winrate"] or 0),
                    "pnl": float(r["total_pnl"] or 0),
                    "trades": int(r["trades_count"] or 0),
                    "avg_bet": 0.0,
                }
            for r in conn.execute(
                "SELECT LOWER(wallet_username) AS name, AVG(actual_size) AS ab "
                "FROM copy_trades WHERE actual_size > 0 GROUP BY LOWER(wallet_username)"
            ).fetchall():
                name = r["name"]
                if not name:
                    continue
                if name in stats:
                    stats[name]["avg_bet"] = float(r["ab"] or 0)
                else:
                    stats[name] = {"wr": 0, "pnl": 0, "trades": 0, "avg_bet": float(r["ab"] or 0)}
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
    # Trader stats at TRAINING time use chronological per-row accumulation
    # from copy_trades themselves — this gives us point-in-time correct
    # stats per row instead of relying on the (sparsely populated)
    # trader_performance table. Leakage-free because each row only sees
    # the stats AS THEY WERE before that row's outcome.
    with db.get_connection() as conn:
        copy_rows = [dict(r) for r in conn.execute(
            "SELECT wallet_username, actual_entry_price, entry_price, category, "
            "side, actual_size, size, fee_bps, created_at, pnl_realized, market_question "
            "FROM copy_trades WHERE status='closed' AND pnl_realized IS NOT NULL "
            "ORDER BY created_at ASC"
        ).fetchall()]
        blocked_rows = [dict(r) for r in conn.execute(
            "SELECT trader, trader_price, category, side, created_at, would_have_won, market_question "
            "FROM blocked_trades WHERE would_have_won IS NOT NULL "
            "ORDER BY created_at ASC"
        ).fetchall()]

    events = []
    for r in copy_rows:
        events.append((r.get("created_at") or "", "copy", r))
    for r in blocked_rows:
        events.append((r.get("created_at") or "", "blocked", r))
    events.sort(key=lambda t: t[0])

    trader_running = {}

    def _snapshot(name):
        s = trader_running.get((name or "").strip().lower())
        if not s or s["n"] == 0:
            return {"wr": 0.0, "pnl": 0.0, "trades": 0, "avg_bet": 0.0}
        return {
            "wr": (s["wins"] / s["n"]) * 100.0,
            "pnl": s["pnl_sum"],
            "trades": s["n"],
            "avg_bet": (s["size_sum"] / s["size_n"]) if s["size_n"] > 0 else 0.0,
        }

    def _accumulate(name, pnl, size):
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

    merged = []
    for ts_str, kind, r in events:
        if kind == "copy":
            name = r.get("wallet_username") or ""
            snap = _snapshot(name)
            features = _get_features(r, snap)
            label = 1 if (r.get("pnl_realized") or 0) > 0 else 0
            merged.append((ts_str, True, features, label))
            _accumulate(name, r.get("pnl_realized") or 0, r.get("actual_size") or 0)
        else:  # blocked
            name = r.get("trader") or ""
            snap = _snapshot(name)
            d = {
                "entry_price": r.get("trader_price") or 0.5,
                "category": r.get("category") or "",
                "market_question": r.get("market_question") or "",
                "side": r.get("side") or "YES",
                "created_at": r.get("created_at") or "",
                "trader_name": name,  # so _trader_id() can hash it
            }
            features = _get_features(d, snap)
            label = int(r.get("would_have_won") or 0)
            merged.append((ts_str, False, features, label))

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

    importances = sorted(zip(FEATURE_NAMES, model.feature_importances_), key=lambda x: -x[1])

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
    """Predict win probability for a trade. Returns 0.0-1.0 or -1 if no model.

    `trade_data` should include `trader_name` (or `wallet_username`) so the
    ML scorer can look up the trader's rolling stats. If missing, predicts
    with neutral defaults — still works but loses the trader-edge signal.
    """
    if not _load_model():
        return -1

    try:
        all_stats = _get_trader_stats_cached()
        name = trade_data.get("trader_name") or trade_data.get("wallet_username") or ""
        ts = _stats_for(all_stats, name)
        features = np.array([_get_features(trade_data, ts)])
        proba = _model.predict_proba(features)[0]
        win_prob = proba[1] if len(proba) > 1 else 0.5
        return round(float(win_prob), 3)
    except ValueError as e:
        # Pickle vs. current feature-count mismatch — old model needs retraining
        logger.warning("[ML] Feature shape mismatch (old model?), waiting for retrain: %s", e)
        return -1
    except Exception as e:
        logger.debug("[ML] Prediction error: %s", e)
        return -1
