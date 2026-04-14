"""Verified per-bucket PnL → MIN/MAX entry price window.

Replaces the auto-tuner's win-rate-based tier defaults for
MIN_ENTRY_PRICE_MAP / MAX_ENTRY_PRICE_MAP with a magnitude-aware
compute from each trader's own verified history. Buckets are 10c wide.
A bucket is "good" if it has enough samples AND a PnL above threshold.
The returned range spans the lowest to highest good bucket; bad buckets
in the middle are absorbed since MIN/MAX can't express disjoint ranges.

Returns None when data is insufficient, signaling the caller to fall
back to the tier default rather than write a spurious range.

Known limitation (accepted tradeoff):
  The "min to max of good buckets" heuristic can produce a suboptimal
  window when the good buckets are sparse and a large-loss bucket
  sits in the middle. Example: good={1, 3, 4, 5, 7} with bucket 2 at
  pnl=-$17 yields (0.10, 0.80) which includes the bad bucket 2. A
  Kadane-style max-PnL-window compute would select a tighter range
  that excludes bucket 2. The Kadane version is ~$3 better per 53
  trades on the current KING7777777 distribution — small enough that
  the simpler algorithm is retained for clarity. If the suboptimality
  ever grows noticeable in live data, replace the `good`-set logic
  with a Kadane enumeration over `(i, j)` pairs maximizing bucket
  sum with `j > i` constraint.

Sample-size guardrail:
  The caller is expected to pass `min_total_trades` matching their
  confidence level. Auto-tuner currently passes 100 (conservative,
  only trusts large samples) but the function's internal default is
  20 for standalone callers. Rationale: at 100 samples per trader,
  per-bucket n ≈ 15-25 which is where bucket means start stabilizing.
  At 20 samples the per-bucket n ≈ 3-5, vulnerable to noise — small
  hot streaks can produce false-positive windows. For the current
  $106 equity and -$75/cycle worst-case blast radius on a wrong
  2-sample bucket, 100 is the right threshold for now; it can be
  lowered staged over weeks as live data accumulates.
"""
from typing import Optional, Tuple


def compute_verified_price_range(
    db_module,
    trader_name: str,
    min_samples_per_bucket: int = 2,
    min_bucket_pnl: float = -2.0,
    min_total_trades: int = 20,
) -> Optional[Tuple[float, float]]:
    """Compute (min_price, max_price) from verified per-bucket PnL.

    Returns None if:
    - fewer than min_total_trades verified trades total
    - fewer than 2 "good" buckets (range too narrow)
    """
    with db_module.get_connection() as conn:
        rows = conn.execute(
            "SELECT actual_entry_price, entry_price, actual_size, size, usdc_received "
            "FROM copy_trades "
            "WHERE wallet_username=? AND status='closed' "
            "  AND usdc_received IS NOT NULL AND actual_size IS NOT NULL",
            (trader_name,),
        ).fetchall()

    if len(rows) < min_total_trades:
        return None

    buckets: dict[int, dict] = {}
    for r in rows:
        p = r["actual_entry_price"] or r["entry_price"] or 0
        if p <= 0 or p >= 1:
            continue
        size = r["actual_size"] or r["size"] or 0
        pnl = (r["usdc_received"] or 0) - size
        bk = int(p * 10)
        if bk not in buckets:
            buckets[bk] = {"n": 0, "pnl": 0.0}
        buckets[bk]["n"] += 1
        buckets[bk]["pnl"] += pnl

    good = sorted(
        bk for bk, b in buckets.items()
        if b["n"] >= min_samples_per_bucket and b["pnl"] > min_bucket_pnl
    )

    if len(good) < 2:
        return None

    return (good[0] / 10.0, (good[-1] + 1) / 10.0)
