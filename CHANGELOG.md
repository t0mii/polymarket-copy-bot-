# Changelog

Session-level notes. For full commit history see `git log`.

## 2026-04-14 (even later) — Brain paper/candidates visibility fix

Diagnosis: `/api/brain/paper-traders` only queried `trader_lifecycle WHERE status='PAPER_FOLLOW'`, which had **0 rows** on the live server because piff's PATCH-038c runs auto-discovery through `trader_candidates` (status `observing`/`promoted`), not through the lifecycle table. Meanwhile `/api/upgrade/candidates` filtered only by `status='observing'`, hiding the 3 actively paper-trading `promoted` candidates (0x3e5b23e9f7 +$20.61, Dropper +$8.80, aenews2 +$0.41). And the brain frontend capped the top-candidates render at 12 rows. Net effect: the user saw an almost-empty Paper Trading panel even though the bot had already generated 5456 paper trades across 39 candidates.

### Fixes

- `dashboard/app.py::api_paper_traders`: now unions two sources. Primary is `trader_candidates WHERE paper_trades > 0 AND status IN ('observing','promoted')`. Secondary is `trader_lifecycle WHERE status='PAPER_FOLLOW'` (for the case where the lifecycle state eventually catches up). Duplicates collapse on address. For candidate-sourced rows without a `status_changed_at` timestamp, `days_in_status` is derived from the oldest paper trade so "days in paper" stays meaningful. Sorted by `paper_pnl` desc.
- `dashboard/app.py::api_candidates`: now returns `status IN ('observing','promoted')` sorted by `profit_total` desc, limit 100. The 3 promoted whales finally show up on the Top Candidates list.
- `dashboard/templates/brain.html::renderCandidates`: slice cap raised from 12 → 30.
- `dashboard/templates/brain.html::renderPaperSummary`: state-badge color logic handles both lifecycle states (`PAPER_FOLLOW` / `LIVE_FOLLOW` / `KICKED`) and candidate states (`OBSERVING` / `PROMOTED`).
- `dashboard/templates/brain.html::renderCoreMini`: Active Traders counter now pulls from `/api/upgrade/trader-performance` (the real followed-traders list) instead of `status.trader_status` (which only held soft-throttle rows — that's why it showed 3/3 even though the roster has more).

### Live verification

Server now returns 32 paper-traders and 39 candidates (36 observing + 3 promoted) through the brain page, with the top performers visible: `0x3e5b23e9f7` +$20.61 / 88 trades / 87.5% WR / 1.6d, `Dropper` +$8.80 / 118 trades / 59% WR / 1.2d.

## 2026-04-14 (still later) — Logs filter precision + brain paper-trades list

### `/logs` — bracket-tagged filter patterns + stable scroll

Two follow-ups to the earlier server-side filter work:

- **Filter patterns now use bracket tags** so `[INFO]` lines that happen to contain the word "filter" as a substring no longer sneak in. FILTERED passes `[filter],[skip]`; BUYS passes `copy trade,order buy:,[new]`; SELLS passes `order sell:,[fast-sell],[auto-sell],[stop-loss],[take-profit]`; HEDGE passes `[hedge-wait]`; EVENT-WAIT passes `[event-wait]`; CLOSES passes `[auto-close],[miss-close],closed (,resolved`; ERRORS passes `[error],[warning],fehler`; PORTFOLIO passes `[snapshot],portfolio value,starting balance`. On live server with the old substring match FILTERED matched 8180 rows (lots of INFO false positives); with bracketed tags it matches 2146 true filter/skip events.
- **Scroll no longer jumps** during the 3s re-render. `render()` now saves `scrollTop` and a "was-at-bottom" flag before replacing the log box contents, then restores the saved position (or scrolls to bottom if the user was already there). An `_ignoreScroll` guard prevents the scroll event listener from flipping `autoScroll` during programmatic scroll.

### `/brain` — paper trading list at the bottom

New panel under Top Candidates: a flat per-trade table of the `paper_trades` DB rows joined with `trader_lifecycle` + `trader_candidates` for username and lifecycle state. Shows time (Vienna), trader (linked to polymarket profile), lifecycle state, market question, side, entry price, current price, PnL, and open/closed status. This is the proving-ground phase — promoted candidates start at PnL 0 and have to prove themselves under live settings. 3 days positive → LIVE_FOLLOW, 7 days without progress → KICKED.

New endpoint `dashboard/app.py::api_paper_trades_list` returns the joined list with graceful fallback when the table doesn't exist (local dev DBs). Wired into `brain.html::refresh` + new `renderPaperTrades()`. Live verification showed 10+ paper trades on the server (e.g. DISCOVERED candidate on "Miami Marlins vs. Atlanta Braves @ 56c").

## 2026-04-14 (later) — Logs server-side filter + settings page completeness

Two small but high-impact fixes on the secondary dashboard pages.

### `/logs` — sparse filter buttons now work

Before: `logs.html` polled `/api/logs?lines=500` every 3s and filtered client-side. When the user clicked FILTERED / ERRORS / HEDGE / CLOSES, the browser scanned the 500-line tail — and since > 98% of those lines are `[INFO]` spam from `apscheduler` / `werkzeug` / scan loops, the sparse event was usually nowhere in the window. Zero matches displayed, for events that had definitely happened.

Fix is split across backend and frontend:

- **`dashboard/app.py::api_logs`**: the old `lines * 3` heuristic is replaced by a real reverse scan. Query params are `lines` (default 500, cap 5000), `filter` (comma-separated, case-insensitive substring match), `scan` (default 20k, cap 200k). Without a filter the endpoint tails the last `lines` rows. With a filter it walks back across up to `scan` recent rows and returns the last `lines` that matched. Response now includes `{total, scanned, matched}` so the UI can show "N matched of M scanned".
- **`dashboard/templates/logs.html`**: `fetchLogs()` builds its URL dynamically and appends `&filter=...&scan=100000` whenever a button is active. `setFilter()` now clears the stale buffer and triggers an immediate refetch instead of only re-rendering. `render()` no longer re-filters client-side — the free-text search box stays client-side so it keeps reacting without a round trip. Line-count display now reads `42 / 42 lines · 8180 matched of 100000 scanned` when a filter is active.

Server verification against live `scanner.log` (≈100k lines, ~98% INFO): `filter=filter,skip&scan=100000` returned 500 filtered rows out of 8180 real matches (vs. 0 before). `filter=error,warning` returned 241 rows. Free-text search on top of the filtered view still narrows further.

### `/wallets` Settings page — 3 missing keys, 22 mis-categorised

Audit found three keys that the bot actually honours but which the `/api/settings` endpoint was not returning:

- `HEDGE_WAIT_SECS` — default wait before copying (hedge detection, 60s default)
- `HEDGE_WAIT_TRADERS` — per-trader override map
- `MAX_FEE_BPS` — max market fee in bps (0 = disabled; 500 caps at 5% so esports at 1000 bps get skipped)

These now appear in the appropriate sections. Additionally, 22 keys that the API already returned were falling into the `cats` map's "core" fallback in `index.html` (e.g. `MAX_DAILY_LOSS` under Core instead of Risk, `PRICE_MULT_*` under Core instead of Filter, `CB_THRESHOLD` under Core instead of Risk). The cats map now covers: `MAX_FEE_BPS`, `MAX_ENTRY_PRICE_CAP`, `MIN_FILL_AMOUNT`, `QUEUE_DRIFT`, `EVENT_WAIT_MAX_SECS`, `TRADE_SEC_FROM_RESOLVE`, `PRICE_MULT_HIGH/MED/LOW` → filter; `AVG_TRADER_SIZE_MAP` → size; `MAX_DAILY_LOSS`, `MAX_DAILY_TRADES`, `MISS_COUNT_TO_CLOSE`, `CB_PAUSE_SECS`, `CB_THRESHOLD`, `CASH_RECOVERY`, `CASH_RESERVE`, `SAVE_POINT_STEP` → risk; `ENTRY_SLIPPAGE`, `DELAYED_BUY_VERIFY_SECS`, `DELAYED_SELL_VERIFY_SECS`, `FILL_VERIFY_DELAY_SECS` → exec.

Server now returns 72 settings (was 69). Trader list still sources from `db.get_followed_wallets()` which is the same source `bot/copy_trader.py` uses at runtime, so no trader mismatch.

### Files touched

- `dashboard/app.py` — `api_logs` rewritten, 3 entries added to `api_settings`
- `dashboard/templates/logs.html` — fetchLogs/setFilter/render filter flow rewired
- `dashboard/templates/index.html` — `cats` mapping expanded by 22 entries

Tier-based settings (`TIER_*`) remain intentionally hidden on this page — they live in the Brain panel's auto-tuner view.

## 2026-04-14 — Dashboard unification + Brain console rewrite

All dashboard pages now share a terminal-styled chrome, and the Brain page has been rebuilt from scratch around a live decision/event stream and an event-driven neural-network visualization.

### Shared chrome (all pages)

New shared assets drive the look on every page:

- `dashboard/static/terminal.css` — CSS variables, body/scanline/grid backdrop, header layout, nav, ticker, card/metric styles, scrollbars, popup-slot. Matches `dashboard.html` exactly (extracted from its inline CSS) and adds wide-mode header compression (`html.wide .hd { padding:4px 0 }` etc.) so every page reacts to the Desktop toggle identically.
- `dashboard/static/terminal.js` — digital clock, status-dot polling (`/api/upgrade/status`), live ticker, single-slot PopupManager (SSE client to `/api/stream`, new events overwrite current popup, 8s auto-dismiss), web-audio Sound toggle (persisted in localStorage), WideMode toggle that defaults to Desktop on screens > 900px.

`_nav.html` reordered to `Dashboard → Brain → DailyReport → Settings → Logs` with DailyReport opening in a new window (`target="_blank"`). All page templates (`dashboard.html`, `brain.html`, `logs.html`, `index.html`, `reports.html`) link `terminal.css` and include `terminal.js` + `<div class="popup-slot">`, and their headers carry the same structure: logo image + "Poly Copybot / by Super Sauna Club" + digiclock + LIVE tag + BOT/API/POLL dots + Sound + Desktop/Mobile toggle + nav. Page-specific CSS stayed inline where it makes sense (logs colorizer, reports newspaper fonts, dashboard ticker+wide+positions logic).

The live ticker on every page now fetches `/api/live-data` and renders open trades exactly like `dashboard.html` — `#ID  🎾 ATP  MarketQuestion  [SIDE @ PRICE]  +$PnL` — using the same `dSp()` keyword map ported into `terminal.js` so sport emojis and labels are identical across the site.

### Brain page (full rewrite)

The old Brain page (achievements/power levels/smartphone chat panel/gamification) is gone. `dashboard/templates/brain.html` is a single-page intelligence console organized into two clearly-separated sections:

**Neural Core** — brain status:
- Hero panel with organic-brain SVG (~25 nodes, cyan→gold fold path, pulsing), "Neural State" readout (LEARNING / EXECUTING / OBSERVING / SCOUTING / IDLE) with state-specific colors and blinking cursor, plus an `#heroStats` grid showing uptime / last cycle (Vienna-time) / next cycle / PERFORMANCE_SINCE cutoff.
- 4 growing counters beside the hero (Trades Scored, Brain Decisions, ML Samples, Candidates) with live Schreiber-Kurve sparklines — each is an SVG polyline oscilloscope sweep that ticks every 300ms, glowing head dot at the leading edge, dashed baseline, grid overlay, resets to the left when the buffer fills. Current value is overlaid in the corner of the sparkbox (`SCORED 12.891` etc.).
- Mini-cards row (left) + ML Model Health panel (right) in a 1.4fr/2fr grid that matches the hero/counter column ratio above, so the whole Neural Core section shares a vertical alignment axis. Mini-cards show Decisions Today, Active Tightens, Blacklists, Active Traders, Paused, Promoted, Observing, Auto Trades, Auto Open. ML Health shows Train Acc / Test Acc / vs Baseline metric cells + feature-importance bars with neon shimmer + last-trained timestamp.
- **Brain Stream + Neural Network** as a side-by-side row (1.7fr/1fr). The stream merges `brain_decisions` + `blocked_trades` (last 72h, 500 rows) + `/api/fun/ticker` events into a unified log rendered in a compact `logs.html`-style box. Filter buttons (ALL / DECISIONS / BLOCKED / TRADES / TIGHTEN / RELAX / BLACKLIST / PAUSE / BOOST). SSE live-appends new events with a gold flash animation. Row times are converted from UTC (server tz) to Europe/Vienna via `Intl.DateTimeFormat`. The neural network next to it is a 6-layer SVG that fires packet-flow animations only on real events (no continuous random pulsing) — `brain_decision` spawns 6 gold packets, `new_trade` spawns 4, `trade_closed` spawns 3 colored by W/L. Input labels on the left (trader_edge, category_wr, price_sig, conviction, market_q, correlation), output labels on the right (BUY / SKIP), live prediction pill in the panel title that updates on each event.

**Battlefield** — live results:
- Field mini-cards (16 metrics: 7d/1d P&L, Win Rate, Best/Worst Trader, Best/Worst Category, Paper P&L, Fee Drag, Slippage, Break-even WR, Stop Loss, etc.).
- Scorer Weights + Score Buckets + Brain Action Counts as a trio-grid panel.
- Per-Trader Intelligence table with power levels (Legendary / Epic / Rare / Common glow effects), streak emojis (🔥 for 3+ wins, 💀 for 3+ losses, ⭐/❌ for singles, on-fire animation), tier badges from auto-tuner, 7d+1d P&L, WR, effective filter summary.
- Category Heatmap + Lifecycle Pipeline + Top Candidates table.

Dropped entirely: the old smartphone-style chat feed, achievement grid, trading-card rarity system, heartbeat animation, whale-alert slide-in. Popup notifications for whale alerts / brain decisions / trade closes / new buys now flow through the shared bottom-right PopupManager instead.

Matrix-rain canvas (gold, opacity 0.12) sits as a background behind the whole page.

### Files touched

- NEW `dashboard/static/terminal.css`, `dashboard/static/terminal.js`
- `dashboard/templates/_nav.html` (reorder + rename + target=_blank)
- `dashboard/templates/brain.html` (full rewrite)
- `dashboard/templates/dashboard.html` (active value only; existing inline CSS/JS untouched)
- `dashboard/templates/logs.html`, `dashboard/templates/index.html`, `dashboard/templates/reports.html` (chrome unified, page-specific body preserved)

No `app.py` changes — all new brain panels reuse existing endpoints (`/api/upgrade/*`, `/api/brain/*`, `/api/ai/blocked-trades`, `/api/fun/ticker`, `/api/stream`).

## 2026-04-13 (Abend) — Code-review finding: lifecycle auto-promote bypass

Self-review of commits `ba70dbf..e2c6129` via `code-review` skill surfaced one real bypass that the earlier `AUTO_DISCOVERY_AUTO_PROMOTE` gate missed.

### The bypass

`AUTO_DISCOVERY_AUTO_PROMOTE=false` was introduced in `e2c6129` and gates the auto_discovery candidate promotion path at `bot/auto_discovery.py:400-406`. But `bot/trader_lifecycle.py::_check_paper_to_live()` at line 105 also calls `_add_followed_trader()` — unconditionally, no flag check. The function target `_add_followed_trader()` at line 218 also had no internal gate.

**Effective chain** (verified against `database/scanner.db` on server): `fsavhlc`, `xsaghav`, `sovereign2013`, plus 4 DISCOVERED whales (`0x161eb16874`, `0x3e5b23e9f7`, `0x6bab41a0dc`, `0x7d0a771ddd`) still exist in `trader_lifecycle` with `status=PAUSED`/`DISCOVERED`. `_check_paused_to_rehab()` transitions PAUSED → PAPER_FOLLOW after `REHAB_DAYS=3`. If `pause_count < MAX_PAUSE_COUNT=2`, `_start_rehab` runs. Then `_check_paper_to_live()` runs paper criteria and, on success, calls `_add_followed_trader()` — which writes the trader back into `settings.env::FOLLOWED_TRADERS`, reversing the roster cleanup from `e2c6129` without user consent.

Acute risk at detection time: `sovereign2013` (pause_count=1, pause_until `2026-04-14`) was 24h away from entering rehab and could have been auto-re-added within days. `fsavhlc`/`xsaghav` have pause_count=17 so `_start_rehab` would KICK them — no risk there.

### The fix

**Logic-level gate in `_check_paper_to_live()`**: When `AUTO_DISCOVERY_AUTO_PROMOTE=false`, a trader meeting paper criteria gets logged as `PROMOTE_RECOMMENDED` via `log_brain_decision` (so the dashboard shows it) and the loop `continue`s. Status stays `PAPER_FOLLOW`, `settings.env` stays untouched.

**Function-level gate in `_add_followed_trader()` (defense-in-depth)**: Early return when flag is false. Covers any future or overlooked call site. Matches the pattern at `pause_trader()` line 63-64 where `_remove_followed_trader` is already disabled because "settings managed manually" — this closes the same door from the opposite side.

Both changes are controlled by the single existing `AUTO_DISCOVERY_AUTO_PROMOTE` flag. User can flip it back to `true` to restore the old behavior. No new config keys.

### Tests

New `tests/test_lifecycle_gate.py` with 4 TDD tests:
- `test_paper_to_live_blocks_when_auto_promote_false` — reproduces the bypass
- `test_paper_to_live_promotes_when_auto_promote_true` — locks in the enable path
- `test_add_followed_trader_direct_call_blocked_when_false` — function-level gate
- `test_add_followed_trader_direct_call_allowed_when_true` — direct call enable path

All 4 RED without the fix, GREEN with. Full suite 56/56.

### Zombie rows left intact

Not cleaned up as part of this fix — the gate makes them inert (no settings.env mutation regardless of status transitions). Data-maintenance separate from bug fix.

## 2026-04-13 (Nachmittag) — Profitability round: roster cleanup + zero-risk filter + feedback-loop cleanup + ghost root cause

User directive: "und gib mir zusammenfassung was wir machen sollten um profitabel zu werden" → "nutze superpowers und fixe alles". Focus shifts from bug-hunting to stopping the bleed. Portfolio $320 start → $93.15 = **-71%**. Root cause is the roster: none of the 5 followed traders are profitable on a 7d basis (combined -$182). Fixing ML / dedup / filters is valuable but doesn't change the math if the upstream signal is bad.

### 1. Roster shrunk to KING + Jargs only

Removed from `settings.env` `FOLLOWED_TRADERS` and from every trader-scoped `*_MAP` setting (`MIN_TRADER_USD_MAP`, `MIN_ENTRY_PRICE_MAP`, `MAX_ENTRY_PRICE_MAP`, `MAX_COPIES_PER_MARKET_MAP`, `CATEGORY_BLACKLIST_MAP`, `MIN_CONVICTION_RATIO_MAP`, `TAKE_PROFIT_MAP`):
- xsaghav (7d -$98.25 / 42.5% WR / 186 trades)
- sovereign2013 (7d -$40.00 / 46.2% / 173)
- fsavhlc (7d -$21.05 / 40.0% / 20)
- aenews2 (idle)
- 0x3e5b23e9f7 (whale from auto_discovery — was still producing copies via FOLLOWED_TRADERS despite `AUTO_DISCOVERY_AUTO_PROMOTE=false`, exposed by #3145 Peru Keiko Fujimori this morning)
- 0x6bab41a0dc (same pattern, idle)

`UPDATE wallets SET followed=0` for all 6 (6 rows affected). Post-cleanup `followed=1`: only `Jargs` and `KING7777777`. Deployed via direct server settings rewrite. Backup at `settings.env.bak.1776075832`.

### 2. Zero-risk category filter

New config flags:
```
ZERO_RISK_CATEGORIES=cs,lol,valorant,dota
ZERO_RISK_MIN_PRICE=0.40
```

New helper `bot/copy_trader.py _is_zero_risk_block(category, trader_price)` → returns True when category ∈ list AND trader_price < threshold. Wired into both buy paths (diff-scan at line ~696 and activity-scan at line ~1701) as an additional filter after the existing `price_range` check. Blocks are logged with `block_reason='zero_risk'` and a clear detail string.

Motivation: esports map markets (CS/LoL/Valorant/Dota) are "bin-or-bust" — unlike sports spreads where losers retain residual value, a lost CS map is worth 0 cents. Concrete recent evidence: #3128 + #3129, both KING7777777 on Counter-Strike Phantom vs HEROIC Academy (Map 1 and Map 2), both bought at 0.266, both resolved to 0, combined loss -$4.62. The filter blocks this exact class of setup going forward. 12 TDD regression tests in `tests/test_zero_risk_filter.py`, including `test_reproduces_3128_and_3129` which pins the real failure.

### 3. Feedback-loop cleanup: 7.2% → 96.8% coverage

Context: ML and scorer diagnostics rely on `trade_scores.outcome_pnl`. Iter 26 flagged coverage at 61/845 = 7.2%. Investigation:
- 786 NULL-outcome rows were all linked to `copy_trades` rows with `status='baseline'` (the startup-baseline snapshot rows that the bot records for existing wallet holdings but never actually buys). These scores will never have a real outcome because no trade ever happened.
- Additional 20 rows were fully orphaned (no matching `copy_trades` at all).

Fix: `DELETE FROM trade_scores WHERE outcome_pnl IS NULL AND NOT EXISTS (SELECT 1 FROM copy_trades ct WHERE ct.condition_id=trade_scores.condition_id AND ct.wallet_username=trade_scores.trader_name AND ct.status != 'baseline')`. 784 rows deleted. Post-cleanup: 63 total / 61 outcome-stamped = **96.8% coverage**. DB backup at `scanner.db.bak.1776075832`.

Also revealed the real uniqueness picture of score buckets (after dedup + cleanup):
- 40-59: 1 unique trade, 0 wins, -$13.43
- 60-79: 9 unique trades, 2 wins, 22.2% WR, -$12.02
- 80-100: 3 unique trades, 0 wins, -$7.78

**Total: 13 unique closed trades have been scored. 2 wins.** The earlier "SCORER_INVERTED" flag from iter 25 was entirely a SCORE_SPAM artifact — the 16 rows in the 80-100 bucket were 3 unique trades duplicated 2-7 times. Real scorer performance can't be evaluated on a sample of 13; the scorer isn't broken, it just has no signal because the underlying bot has no signal. Confirmation that scorer fixes alone won't solve profitability.

### 4. Ghost positions root cause (27 positions, $22.04 real chain value)

Reconcile job exposed 27 on-chain positions worth $22.04 total that the DB has marked as `status='closed'`. Previously hypothesized as "legacy / auto_discovery / manual". Actual classification:
- **15 with `usdc_received = NULL` (chain $14.24)**: close paths that mark the DB as closed without ever calling sell_shares. Top samples: #3127 Peru Keiko Fujimori ($3.34), #1248 Levica Slovenia ($2.32), #3126 Peru Lopez Aliaga ($2.15).
- **12 with `usdc_received > 0` (chain $7.80)**: partial sells where some USDC came back but shares remained on chain. Top samples: #1669 fsavhlc Iran/Qatar ($2.40 chain, $2.18 recv), sov2013 NHL O/U cluster.

**Root cause**: at least one auto-close code path marks `status=closed` in the DB while the sell order either (a) partially filled and the DB recorded the partial USDC but left the remaining shares, or (b) never fired at all. $22.04 of real money stuck in positions the DB pretends are closed. Going forward they'll resolve naturally and the payout will (or won't) land on chain, but the DB P&L numbers are structurally wrong by that amount.

**NOT FIXED in this round** — fixing requires auditing every close path (AUTO-CLOSE-lost, AUTO-CLOSE-won, trailing-stop, miss-close, FAST-SELL, TAKE-PROFIT, trader-closed-it, Gamma fallback) for correct partial-fill semantics. Scope exceeds this session. Documented as a standalone finding for the next round. User action for this round: none — the money isn't lost, just mis-recorded; positions will resolve organically.

### 5. Brain oscillation mutex test — fixed under `unittest discover`

`test_revert_skips_trader_in_mutex` was passing in isolation but failing under `python -m unittest discover tests` because the StreamHandler capture approach didn't survive the `importlib.reload(database.db)` done by `setup_temp_db()` in other test classes. Switched to `self.assertLogs('bot.brain', level='INFO')` context manager + explicit `importlib.reload(bot.brain)` in the test to force a fresh module reference. 52/52 tests green under discover.

### Summary of user-visible impact

- Bot now copies **only KING + Jargs** (roster shrunk 5→2). xsaghav, sov, fsavhlc, both whales, aenews2 are `followed=0`.
- CS/LoL/Valorant/Dota underdogs below 40¢ are **blocked automatically**.
- Feedback metric is now meaningful (96.8% coverage on 63 scored trades).
- Known $22.04 DB↔chain divergence documented but not fixed — not losing more money, just misrecorded.

All 52/52 unit tests green. Bot restarted cleanly on server. No new errors since restart.

## 2026-04-13 (Mittag-2) — Iter 25 findings: brain mutex + trade_scores dedup

First ralph iteration after the morning deploy (commit `ba70dbf`) exposed 3 new findings. 2 are real bugs, 1 was a false positive driven by the same spam pattern as the first. All fixed in one commit.

### 1. BRAIN_OSCILLATION intra-cycle — `_tightened_this_cycle` mutex

Observed brain cycle at 2026-04-13 09:28:57: decisions 481 (`TIGHTEN_FILTER KING7777777` from 12 BAD_PRICE losses) and 485 (`RELAX_FILTER KING7777777` from 7d pnl=+$29 tier=neutral) fired 1 second apart in the same brain run. The cross-cycle dedup I added this morning is keyed on `(action, target)`, so TIGHTEN_FILTER and RELAX_FILTER with the same target don't collide. And investigation confirmed the RELAX branch in `_revert_obsolete_tightens` actually writes settings.env AFTER the TIGHTEN branch, clobbering it (both `_tighten_price_range` and `_revert_obsolete_tightens` rewrite MIN/MAX_ENTRY_PRICE_MAP in the same cycle). That's why piff's PATCH-025 note "Auto-tuner AFTER reverts" didn't save us — the revert itself was the last writer.

Fix: module-level set `_tightened_this_cycle` in `bot/brain.py`. Cleared at the top of every `run_brain()` call. Populated in `_classify_losses()` immediately after each `_tighten_price_range()` call. Checked at the top of the `_revert_obsolete_tightens()` loop — skip + log if trader is already in the set. This is an intra-cycle guard, not a cross-cycle one: the next cycle 2h later can still relax the trader if conditions justify it, but the revert can't undo a decision from the same run.

### 2. SCORE_SPAM — `_score_dedup_cache` on `log_trade_score`

Observed: 86 `trade_scores` rows in 14 minutes for a single (sovereign2013, `0x5042fda9...` Barcelona Open Buse vs Moutet, QUEUE, score 53) triple. Root cause: `bot/trade_scorer.py:score()` is called from `bot/copy_trader.py` inside the scan loop (every 5-10s) and unconditionally calls `db.log_trade_score()` → INSERT. Same pattern as the `log_blocked_trade` spam fixed this morning.

Fix: `_score_dedup_cache` in `database/db.py` mirroring `_blocked_dedup_cache`. TTL 60s, max size 20000, key `(trader_name, condition_id, action)`. **Important exception**: `trade_id != None` bypasses the dedup — when a real buy lands and the scorer wants to stamp the row with its trade_id, we always write. This preserves the `update_trade_score_outcome()` feedback-loop linkage (it matches on newest-NULL-outcome row per `(cid, trader)`, which still works because the first dedup-write is the only NULL row within the TTL window).

### 3. SCORER_INVERTED — **not a real bug**, artifact of SCORE_SPAM

Ralph iter 25 flagged score bucket 80-100 as "0/16 WR" vs 60-79 at "32/38 WR (84%)" — apparent anti-discrimination. Before writing a fix, I queried the actual 16 rows: they are only **4 unique trades** duplicated 2-7 times each (KING Fukuoka SoftBank × 7, Ground Zero Gaming × 6, Wolves Esports × 3 across two entry prices). The 60-79 "84% WR" bucket is inflated the same way (Gen.G Esports × 8 rows for one +$1.00 win). Once SCORE_SPAM is fixed, each unique trade counts once and bucket stats become meaningful. Resolved by the fix above.

The lesson: before trusting any aggregate over the feedback cohort, check unique-trade count, not row count. Noted in `docs/trade_analysis/state.json` iter 25 summary.

### Tests

Extended `tests/test_log_dedup.py` with 2 new classes, +10 tests (30 → 40 total):
- `TestTradeScoreDedup` — 8 tests including `test_simulated_86_scan_cycles_collapse_to_one` (reproduces the production symptom), `test_trade_id_bypasses_dedup` (verifies buys always stamp), `test_outcome_lookup_still_finds_newest_null` (verifies feedback-loop linkage survives dedup).
- `TestBrainOscillationMutex` — 2 tests including `test_revert_skips_trader_in_mutex` (patches `_parse_map` + `_read_settings` + `get_trader_rolling_pnl`, asserts `get_trader_rolling_pnl` is **never called** for a trader in the mutex set, proving short-circuit).

All 40/40 pass. Deployed via `scp bot/brain.py + database/db.py` (tests dir doesn't exist on prod). Smoke-verified 5/5 on live server: `_score_dedup_cache` loaded, `_tightened_this_cycle` present as set, `log_trade_score` source contains `trade_id is None` gate + cache ref, `_revert_obsolete_tightens` logs "Skipping RELAX", `run_brain` clears mutex.

### Production smoke verification

Bot running at $73.73 wallet / $93.23 total, 0 errors post-restart. Blocks at 9/5min with 4 unique keys (dedup still effective from morning commit). Score dedup can't be fully verified until scorer fires again (next scan cycle picks up active markets).

## 2026-04-13 (Mittag) — All 6 open ralph-loop findings fixed

Ralph-loop ran 24 iterations overnight + morning, surfacing 6 open findings carried over from the previous session. All fixed, tested (30/30 unit tests green), deployed to prod, and smoke-verified on the live server.

### 1. BRAIN_CYCLIC_SPAM → cross-cycle dedup in `log_brain_decision`

`database/db.py` `log_brain_decision()` gained `dedup_hours: int = 3` parameter. Before INSERT, SELECT on `(action, target, created_at >= cutoff)` and skip if match exists. Pass `dedup_hours=0` to disable. Six consecutive brain cycles writing byte-identical rows (TIGHTEN KING / PAUSE sov / PAUSE xsaghav / PAUSE fsavhlc / RELAX KING) now collapse to one row per 3h window.

### 2. EVENT_TIMING re-block spam → in-memory TTL cache on `log_blocked_trade`

`database/db.py` got `_blocked_dedup_cache` (process-local dict) with `_BLOCKED_DEDUP_TTL_SEC=60` and `_BLOCKED_DEDUP_MAX_SIZE=20000`. Key is `(trader, condition_id, block_reason)`. Same key within 60s skips the INSERT silently. Cache flushes completely when oversized (acceptable cost vs. dragging a per-entry LRU into the hot path). Previously `sovereign2013 + 0x14d57e73 + exposure_limit` wrote a row every 10s scan = 360/hour. Now: 1 row per minute max.

### 3. BRAIN_DATA_DIVERGENCE → false alarm, no code change

Investigated `db.get_trader_rolling_pnl()` (db.py:1045). It intentionally uses verified-only mode when >=10 trades have `usdc_received + actual_size IS NOT NULL`. Brain reports KING +$29.17 from 20 verified trades; my naive ralph SELECT summed all 127 closed `pnl_realized` including 107 historical broken-path rows. **Brain is correct, ralph monitoring query was naive.** Documented — no code fix.

### 4. ML_ACCURACY_SUSPICIOUS → COPY-ONLY diagnostics + time-split fix

`bot/ml_scorer.py` `_build_training_data()` had a subtle bug: it concatenated copy_rows then blocked_rows, breaking the chronological 80/20 split. Now merges both sources by `created_at` ASC and returns a 5-tuple `(X, y, is_copy, copy_count, blocked_count)` with an `is_copy` marker aligned to rows.

`train_model()` now slices the test set by `is_copy_test` and computes COPY-ONLY test accuracy + confusion matrix (TP/FP/TN/FN/precision/recall). Logs:
```
[ML] COPY-ONLY test subset (n=X, W win/L loss): acc=X% baseline=X% | TP=... FP=... TN=... FN=... | prec=X rec=X
```
This exposes whether the 94.9% headline is driven by the blocked_trades population (where extreme-price markets trivially win) vs. actual copy-trade predictive power.

### 5. WHALE_AUTO_COPY_PATH → gated behind `AUTO_DISCOVERY_AUTO_PROMOTE`

`bot/auto_discovery.py:397` used to unconditionally call `_add_followed_trader()` + `db.add_followed_wallet()` when a DISCOVERED wallet met WR+PnL thresholds. This silently followed two whale wallets (`0x3e5b23e9...` and `0x6bab41a0...`) that produced live copy_trades (#3124-#3127, combined -$0.81).

Fix: new config flag `AUTO_DISCOVERY_AUTO_PROMOTE` (default `false`). When false, logs `"meets promote criteria but AUTO_PROMOTE=false — review manually"` and leaves the wallet in DISCOVERED state. User must explicitly enable or add via dashboard. Also manually `UPDATE wallets SET followed=0` for the two previously-auto-followed wallets.

### 6. DB_VS_WALLET_POSITION_DIVERGENCE → scheduled reconciliation job

New `reconcile_db_vs_wallet()` in `main.py`. Fetches on-chain positions from `data-api.polymarket.com/positions?user=<funder>`, computes `{ghost_cids}` (on-chain but not in DB-open) and `{orphan_cids}` (DB-open but not on-chain), logs counts + first 3 samples of each category, and estimated ghost USD value. APScheduler runs it every 30min starting 7min after bot start.

### Tests

New file `tests/test_log_dedup.py` with 12 regression tests covering both dedup mechanisms: first-call writes, within-TTL skipped, different key still writes, simulated 500-iter scan loop keeps 1 row, simulated 5 brain cycles dedup to 5 rows, `dedup_hours=0` disables guard. Updated `tests/test_ml_time_split.py` to expect the new 5-tuple and assert `COPY-ONLY` appears in training logs. All 30/30 unit tests pass.

### Production smoke verification

Post-deploy, ran a Python smoke test against the live server confirming each fix is loaded: AUTO_PROMOTE=False, `_blocked_dedup_cache` exists, `log_brain_decision` signature has `dedup_hours=3`, `_build_training_data` returns 5 values, `reconcile_db_vs_wallet` function exists, scheduler has `reconcile_db_wallet` job. All PASS. Bot running clean at $91.69, 0 errors.

## 2026-04-13 (Morgen) — Feedback-loop gap patch + trailing-stop extended disable

Two critical production bugs found during ~15h overnight ralph-loop monitoring of Round 4 work.

### 1. Feedback loop missing call sites (commit `26099b1`)

Round 4 Task 2 patched `update_trade_score_outcome` into 3 close paths in `copy_trader.py` (resolved-at-0.99/0.01, stop-loss, trailing-stop). Overnight analysis showed the real bot closes trades via **8 additional code paths**, none of which were calling the feedback helper. Concrete failure: on 2026-04-13 #3128 (KING7777777 CS Map 1 HEROIC Academy, -$2.35 total loss) closed via the `main.py` AUTO-CLOSE-lost periodic price check, and the matching `trade_scores` row #743 stayed at `outcome_pnl=NULL` until I manually backfilled it. Feedback coverage was stuck at 59/742 for 15+ ralph iterations because most closes bypass the patched paths entirely.

Patched sites:
- `bot/copy_trader.py` FAST-SELL (trader exited → we sell, line 1581)
- `bot/copy_trader.py` FAST-SELL cascade (secondary same-cid closes, line 1590)
- `bot/copy_trader.py` TAKE-PROFIT (line 2434)
- `bot/copy_trader.py` trader-closed-it fallback (line 2481)
- `bot/copy_trader.py` Gamma API fallback resolved (line 2523)
- `bot/copy_trader.py` miss-close (line 2565)
- `main.py` AUTO-CLOSE lost (line 286) — added `wallet_username` to the SELECT so the helper can match
- `main.py` AUTO-CLOSE won (line 325) — same

All call the same try/except pattern and use `db.update_trade_score_outcome(cid, trader, pnl)`. Silent debug-log on exception. Score `trade_id` linkage still null at write time (happens in scorer.log_trade_score), but `(cid, trader)` match on newest NULL-outcome row is reliable — no window filter since NO_REBUY_MINUTES=120 guarantees one NULL row per pair.

### 2. Trailing stop extended disable — cover thin-book US sports (same commit)

Piff's esports disable (cs/lol/valorant/dota) was introduced in Round 4 because trailing-stop fills were walking the book below the -20c max slippage config on thin orderbooks. Analysis of today's real trades showed **the exact same pattern on NBA markets**:

| Trade | Market | Entry | Peak | Quote @ trigger | Actual fill | Loss |
|---|---|---|---|---|---|---|
| #3035 | Spurs spread (nba) | 0.51 | 0.67 | 0.55 | ~0.34 | -$0.37 |
| #3036 | Jazz/Lakers O/U (nba) | 0.51 | 0.665 | 0.54 | ~0.27 | -$0.55 |

Both triggered correctly (peak − 12c margin), both filled ~20-27c below quote = 50% slippage = trailing stop actually destroyed realized PnL instead of protecting it. Combined peak-unrealized was +$0.57, combined realized was -$0.92. $1.49 destroyed on $2.25 capital (**66%** value destruction peak-to-exit).

Extended `_ts_thin_book` list to include `nba`, `mlb`, `nhl` alongside esports. Variable renamed from `_ts_is_esports` to `_ts_thin_book` to reflect broader semantics. TRAILING_STOP_ENABLED still checked; only these categories are excluded.

### Side note: manual backfill of #743

Before the code patches, I manually ran `UPDATE trade_scores SET outcome_pnl=-2.35, trade_id=3128 WHERE id=743` to unstick the 15-iteration feedback coverage stall. Feedback coverage went from 59/743 → 60/755 (new scores arrived in parallel). Going forward, the code patches above should keep coverage growing organically without manual intervention.

### Ralph loop findings still open (not fixed this commit)

- **BRAIN_CYCLIC_SPAM**: 6 consecutive brain cycles wrote the byte-identical 5 decisions (TIGHTEN KING / PAUSE sov / PAUSE xsaghav / PAUSE fsavhlc / RELAX KING). Need cross-cycle dedup in `log_brain_decision` — skip if the same `(action, target, reason)` was written in the last N hours.
- **BRAIN_DATA_DIVERGENCE**: brain reports KING7777777 7d pnl +$31.52 / 53% WR across 6 cycles; ralph SELECT on same window shows -$6 to -$10 / 41%. `db.get_trader_rolling_pnl` is reading something different. Worth grepping the helper to find the discrepancy.
- **EVENT_TIMING stuck at ~540**: same pre-game markets re-blocked every scan for 10+ iters without dedup. A 5-min TTL cache on `(trader, cid)` would eliminate thousands of redundant `blocked_trades` inserts.
- **ML_ACCURACY_SUSPICIOUS**: training logs show 94.9% test accuracy vs 65.3% baseline (29.6pp gap). Probable feature leakage via `entry_price`/`category` correlation with `blocked_trades.would_have_won` (most training data is extreme-price markets where high prices win trivially). Live evidence: the ML penalty demoted score #743 (KING CS) from BOOST (79.4) to EXECUTE (64), predicting <30% win prob — which happened to be correct this one time, but sample=1. Morning report should include a confusion-matrix check on the 614-copy-trades subset only.
- **WHALE_AUTO_COPY_PATH**: `auto_discovery` produces real copy_trades from DISCOVERED-status wallets (#3124-#3127 from `0x3e5b23e9f7`, combined -$0.81). Decision pending whether this is intentional.
- **DB_VS_WALLET_POSITION_DIVERGENCE**: wallet snapshot shows $17 in positions while `copy_trades WHERE status='open'` shows 0 rows. Reconciliation job needed.

---

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
