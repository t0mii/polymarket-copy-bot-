"""Scenario D Phase γ/D — promotion gate evaluator.

Pure-function gate that takes a candidate's paper-trade stats and
returns (pass: bool, reason: str). Used by:

- `bot.auto_discovery.check_promotions` as the authoritative gate
- `dashboard.app.api_promotion_dryrun` as the read-only dry-run view

Both paths must return the same verdict for the same input — that's
the whole point of factoring this out.

Gates are evaluated in a fixed order; the first failing one determines
the rejection reason:

    1. insufficient_trades      — below statistical power threshold
    2. low_win_rate             — observed WR below fee-adjusted break-even
    3. weak_wilson_lb           — WR is noise at this sample size
    4. low_roi                  — total pnl per trade below cost-of-capital
    5. below_abs_pnl_floor      — absolute pnl too small to matter
    6. stale                    — newest trade too old to trust

This ordering is ALSO the order we want dry-run to report rejections
in: data-quantity first, then quality, then statistical significance,
then magnitude, then recency.
"""
from bot.stats import wilson_lower_bound


def _default_thresholds() -> dict:
    """Read production thresholds from `config.py` at call time.

    Imported lazily so tests can import this module without side-effects.
    """
    import config
    return {
        "min_trades":       int(config.PROMOTE_MIN_PAPER_TRADES),
        "min_wr":           float(config.PROMOTE_MIN_OBSERVED_WR),
        "min_wilson_lower": float(config.PROMOTE_MIN_WILSON_LOWER),
        "min_roi":          float(config.PROMOTE_MIN_PAPER_ROI),
        "min_abs_pnl":      float(config.PROMOTE_MIN_ABS_PNL),
        "max_age_days":     float(config.PROMOTE_MAX_TRADE_AGE_D),
    }


def evaluate_promotion(
    n_trades: int,
    wins: int,
    total_pnl: float,
    newest_trade_age_days: float,
    thresholds: dict = None,
) -> tuple:
    """Return (passed, reason) for a candidate's promotion eligibility.

    Args:
        n_trades: count of closed paper_trades for this candidate
        wins:     count of paper_trades with pnl > 0
        total_pnl: sum of pnl across closed paper_trades
        newest_trade_age_days: days since the newest paper_trade created_at
        thresholds: optional dict overriding config defaults; keys are
                    min_trades, min_wr, min_wilson_lower, min_roi,
                    min_abs_pnl, max_age_days.

    Returns:
        (True, "ok") if all gates pass
        (False, "<reason_code>: <detail>") if any gate fails

    Raises:
        ValueError if wins is negative or greater than n_trades.
    """
    if wins < 0 or wins > n_trades:
        raise ValueError("wins=%s must satisfy 0 <= wins <= n_trades=%s"
                         % (wins, n_trades))

    t = thresholds if thresholds is not None else _default_thresholds()

    if n_trades < t["min_trades"]:
        return False, "insufficient_trades: %d < %d" % (n_trades, t["min_trades"])

    wr_pct = (wins * 100.0 / n_trades) if n_trades > 0 else 0.0
    if wr_pct < t["min_wr"]:
        return False, "low_win_rate: %.1f%% < %.1f%%" % (wr_pct, t["min_wr"])

    lb = wilson_lower_bound(wins, n_trades)
    if lb < t["min_wilson_lower"]:
        return False, "weak_wilson_lb: %.3f < %.3f" % (lb, t["min_wilson_lower"])

    # ROI is defined as total pnl divided by trade count. paper_pnl is in
    # dollars (set by _paper_bet_size at close time) but all trades use the
    # same bet-size formula so the denominator is effectively a constant
    # bet-size factor — the ratio gives a dimensionless "edge per trade"
    # number that matches the "3% edge" semantic in project_promotion_criteria.md.
    roi = total_pnl / n_trades if n_trades > 0 else 0.0
    if roi < t["min_roi"]:
        return False, "low_roi: %.4f < %.4f" % (roi, t["min_roi"])

    if total_pnl < t["min_abs_pnl"]:
        return False, "below_abs_pnl_floor: $%.2f < $%.2f" % (total_pnl, t["min_abs_pnl"])

    if newest_trade_age_days > t["max_age_days"]:
        return False, "stale: newest_trade %.1fd > %.1fd" % (
            newest_trade_age_days, t["max_age_days"])

    return True, "ok"


def promotion_cooldown_active(db_module=None) -> tuple:
    """Return (active: bool, reason: str).

    Queries `activity_log` for the most-recent row with
    event_type='promotion' and checks whether its age is within
    PROMOTE_COOLDOWN_DAYS. If yes, further auto-promotions are
    temporarily blocked — this prevents a single noisy weekend from
    flipping multiple traders live simultaneously.

    Returns (False, "ok") if no recent promotion event exists.
    """
    import config
    if db_module is None:
        from database import db as db_module

    cooldown_d = float(getattr(config, "PROMOTE_COOLDOWN_DAYS", 7.0))
    with db_module.get_connection() as conn:
        row = conn.execute(
            "SELECT created_at FROM activity_log "
            "WHERE event_type = 'promotion' "
            "  AND julianday('now', 'localtime') - julianday(created_at) <= ? "
            "ORDER BY created_at DESC LIMIT 1",
            (cooldown_d,),
        ).fetchone()

    if row:
        return True, "cooldown: last promotion at %s (cooldown %.1fd)" % (
            row["created_at"], cooldown_d)
    return False, "ok"


# =============================================================================
# γ.5 — Probation tier state
# =============================================================================

def start_probation(address: str, db_module=None) -> None:
    """Enter a fresh trader into the probation window.

    Sets `auto_promoted_at=now`, `probation_until=now + PROBATION_DURATION_DAYS`,
    and `probation_trades_left = PROBATION_MAX_TRADES`. Called from the
    auto-promotion path immediately before `_add_followed_trader`.
    """
    import config
    from datetime import datetime, timedelta
    if db_module is None:
        from database import db as db_module

    now = datetime.now()
    promoted_at = now.strftime("%Y-%m-%d %H:%M:%S")
    prob_until = (now + timedelta(days=float(config.PROBATION_DURATION_DAYS))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    trades_left = int(config.PROBATION_MAX_TRADES)

    with db_module.get_connection() as conn:
        conn.execute(
            "UPDATE trader_candidates SET "
            "  auto_promoted_at = ?, "
            "  probation_until = ?, "
            "  probation_trades_left = ? "
            "WHERE address = ?",
            (promoted_at, prob_until, trades_left, address),
        )


def is_in_probation(username: str, db_module=None) -> tuple:
    """Return (active: bool, reason: str) for a trader's probation state.

    Active iff probation_until is a non-empty future timestamp AND
    probation_trades_left > 0. Graduation happens on the earlier of
    the two conditions.
    """
    if db_module is None:
        from database import db as db_module

    with db_module.get_connection() as conn:
        row = conn.execute(
            "SELECT probation_until, probation_trades_left "
            "FROM trader_candidates WHERE username = ? LIMIT 1",
            (username,),
        ).fetchone()
    if not row:
        return False, "none"
    until = row["probation_until"] or ""
    trades_left = int(row["probation_trades_left"] or 0)
    if not until:
        return False, "none"
    if trades_left <= 0:
        return False, "graduated_trades"

    from datetime import datetime
    try:
        until_dt = datetime.strptime(until, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False, "invalid_probation_until"
    if until_dt <= datetime.now():
        return False, "graduated_time"

    return True, "active until %s, %d trades left" % (until, trades_left)


def decrement_probation_trade(username: str, db_module=None) -> None:
    """Decrement `probation_trades_left` by 1, clamped at 0.

    Called from the live copy-trade creation path after a successful
    buy for this trader. No-op if the trader is not in probation.
    """
    if db_module is None:
        from database import db as db_module

    with db_module.get_connection() as conn:
        conn.execute(
            "UPDATE trader_candidates SET "
            "  probation_trades_left = MAX(probation_trades_left - 1, 0) "
            "WHERE username = ? "
            "  AND probation_until IS NOT NULL AND probation_until != '' "
            "  AND probation_trades_left > 0",
            (username,),
        )


def probation_limits(username: str, db_module=None) -> tuple:
    """Return (bet_multiplier, max_exposure_cap_usd) for a trader.

    If in probation: (PROBATION_BET_SIZE_PCT, PROBATION_MAX_EXPOSURE_USD).
    Otherwise: (1.0, None) which signals "use standard sizing, no override".

    Called by the bet-sizing path (wiring deferred to a separate
    shadow-canary commit).
    """
    import config
    active, _ = is_in_probation(username, db_module=db_module)
    if not active:
        return 1.0, None
    mult = float(getattr(config, "PROBATION_BET_SIZE_PCT", 0.5))
    cap = float(getattr(config, "PROBATION_MAX_EXPOSURE_USD", 5.0))
    return mult, cap


def compute_dry_run(db_module=None) -> dict:
    """Read-only snapshot: who would be promoted RIGHT NOW under the
    current thresholds + safety rails, and why the others would fail.

    Used by the `/api/upgrade/promotion-dryrun` dashboard endpoint to
    give a live view of the promotion gate WITHOUT actually promoting
    anything. This is how we tune PROMOTE_* values over weeks before
    flipping AUTO_DISCOVERY_AUTO_PROMOTE to true.

    Returns:
        {
          "thresholds":             <dict from _default_thresholds>,
          "cooldown_active":        bool,
          "cooldown_reason":        str,
          "circuit_breaker_halted": bool,
          "circuit_breaker_reason": str,
          "candidates": [
            {
              "address", "username", "status",
              "n_trades", "wins", "total_pnl", "winrate",
              "wilson_lower_bound", "newest_trade_age_days",
              "would_promote": bool,
              "rejection_reason": str,     # "ok" if would_promote
            },
            ...
          ],
        }
    """
    if db_module is None:
        from database import db as db_module

    import config as _config
    thresholds = _default_thresholds()
    cooldown_active, cooldown_reason = promotion_cooldown_active(db_module)
    breaker_halted, breaker_reason = compute_circuit_breaker_state(db_module)

    # Scenario D Phase E.1 — stats cutoff filter. The cutoff moves
    # into the LEFT JOIN condition (not the WHERE clause) so
    # candidates with zero post-cutoff rows still appear in the
    # output with n_trades=0 (the insufficient_trades branch of
    # evaluate_promotion will reject them, which is the correct
    # dry-run behavior).
    cutoff = (getattr(_config, 'PROMOTE_STATS_CUTOFF', '') or '').strip()
    if cutoff:
        join_extra = " AND pt.closed_at >= ?"
        join_params = (cutoff,)
    else:
        join_extra = ""
        join_params = ()

    with db_module.get_connection() as conn:
        rows = conn.execute(
            "SELECT tc.address, tc.username, tc.status, "
            "  COUNT(pt.id) AS n_trades, "
            "  SUM(CASE WHEN pt.pnl > 0 THEN 1 ELSE 0 END) AS wins, "
            "  COALESCE(SUM(pt.pnl), 0) AS total_pnl, "
            "  MAX(pt.created_at) AS newest_trade_at "
            "FROM trader_candidates tc "
            "LEFT JOIN paper_trades pt ON pt.candidate_address = tc.address "
            "  AND pt.status = 'closed'" + join_extra + " "
            "WHERE tc.status IN ('observing', 'promoted') "
            "GROUP BY tc.address "
            "ORDER BY total_pnl DESC",
            join_params,
        ).fetchall()

    from datetime import datetime
    now = datetime.now()

    candidates = []
    for r in rows:
        n = int(r["n_trades"] or 0)
        wins = int(r["wins"] or 0)
        total_pnl = float(r["total_pnl"] or 0)
        newest_raw = r["newest_trade_at"] or ""
        if newest_raw:
            try:
                newest_dt = datetime.strptime(newest_raw, "%Y-%m-%d %H:%M:%S")
                age_days = (now - newest_dt).total_seconds() / 86400.0
            except ValueError:
                age_days = 9999.0
        else:
            age_days = 9999.0

        winrate = (wins * 100.0 / n) if n > 0 else 0.0
        wlb = wilson_lower_bound(wins, n)

        try:
            passed, reason = evaluate_promotion(
                n_trades=n,
                wins=wins,
                total_pnl=total_pnl,
                newest_trade_age_days=age_days,
                thresholds=thresholds,
            )
        except ValueError as e:
            passed, reason = False, "invalid_stats: %s" % e

        candidates.append({
            "address": r["address"],
            "username": r["username"] or "",
            "status": r["status"],
            "n_trades": n,
            "wins": wins,
            "total_pnl": round(total_pnl, 4),
            "winrate": round(winrate, 2),
            "wilson_lower_bound": round(wlb, 4),
            "newest_trade_age_days": round(age_days, 2),
            "would_promote": passed,
            "rejection_reason": reason,
        })

    return {
        "thresholds": thresholds,
        "cooldown_active": cooldown_active,
        "cooldown_reason": cooldown_reason,
        "circuit_breaker_halted": breaker_halted,
        "circuit_breaker_reason": breaker_reason,
        "candidates": candidates,
    }


def compute_circuit_breaker_state(db_module=None) -> tuple:
    """Return (halted: bool, reason: str).

    Checks whether any recently-auto-promoted trader has accumulated
    losses above `CIRCUIT_BREAKER_MAX_LOSS_USD` during their first
    `CIRCUIT_BREAKER_WINDOW_DAYS` of live trading. If yes, ALL future
    auto-promotions halt until a human investigates.

    "Recently" means: `trader_candidates.auto_promoted_at` is within
    the last window_days. "Losses during the window" means: all
    `copy_trades` rows for that wallet_username with closed_at
    strictly greater than auto_promoted_at and within the window.

    Pre-promotion historical losses do NOT count — we only judge the
    trader on their post-promote performance.

    There is no persisted "halted" flag. The computation is always
    fresh. Audit history comes from logging each halt decision to
    activity_log at the caller's discretion.
    """
    import config
    if db_module is None:
        from database import db as db_module

    max_loss = float(getattr(config, "CIRCUIT_BREAKER_MAX_LOSS_USD", 10.0))
    window_d = float(getattr(config, "CIRCUIT_BREAKER_WINDOW_DAYS", 7.0))

    with db_module.get_connection() as conn:
        # Recently auto-promoted candidates only.
        candidates = conn.execute(
            "SELECT address, username, auto_promoted_at FROM trader_candidates "
            "WHERE auto_promoted_at IS NOT NULL AND auto_promoted_at != '' "
            "  AND julianday('now', 'localtime') - julianday(auto_promoted_at) <= ?",
            (window_d,),
        ).fetchall()

    for cand in candidates:
        username = cand["username"]
        promoted_at = cand["auto_promoted_at"]
        if not username:
            continue
        with db_module.get_connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl_realized), 0) AS window_pnl "
                "FROM copy_trades "
                "WHERE wallet_username = ? "
                "  AND status = 'closed' "
                "  AND closed_at > ? "
                "  AND julianday('now', 'localtime') - julianday(closed_at) <= ?",
                (username, promoted_at, window_d),
            ).fetchone()
        window_pnl = float(row["window_pnl"] or 0) if row else 0.0
        if window_pnl < -max_loss:
            return True, (
                "circuit_breaker: %s window_pnl=$%.2f (< -$%.2f threshold, "
                "window=%.1fd)" % (username, window_pnl, max_loss, window_d)
            )

    return False, "ok"
