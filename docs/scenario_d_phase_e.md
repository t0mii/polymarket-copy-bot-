---
name: Scenario D Phase E — Verify + Activate Auto-Promotion (canonical state)
description: Current canonical state as of 2026-04-15 evening. Phase A-γ is code-complete (11 commits today, 166 tests green, 7/7 invariants met). Phase E is the verification + activation path. Three blockers found in audit, one-line user decisions captured, E.1-E.7 phased rollout, rollback criteria. READ FIRST for any Scenario D follow-up work.
type: project
originSessionId: 7cd641ee-c4e5-4c9a-8dca-f184ddeb6513
---
## Status summary

**Phase A through γ.6 shipped today** in 11 commits. All code exists for:
- Paper-trade dedup (signature, A2)
- Sort-starvation fix (rotation cursor, A3)
- Orphan whale cleanup SQL (A1)
- Schema migrations for B1/B2 columns (B0)
- Paper resolution tracker with Gamma + side-aware pricing (B2)
- Shared filter helper `bot/trader_filters.py` (B1a+B1b) — paper and live now call the same pre-score filter chain via `apply_pre_score_filters_live()`, live uses `run_scorer=False` to keep inline scorer at line 2152
- Promotion gate evaluator `bot/promotion.py::evaluate_promotion` with 6 branches (insufficient_trades / low_win_rate / weak_wilson_lb / low_roi / below_abs_pnl_floor / stale), plus cooldown + circuit breaker + probation state (γ.1-γ.5)
- Probation bet sizing wired into `_calculate_position_size` (γ.5b)
- `/api/upgrade/promotion-dryrun` endpoint + dashboard panel at `/brain` (γ.6)

**What's still OFF**: `AUTO_DISCOVERY_AUTO_PROMOTE=false` in walter's settings.env. Master flag. Everything else is ready to fire.

**Goal**: flip the flag safely such that the pipeline auto-discovers, auto-promotes, auto-starts-probation, and auto-follows new traders under the circuit breaker + probation guardrails, with zero manual intervention.

## Audit findings from 2026-04-15 18:35

Walter state at the audit:
```
paper_trades    closed=1559, open=143
trader_candidates  observing=36, promoted=3, inactive=62
FOLLOWED_TRADERS  Jargs, xsaghav, sovereign2013, KING7777777, fsavhlc (5, unchanged)
scheduler jobs   track_paper_outcomes_job registered, discovery_scan running every 3h
B2 tracker      Manual invocation: "Updated 124 rows (0 resolved, 124 still-open price-refresh)"
                87% Gamma API success rate, 0 markets resolved (all still active)
```

### The three blockers that prevent flag flip

**Blocker 1 — Fake-loss contamination on 1559 historical rows**

All 1559 `closed` paper_trades have `close_reason=''` and the fingerprint `current_price = entry_price * 0.95` with `pnl ≈ -0.05`. These were closed by the pre-B2 `entry * 0.95` fallback code and carry an artificial 5% loss each. Candidate WRs (swisstony 28.6%, RN1 20.4%, GamblingIsAllYouNeed 18.1%, ImJustKen 32.1%) are dominated by this fake-loss noise, not real trader signal. The promotion gate is mathematically impossible to clear under contaminated data.

**Blocker 2 — Stale cooldown entry in activity_log**

Row dated 2026-04-13 00:08:37 with `event_type='promotion'` — the orphan-whale promotion from before the gate existed. `promotion_cooldown_active()` reads the latest promotion event and blocks new promotions if younger than `PROMOTE_COOLDOWN_DAYS=7`. Currently ~2.7 days old → cooldown tripped until ~2026-04-20.

**Blocker 3 — Insufficient clean post-B2 data**

B2 deployed ~17:19 UTC today. Only 2-3 hours of clean data exists. Top candidates have 0-5 post-B2 paper_trades each. At the current trade frequency it would take 1-2 weeks to organically reach `PROMOTE_MIN_PAPER_TRADES=100` for even the most active candidates.

## User decisions (2026-04-15 evening, locked in)

1. **Cleanup via stats-cutoff filter**, not delete or retroactive re-evaluation. New env var `PROMOTE_STATS_CUTOFF='2026-04-15 17:19:00'` makes `get_candidate_stats`, `compute_dry_run`, and the recency query in `check_promotions` filter out rows with `closed_at < cutoff`. Non-destructive, reversible (unset env → filter off), preserves full audit trail in the table. Ship tonight.

2. **Stepped threshold unlock**: temporarily
   ```
   PROMOTE_MIN_PAPER_TRADES=30
   PROMOTE_MIN_WILSON_LOWER=0.55
   ```
   in walter's settings.env (hot-reloaded via dotenv). All other gates stay at strict defaults. Ships tonight. Tightens back to `n=100 / wilson=0.50` after the first auto-promotion graduates from probation without tripping circuit breaker.

## Phase E phases (execute in order, each gated on the previous)

### E.1 — Stats-cutoff filter (tonight, code + deploy)

Files to modify:
- `config.py` — `PROMOTE_STATS_CUTOFF = os.getenv('PROMOTE_STATS_CUTOFF', '').strip()`
- `database/db.py::get_candidate_stats` — add `AND closed_at >= ?` conditional clause based on cutoff
- `bot/promotion.py::compute_dry_run` — same conditional in the LEFT JOIN condition
- `bot/auto_discovery.py::check_promotions` — same filter in the `SELECT MAX(created_at)` recency query
- `tests/test_promotion_dryrun.py` — 3 TDD tests: excludes pre-cutoff, empty cutoff = no filter, both paths return same count
- CHANGELOG.md entry with piff-sync section

Deploy checklist:
- Pre-deploy DB snapshot on walter
- scp the 4 modified files + test file
- Update walter's settings.env with `PROMOTE_STATS_CUTOFF='2026-04-15 17:19:00'`
- `sudo systemctl restart polybot`
- Verify dry-run endpoint returns mostly `n_trades=0` (only 2-3h of post-cutoff data)
- Full local regression (169 tests target)

### E.2 — Stepped threshold relaxation (tonight, settings.env only, no code)

Add to walter's settings.env:
```
PROMOTE_MIN_PAPER_TRADES=30
PROMOTE_MIN_WILSON_LOWER=0.55
```
Hot-reloaded — no restart needed if E.1 already restarted. No code change (the helpers read these values at call time via `getattr(config, ...)`).

### E.3 — Cooldown reset (tonight, SQL)

On walter:
```bash
ssh walter@10.0.0.20 'cd /home/walter/polymarketscanner && venv/bin/python3 -c "
import sqlite3
c = sqlite3.connect(\"database/scanner.db\")
c.execute(\"DELETE FROM activity_log WHERE event_type=\\\"promotion\\\" AND created_at < \\\"2026-04-15\\\"\")
c.commit()
print(\"deleted\", c.total_changes, \"promotion rows\")
"'
```

Surgical — only removes pre-today `promotion` events. Snapshot the rows first for audit.

### E.4 — Observation window (24-48 hours)

Autonomous period. Monitor:
- Fresh `close_reason != ''` rows accumulating via `close_paper_trades` (24h time_cutoff)
- `track_paper_outcomes_job` finding resolved markets (`close_reason='resolved_yes'/'resolved_no'`)
- Dashboard `/brain` dry-run panel showing `would_promote=true` rows

Minimum data bar for E.5 flag flip:
- ≥ 1 candidate with `would_promote=true`
- Cooldown + circuit breaker both clear
- Manual sanity-check on the candidate (strategy compatible with our filters, reasonable bet sizes)

Failure mode: if no candidate passes after 48h, either diagnose the 13% Gamma-null cid rate OR drop `PROMOTE_MIN_PAPER_TRADES=20`.

### E.5 — Flag flip (after observation passes)

Pre-flip checks (script this):
```bash
curl -s http://localhost:8090/api/upgrade/promotion-dryrun | jq '{
  wp: (.candidates | map(select(.would_promote)) | length),
  cd: .cooldown_active,
  cb: .circuit_breaker_halted
}'
```
Want: `wp >= 1, cd == false, cb == false`.

Then:
```bash
# walter settings.env
AUTO_DISCOVERY_AUTO_PROMOTE=true

sudo systemctl restart polybot
```

Restart is mandatory — `config.AUTO_DISCOVERY_AUTO_PROMOTE` is imported at module load in `bot/auto_discovery.py`, not hot-reloaded.

### E.6 — Post-flip monitoring (24-72h after first auto-promotion)

Watch for:
- First `[PROBATION] <name>: $X.XX -> $Y.YY (mult=0.50, cap=$5.00)` log line
- `probation_trades_left` counting down from 20
- `activity_log` promotion entry → next cooldown until +7d
- No error tracebacks in polybot journal
- No circuit breaker trips (first 7d net pnl ≥ -$10)

Rollback triggers during monitoring:
- Any error traceback in promotion path → immediate `AUTO_DISCOVERY_AUTO_PROMOTE=false` + restart
- Circuit breaker trips → investigate
- Probation trader causes >$15 loss in 72h → manual unfollow

### E.7 — Tightening (after first successful graduation)

When the first auto-promoted trader graduates from probation (14 days OR 20 trades, whichever first), without tripping circuit breaker, with net ≥ $0 live pnl:

```
# settings.env
PROMOTE_MIN_PAPER_TRADES=100
PROMOTE_MIN_WILSON_LOWER=0.50
```

Steady-state defaults. Next auto-promotion then requires much stronger evidence, which is intended.

## Rollback cheat sheet

| Phase | Rollback | Time |
|---|---|---|
| E.1 | Unset `PROMOTE_STATS_CUTOFF` in settings.env + restart. Or git revert E.1 commit. | < 1 min |
| E.2 | Unset `PROMOTE_MIN_PAPER_TRADES` + `PROMOTE_MIN_WILSON_LOWER`, defaults from config.py reload. | hot-reload |
| E.3 | Stale row is gone; re-insert manually if somehow needed (no reason to). | n/a |
| E.5 | `AUTO_DISCOVERY_AUTO_PROMOTE=false` + restart. Auto-promoted traders stay in FOLLOWED_TRADERS unless manually removed via `scripts/remove_followed_trader.sql` + settings.env edit. | < 1 min |

## What NOT to do during E phase

- Do NOT re-run the legacy `entry * 0.95` code path (the close_paper_trades refactor removed it, confirm you're running post-B2 code before making changes)
- Do NOT delete rows from paper_trades (cutoff filter preserves them as audit trail)
- Do NOT lower thresholds without observation (E.2 is calibrated; further relaxation only if E.4 fails after 48h)
- Do NOT flip the flag without the pre-flip checks passing
- Do NOT touch `_remove_followed_trader` — still disabled per piff-philosophy

## Related memory

- `project_backfill_status.md` — architecture history, updated with 2026-04-15 evening 11-commit scope
- `project_promotion_criteria.md` — original critique + now the stepped-unlock rationale
- `feedback_staged_rollout.md` — evidence-gated autopilot principles, stats-cutoff pattern added
- `feedback_piff_philosophy.md` — auto-pause/throttle/kick stay disabled
- `feedback_changelog.md` — CHANGELOG is piff sync channel, every session must update
- `project_ops.md` — walter paths, deploy via scp, credential refs
