# Changelog

Session-level notes. For full commit history see `git log`.

## 2026-04-12 (Spaet-Nacht) — Merge piff-custom PATCH-023..026 + HEDGE_WAIT parse fix

Pulled piff's full patch series (PATCH-001..026) into main via merge commit `74b66d5`. Piff had merged our Round 4 into his branch (`piff-custom`) and then applied four patch commits with bug fixes to the Round 4 work:

- **PATCH-023** (`27e4b27`): 14 code-review bugs. `brain.py` uses `_current_live_count()` helper (the same fix I did in Round 4 Task 6, lost during his earlier merge). `smart_sell.py` no longer closes DB when price ≥0.95 — retries sell instead to avoid orphan positions. `database/db.py` gets 30s timeout on sqlite3.connect + defensive try/except on `reopen_copy_trade` for UNIQUE constraint violations. `config.py` wires `MAX_COPIES_PER_MARKET_MAP` (was missing — auto_tuner was writing a setting nobody read). `main.py` re-enables `auto_tune_settings()` scheduler call. Also: `ai_analyzer.py`, `auto_backup.py` assorted path fixes.
- **PATCH-024** (`0d6401a`): 7 bugs. Critical: `auto_tuner.py` now MERGES MIN/MAX_ENTRY_PRICE_MAP with existing values and keeps the **tighter** value (higher min, lower max) — preserves brain's BAD_PRICE tightens across the next tuner cycle. Before: auto_tuner overwrote brain's tightens every 2h. `brain._revert_obsolete_tightens` re-reads settings fresh before writing to avoid stale-read races. `trader_lifecycle._add_followed_trader` uses `_seed_tier_defaults` for consistent NEUTRAL tier seed. `config.py` adds `AUTONOMOUS_PAPER_MODE` and `MAX_RESOLVE_HOURS` (both were referenced but never defined).
- **PATCH-025** (`170d0e8`): Auto-tuner call moved from START of `run_brain()` to END (after `_revert_obsolete_tightens`). Otherwise a relaxation from the revert helper gets immediately re-clobbered by the tuner's fresh tier defaults. My Round 4 had the tuner first; piff's ordering is correct. Plus: `_get_max_copies()` wired to all 13 call sites, pending-buy lock+recheck.
- **PATCH-026** (`1187b1a`): `kelly.py` returns 1.0 on zero-loss data (was dividing by near-zero). `autonomous_signals.py` uses `.get("total_pnl", 0)` instead of subscript (KeyError-safe). `copy_trader._reload_maps` now hot-reloads `HEDGE_WAIT_TRADERS` too. Dashboard template fixes.

**Post-merge crash fix** (`673cbac`): After the first restart the bot crashed on every scan with `ValueError: invalid literal for int() with base 10: '90.0'`. Piff's HEDGE_WAIT_TRADERS hot-reload exposed an existing parser at `copy_trader.py:1730` that did `int(parts[1])` — the server's `settings.env` has `HEDGE_WAIT_TRADERS=Jargs:90.0,...` (float strings). Wrapped with `int(float(...))` inside try/except. 18/18 tests still pass, bot restarted cleanly, scans running.

**Philosophy difference noted**: Piff's design keeps all auto-pause/throttle/kick functions log-only (see his "Notes for t0mii" in the PATCH-001..022 section below). Round 4 had re-enabled active `pause_trader()` calls; piff's PATCH-023 re-disabled them while keeping the log output (`[BRAIN] Would pause ... DISABLED — settings managed manually`). Honoring piff's design as source of truth for the pause/throttle layer going forward.

**Also during this session**: `MAX_DAILY_LOSS` set to `0` (unlimited) per user request after the Batch 1 rail of `$10` was hitting immediately on today's -$33 realized losses. `MAX_DAILY_TRADES=30` remains. `STOP_LOSS_PCT=0.25` remains.

---

## 2026-04-12 (Nacht) — Fix-Everything: Brain / Scorer / Lifecycle Round 4

Three deploy batches, 14 commits, all verified live on server (`walter@10.0.0.20`). Git tags: `batch2-deployed`, `batch3-deployed`. Design: `docs/superpowers/specs/2026-04-12-polybot-brain-fixes-design.md`. Plan: `docs/superpowers/plans/2026-04-12-polybot-brain-fixes.md`. Tests: new `tests/` dir with stdlib `unittest` (no pytest dep), 18/18 passing — run via `python3 -m unittest discover tests -v`.

### Batch 1 — Safety Rails (commit `6cc4059`)

- `settings.env`: `MAX_DAILY_LOSS=10`, `MAX_DAILY_TRADES=30` (were both `0` = unlimited). `STOP_LOSS_PCT` unchanged at `0.25` (user-tuned per "352 Trades >25% = -$48" note — not touched).
- `scorer_weights.example.json` + server `scorer_weights.json` initialized with defaults. The trade scorer stops falling back to in-memory `DEFAULT_WEIGHTS` on every call — weights now persist.
- **Verified live**: `[SKIP] Max daily loss reached ($-33.47 <= -$10)` — the daily cap correctly halts new buys when today's realized loss exceeds the threshold. This is safety rail working as designed; if it feels too tight, raise the cap and hot-reload kicks in within ~5s.

### Batch 2 — Code Bugs (7 commits, `1803f35` → `f7f3e7e`)

**Feedback loop wired** (commits `1803f35` + `8994293`, Task 2): Previously `trade_scores.outcome_pnl` was NEVER written — `brain._optimize_score_weights` read zero resolved rows and never tuned thresholds. Added `db.update_trade_score_outcome(cid, trader, pnl)` called from `smart_sell.py` after close + 3 paths in `copy_trader.py` (resolved-at-0.99/0.01 / stop-loss / trailing-stop). Plus `db.backfill_trade_score_outcomes()` called from `outcome_tracker.track_outcomes()` as a periodic sweep. After code review: dropped the original 120-minute window (too short for stop-loss/trailing which fire hours later), match by newest NULL-outcome row; `round(pnl, 2)` replaced with raw `pnl`; bare `except: pass` replaced with `logger.debug`. **Verified live**: first backfill run filled 57 historical trade_scores rows.

**Settings reload revised scope** (commit `e2b0f2c`, Task 3): Plan originally proposed a new dirty-flag + `config.reload()` mechanism, but discovered `bot/copy_trader.py::_reload_maps()` ALREADY exists and uses mtime-based hot-reload of per-trader maps — proven in production logs as `[RELOAD] Settings maps refreshed`. Scope reduced to: (1) remove the stale `auto_tuner.py` "restart recommended" warning (replaced with `[TUNER] Settings written — copy_trader will reload on next scan`), (2) add `FOLLOWED_TRADERS` to `_reload_maps()` so `brain.pause_trader → _remove_followed_trader` takes effect live without restart.

**Brain log dedup** (commit `51c77e7`, Task 4): `brain._classify_losses` was writing one `BLACKLIST_CATEGORY` `brain_decisions` row per loss — production had 357 rows, most duplicates within the same second for the same `(trader, category)` pair. Now `_execute_loss_actions` collapses to unique pairs first, and `_add_category_blacklist` early-returns when the pair is already in `CATEGORY_BLACKLIST_MAP`. Same pattern for `BAD_PRICE` (tighten each trader once per cycle, not once per loss). **Verified live**: 21 `BAD_CATEGORY` losses → 0 new rows (all already blacklisted, early-return).

**Lifecycle bootstrap** (commit `ec71e0d`, Task 5): Primary followed traders (KING7777777, Jargs, aenews2, …) were missing from `trader_lifecycle` because the only writer was `brain.pause_trader`. New `trader_lifecycle.ensure_followed_traders_seeded()` upserts a `LIVE_FOLLOW` row for every entry in `FOLLOWED_TRADERS`. Called once at startup from `main.py` after `init_db()`, and at top of `brain._check_trader_health` every cycle (picks up adds-between-restart). **Verified live**: `[STARTUP] Seeded 2 trader_lifecycle rows` log on bot restart.

**`MIN_LIVE_TRADERS` race** (commit `f7f3e7e`, Task 6): `brain._check_trader_health` read `live_count` ONCE before the trader loop, then used that stale local to gate each pause. Three losing traders could all pass the guard simultaneously and drop live count below `MIN_LIVE_TRADERS=2`. Fix: nested `_current_live_count()` helper re-reads `FOLLOWED_TRADERS` from disk on each iteration, so each pause immediately reflects in the next guard check.

**`signal_performance` bookkeeping** (commit `9035e26`, Task 7): `clv_tracker.update_clv_for_closed_trades` was writing `wins = int(avg_clv > 0)` (global 0/1 boolean) and hardcoded `losses = 0`, plus `total_pnl = avg_clv * 100` (wrong units — CLV percentage, not USDC). Production showed `{trades: 389, wins: 1, losses: 0, pnl: $21.22}` which was obviously invalid. Fix iterates trades, counts per-trade wins/losses on `pnl_realized`, sums `pnl_realized` into `total_pnl`. **Verified live after manual trigger**: `{trades: 389, wins: 276, losses: 107, pnl: +$810.17}`.

**ML time-series split + baseline** (commit `73354a4`, Task 8): `ml_scorer.train_model` was using `train_test_split(random_state=42)` — random i.i.d. split that leaks future trades into training for time-series data. Combined with heavy class imbalance in production (most trades lose), the reported 92.9% accuracy was a mixture of leakage and "always predict loss" baseline. Fix: `_build_training_data` now returns rows sorted `ORDER BY created_at ASC`. `train_model` slices first 80% as training, last 20% as test. Additionally logs `Class balance` and `Baseline` (majority-class accuracy) alongside test accuracy — future-us can now see whether the model is actually learning beyond the baseline. Removed unused `from sklearn.model_selection import train_test_split` import.

### Batch 3 — Design Cleanups (commits `49bb00a`, `4695fb6`)

**Unified trader state reader** (commit `49bb00a`, Task 10): During planning we discovered `trader_status` and `trader_lifecycle` are **orthogonal axes, not duplicates**. `trader_status` (written by `trader_performance.py` every 30min) is a soft-throttle multiplier (0.0/0.5/1.0 = paused/throttled/active) based on 7d PnL thresholds. `trader_lifecycle` (written by `brain.pause_trader`) is a hard pause that removes the trader from `FOLLOWED_TRADERS`. A trader can legitimately be `soft=active` + `hard=PAUSED` at the same time. Plan revised from "merge into one table" to "add unified reader". New `db.get_trader_effective_state(username) -> {hard_status, soft_status, multiplier, is_paused, reasons}` combines both layers. `db.is_trader_paused(username)` is the boolean wrapper. `bot/daily_report.py` switched to the unified helper (line 125). `dashboard/app.py` deliberately NOT touched — 5 call sites, UI regression risk, follow-up work.

**Brain auto-revert** (commit `4695fb6`, Task 11): `BLACKLIST_CATEGORY` and `TIGHTEN_FILTER` decisions were previously permanent — once `KING7777777:dota` was blacklisted, even if KING later had 60% WR in dota the blacklist stayed forever. Over time these accumulated and strangled the bot's trading activity. Added `brain._revert_obsolete_blacklists()` (removes blacklist when 7d cnt>=3 + WR>=50% + PnL>=0) and `brain._revert_obsolete_tightens()` (relaxes MIN/MAX_ENTRY_PRICE one 5c step per cycle toward the current tier default, only for traders with positive 7d PnL). Both called at the end of `run_brain()` after `check_transitions()`. **Verified live**: in the first post-deploy brain cycle, KING7777777 was tightened to 35-80c for 12 BAD_PRICE losses and then **immediately relaxed back to tier default 30-85c** because his STAR tier 7d PnL=+$34 triggers the revert. Good trader keeps the wide range, loss-heavy tightening stays stuck for weak traders.

### Architecture decisions / non-obvious context

- **Three layers write `settings.env`**, with distinct semantics and all pick-up-able by the mtime-based `_reload_maps()` without restart: (1) `auto_tuner` writes per-trader tier-derived values every 2h, (2) `brain` writes targeted loss-reactions (blacklist/tighten/pause/revert/relax), (3) `trader_lifecycle.pause_trader` writes `FOLLOWED_TRADERS` on hard-pause. **The ML model itself does NOT write settings** — `ml_scorer.predict()` only feeds a ±15 bonus/malus into `trade_scorer.score()`, and `brain._optimize_score_weights()` writes `scorer_weights.json` (not `settings.env`) but is dormant because the scorer never produces `BLOCK` actions in current data (scores range 59-88, block threshold is 40).

- **Deliberate YAGNI from this round** (listed here so next session doesn't get surprised):
  - ML model redesign beyond the time-split fix (no feature engineering, no CV, no drift monitoring). Sample size (~600) doesn't justify it.
  - `autonomous_trades` / paper mode — feature disabled, table empty. Not touched.
  - `dashboard/app.py` still reads `trader_status` directly at 5 call sites (works correctly since trader_status is still written; UI risk deferred).
  - Trade-score backfill for the 426 legacy NULL rows — the periodic sweep in `outcome_tracker` handles going-forward, legacy gaps filled opportunistically.
  - `get_score_range_performance` joins on `ts.trade_id IS NOT NULL` but the scorer never passes `trade_id` when logging → brain's "Score range performance" log is empty. Cosmetic (log-only), not functional. Can be fixed by rewriting the query to join on `(condition_id, trader_name)`.
  - DB-PnL vs wallet discrepancy ($810 DB sum of `pnl_realized` vs ~$100 wallet reality). Suspected resolved-to-zero inflation in `pnl_realized` for auto-closed trades. Own investigation needed, not blocking.

### Deploy protocol used

- Batch 1: SCP `settings.env` + `scorer_weights.json` → `sudo systemctl restart polybot` → tail 5 min → commit example files.
- Batch 2: SCP 10 Python files → `python3 -m py_compile` on server → restart → manual `from bot.brain import run_brain; run_brain()` to exercise all Batch 2 fixes end-to-end → verified `[RELOAD]`, `[LIFECYCLE] Seeded`, brain_decisions dedup, unified reloaded settings → tag `batch2-deployed`.
- Batch 3: SCP `database/db.py` + `bot/brain.py` + `bot/daily_report.py` → compile check → restart → manual brain run → saw `[BRAIN] Relaxed KING7777777` firing as auto-revert → tag `batch3-deployed`.
- All commits via `git push origin main` (14 commits + 2 tags). GitHub bypass-rule notice is expected per user workflow (main-only, no PR gate).

### Known post-deploy state

- Bot is running `active`, portfolio ~$100.75. `MAX_DAILY_LOSS=$10` daily cap is currently holding — today's realized loss is $33.47 so `[SKIP] Max daily loss reached` fires every scan until midnight resets the counter. This is the new safety rail working as intended; raise the cap in `settings.env` if you want it looser (hot-reload picks it up in ~5s).
- `FOLLOWED_TRADERS` has shrunk from 5 to 3 live (Jargs, sovereign2013, KING7777777) after brain paused xsaghav (-$129) and fsavhlc (-$21). `MIN_LIVE_TRADERS=2` was enforced correctly.
- `trade_scores.outcome_pnl` now populated for 57 historical rows; going forward every close writes its outcome back directly.
- `ml_model.pkl` retrained at 20:35 under the new time-split + baseline-logging pipeline.

---

## 2026-04-12 (Abend) — WS noise, auto_backup, Auto-Tuner refactor

### Why no trades accepted during a 2h window — root cause investigation
Symptom: no new copy trades between 15:35 and 17:54 despite bot running normally.

Findings:
- **KING7777777** (STAR tier): actually inactive for 2h — no new trades from the trader, nothing to copy.
- **aenews2** (NEUTRAL): only made a SELL during the window, which doesn't create a new position.
- **Jargs** (WEAK): made exactly one new BUY at 17:53:53 UTC ("Open Capfinances Rouen Metropole: Hailey", $1198, 81c). Correctly detected by the scanner and logged as `[NEW]` — then rejected by the price-range filter because Jargs' WEAK tier range is 42-70c.

This is **working as designed** but exposed a deeper problem with the tier-default approach (see Auto-Tuner section below).

### Jargs price-bucket analysis (16 historical trades)

| Price Bucket | Trades | P&L | Tier allows? |
|---|---|---|---|
| 42-50c | 3 | **-$5.47** | ✅ yes |
| 50-60c | 4 | +$1.03 | ✅ yes |
| 60-70c | 1 | +$0.37 | ✅ yes |
| 70-85c | 3 | +$0.63 | ❌ blocked |
| >85c | 1 | +$0.21 | ❌ blocked |

The WEAK tier default (42-70c) allows the **only losing bucket** and blocks all profitable higher-price entries. A tier-based filter cannot fix this — it needs per-trader data analysis.

### Auto-Tuner tier defaults → settings.env (`7153673`)

`bot/auto_tuner.py` previously had a hardcoded `TIERS` dict with 5 tiers × 10 fields. Changing any value meant editing Python and restarting.

Moved all 10 fields to `settings.env` as `TIER_*` MAP lines (format: `tier:value,tier:value`):

```
TIER_BET_SIZE, TIER_EXPOSURE, TIER_MIN_ENTRY, TIER_MAX_ENTRY,
TIER_MIN_TRADER_USD, TIER_TAKE_PROFIT, TIER_STOP_LOSS,
TIER_MAX_COPIES, TIER_HEDGE_WAIT, TIER_CONVICTION
```

`_load_tiers()` reads them on every `auto_tune()` call → changes take effect on the next 2h cycle without a restart. Hardcoded `_TIER_DEFAULTS` in the module remain as fallbacks so missing entries (or an entirely absent line) keep working.

Classification thresholds (`pnl_7d > 5 and wr > 55 → star`, etc.) in `_classify_trader()` are **still hardcoded** — separate follow-up.

**Limitations** — the tier approach is fundamentally a lookup table, not real auto-tuning. A future refactor (Option C) should compute profitable price buckets **per trader** from their own history and use those instead of tier defaults, with a minimum-sample-size guard.

### WebSocket price tracker noise fix (`de89dd3`)

`bot/ws_price_tracker.py` was reconnecting ~117 times per hour, producing 231 WARNING lines per 12 minutes and flooding the dashboard `/logs` view.

Root cause: when there were no open positions, `_on_open` sent an empty subscription payload. Polymarket's server closed the idle connection immediately, triggering a fixed 10s reconnect loop.

Fix:
- `_has_work_to_do()` checks `_condition_map`, `_pending_tokens`, or the DB (`copy_trades WHERE status='open' AND condition_id != ''`). Sleeps 30s when idle instead of reconnecting.
- Exponential backoff 10s → 20s → 40s → 60s (capped). Resets to 0 whenever `_last_successful_event_ts` is recent (set in `_on_message`).
- `_on_error` / `_on_close` downgraded to DEBUG. WARNING only surfaces after 5+ consecutive failures, so real outages still escalate.
- `copy_trader.py` `[WS] Price tracker disconnected` warning downgraded to DEBUG — the WS is idle-by-design when there are no open positions, and entry pricing uses HTTP anyway.

**Verified**: 0 WARN/ERR in 400 log lines after deploy (previously ~19/minute).

### auto_backup graceful skip (`f6318e7`)

`bot/auto_backup.py` tried to push to a `piff` remote / `piff-custom` branch every 6h. Neither exists on the production server, so every run logged a WARNING about `src refspec piff-custom does not match any`.

Fix: added `_remote_exists()` and `_local_branch_exists()` guards that return silently with a DEBUG log when the remote/branch isn't configured on this host. The module resumes working automatically if the remote gets added later.

### Current state (as of 2026-04-12 evening)
- Portfolio: **$101.13** ($84.66 wallet + $16.48 positions).
- Active followed traders: **3** — Jargs (WEAK), KING7777777 (STAR), aenews2 (NEUTRAL).
- Historical auto-tuner stats still track xsaghav / sovereign2013 / fsavhlc but they're no longer followed.

---


## 2026-04-12

### Bug Fixes

- **PATCH-001**: Fix missing `import os` in `bot/trader_performance.py` — caused NameError crash when scheduler runs `update_adaptive_stop_loss()`
- **PATCH-002**: Fix `OrderBookSummary` dataclass access in `bot/liquidity_check.py` — was treated as dict, causing AttributeError and bypassing all liquidity checks silently
- **PATCH-003**: Add missing `X-Dashboard-Key` auth header to report fallback fetch in dashboard — `/api/report/latest` was always returning 403
- **PATCH-004**: Remove unused API call in `bot/wallet_scanner.py` — `act_resp` from `/activity` endpoint was fetched but never used, wasting requests
- **PATCH-006**: Use developer helper `_get_attr_or_key()` for orderbook level access in `bot/liquidity_check.py` — supports both dataclass and dict format
- **PATCH-009**: Fix hidden `-$10` auto-pause threshold in `bot/brain.py` — was pausing traders independently from lifecycle
- **PATCH-011**: Fix third hidden `-$10` throttle in `bot/trader_performance.py` — `THROTTLE_PNL_7D` was auto-throttling traders at `-$10`

### Infrastructure

- **PATCH-005**: Improved `auto-update.sh` with syntax checks before restart, 30s health check after restart, automatic rollback on service crash
- **GitLab migration**: Repo moved from GitHub to `gitlab.com/piff.patrick/polymarket-copy-bot`, auto-update cron every 15 min fetches from upstream (GitHub), merges with `-X ours`, pushes to GitLab

### Settings Management

- **PATCH-008**: Raised lifecycle pause/kick thresholds (`-$20`/`-$50` instead of `-$10`/`-$30`)
- **PATCH-010**: Full settings reset — all 6 traders equal baseline (3% bet, 10% exposure, 0.3 conviction, 30% SL, 150% TP, no category blacklists)
- **PATCH-013**: Disabled auto-pause/remove in `trader_lifecycle.py`, `brain.py`, and `trader_performance.py` — settings now managed manually, functions still log but do not modify `settings.env`
- **PATCH-016**: Disabled `auto_tuner.py` hardcoded tier table — was overwriting all settings every 2h with rigid star/solid/neutral/weak/terrible tiers

### Discovery Scanner

- **PATCH-012**: Enhanced leaderboard scan — now scans 4 time periods (ALL/30d/7d/1d) to find both established and rising traders. Increased `MAX_CANDIDATES` from 50 to 100, lowered whale scanner thresholds

### Dashboard

- **PATCH-007**: ML Model update
- **PATCH-014**: ML Model update + settings stabilization
- **PATCH-015**: Trader Power Levels now shows traders with 0 trades (removed `trades_count > 0` filter)
- **PATCH-017**: Added copied trades count next to trader name, added 1d P&L/WR/trades row to trader cards

### Notes for t0mii

- All auto-pause/throttle/kick functions are **disabled** (log-only). We manage settings manually based on performance data. If you re-enable them, they will override our settings.
- The `auto_tuner.py` hardcoded `TIERS` dict is disabled. Consider making tiers configurable via `settings.env` instead of hardcoded.
- `_remove_followed_trader()` in `trader_lifecycle.py` is disabled because it rewrites `settings.env` and destroys other map settings when removing a trader.
- Upstream merges use `-X ours` strategy — our changes take priority on conflicts.
