# Changelog

Session-level notes. For full commit history see `git log`.

## 2026-04-15 — paper_follow: stateful watermark + UNIQUE dedup + concurrency guard (piff-flagged)

**Two commits**: `112160f` (primary watermark fix) + `4bd46b3` (concurrency/dedup follow-up after code review).

### What piff needs to do

1. `git pull` on your fork.
2. `sudo systemctl restart polybot` — required because `auto_discovery` is imported at startup, `init_db` runs the migrations on startup, and the scheduler setup in `main.py:799` changed.
3. **No settings.env changes needed.** `ENTRY_TRADE_SEC=300` is untouched — only the live copy path uses it now; paper_follow has a stateful watermark instead.
4. The migration runs automatically on first startup (idempotent, no-op on subsequent runs):
   - `ALTER TABLE trader_candidates ADD COLUMN last_paper_scan_ts INTEGER DEFAULT 0`
   - `DELETE FROM paper_trades WHERE rowid NOT IN (SELECT MIN(rowid) ... GROUP BY candidate_address, condition_id, side) AND status='open'` — collapses fill-split duplicates on open rows only (closed rows are left alone; partial index only enforces on open).
   - `CREATE UNIQUE INDEX idx_paper_trades_open_dedup ON paper_trades(candidate_address, condition_id, side) WHERE status='open'`.

### The bug piff flagged

Piff reported: "Beide 3 Stunden — identisch. Mit ENTRY_TRADE_SEC=300 ist es fast unmöglich einen Trade zu erwischen."

Verified: `paper_follow_candidates()` in `bot/auto_discovery.py:309` was applying `ENTRY_TRADE_SEC=300` — copy-pasted from `bot/copy_trader.py:1892` where it makes sense because the live copy path scans every 60s. But `discovery_scan` (which calls `paper_follow_candidates`) runs every 3h, so 300/10800 = **2.78% theoretical coverage** per trade. Live snapshot pre-fix: **149 paper_trades / 24h across 40 candidates = 3.7 trades/candidate/day** — a trader with 15 trades/day would take 20 days to cross `PROMOTE_MIN_TRADES=50`.

### Primary fix (commit `112160f`)

Replaced the fixed 300s window with a per-candidate `last_paper_scan_ts` watermark stored on `trader_candidates`. Each scan captures BUYs strictly newer than the watermark, then advances it to the newest timestamp. Robust against any scan cadence. Plus `fetch_wallet_recent_trades` limit 10 → 50 so hyperactive traders aren't silently truncated.

Files:
- `database/db.py` — `ALTER TABLE` + `get_candidate_paper_scan_ts` / `set_candidate_paper_scan_ts` helpers.
- `bot/auto_discovery.py` — watermark read/filter/advance logic in `paper_follow_candidates`. `ENTRY_TRADE_SEC` filter removed from this path.
- `tests/test_paper_follow_stateful.py` — 5 TDD tests.
- `bot/copy_trader.py` — **unchanged**. Live copy path still uses `ENTRY_TRADE_SEC=300` as intended.

### Follow-up fix (commit `4bd46b3`)

Independent code review flagged 2 blockers + 3 highs:

- **`add_paper_trade` had `INSERT OR IGNORE` but no UNIQUE constraint** → 91% of `paper_trades` rows (5312/5819) were duplicates pre-cleanup. Most of that "duplication" was Polymarket returning one logical trader decision as N separate partial-fill activities (e.g. 11 rows for one Vitality trade at microprices 0.81, 0.8100000026, 0.810000043, ...). Economically: 1 decision, collapsed correctly.
- **`discovery_scan` lacked `max_instances=1`** and the job was being re-registered at irregular 15-30min intervals (root cause unidentified; candidates: auto-update.sh, vpn-watchdog.sh, settings-reload, systemd restart loop). Two concurrent scans would race on the same `last_ts` and double-insert.
- **`get_/set_candidate_paper_scan_ts` opened separate connections** → stale concurrent writer could roll the watermark backwards.
- **`newest_ts` advanced only on BUYs** → SELL-heavy windows caused next scan to re-read same tail.

Fixes in `4bd46b3`:
- `main.py:799` — `max_instances=1, coalesce=True, misfire_grace_time=600, replace_existing=True` on the `discovery_scan` job.
- `database/db.py::init_db` — new cleanup `DELETE` migration + `CREATE UNIQUE INDEX idx_paper_trades_open_dedup ON paper_trades(candidate_address, condition_id, side) WHERE status='open'`. Partial index so re-entry after close is still allowed.
- `database/db.py::set_candidate_paper_scan_ts` — `SET last_paper_scan_ts = MAX(COALESCE(last_paper_scan_ts, 0), ?)` for monotonic-increasing guarantee.
- `bot/auto_discovery.py::paper_follow_candidates` — reordered so `newest_ts` advances on every trade, not just BUYs.
- `tests/test_paper_follow_stateful.py` — 5 new TDD tests (monotonic, UNIQUE blocks dup open, reentry after close allowed, different sides allowed, cleanup migration collapses dupes).

Total 10/10 paper_follow tests pass + 93 full-suite pass + 1 pre-existing brain_dedup unrelated.

### Live verify on our server (post-deploy)

```
02:46 scan: 47 unique paper_trades captured (first post-fix scan)
03:02 scan: 2 new unique captures (second scan — different content, not identical)
Zero duplicate groups in the 30-min observation window
125 historical open dupe rows removed by the cleanup migration (5819 → 5694 total)
21 of 41 active candidates have non-zero watermarks, monotonic advancing
```

Commands piff can run to verify the same on his side:

```sql
-- 1. Watermark column exists and is populating
SELECT address, status, last_paper_scan_ts FROM trader_candidates
 WHERE status IN ('observing','promoted')
 ORDER BY last_paper_scan_ts DESC LIMIT 10;

-- 2. UNIQUE partial index was created
SELECT sql FROM sqlite_master
 WHERE type='index' AND name='idx_paper_trades_open_dedup';

-- 3. No duplicate open rows remain
SELECT candidate_address, condition_id, side, COUNT(*) n
  FROM paper_trades WHERE status='open'
  GROUP BY candidate_address, condition_id, side
 HAVING n > 1;
-- expect 0 rows

-- 4. paper_trades growth rate (run 30 min after restart, then 3h later, compare)
SELECT COUNT(*) FROM paper_trades
 WHERE created_at > datetime('now','-30 minutes');
```

### Known separate issue NOT fixed here

`database/db.py::get_candidate_stats` counts `SELECT COUNT(*) FROM paper_trades` — the cleanup only scoped to `WHERE status='open'`, so **5632 historical closed dupe rows** still contaminate `total` / `wins` / `losses` / `total_pnl` for candidates with pre-fix paper_trade history. Promotion-gate counts from those are inflated.

**This must be fixed before flipping `AUTO_DISCOVERY_AUTO_PROMOTE=true`** — either change `get_candidate_stats` to use `COUNT(DISTINCT candidate_address, condition_id, side)` with proportional wins/losses/pnl dedup, or migrate historical closed dupes out. Separate PR, not in this session.

### Expected empirical behavior post-fix

- Per-scan capture should be **~30-80 unique rows** depending on trader activity (vs. old ~5 per scan).
- paper_trades growth should be **~5-10x** pre-fix once each candidate has been scanned at least once.
- Two consecutive 3h scan windows will no longer be identical — each picks up the trades that happened since the prior watermark.
- No dup groups should ever appear again in the open-row set (UNIQUE partial index enforces it at the DB level).

## 2026-04-14 (latest) — Heal id=3547 Angels ghost + dashboard reads actual_size from DB

Two small follow-ups to the partial-ghost detection commit (39b5f22).

### 1. Manual DB heal: id=3547 actual_size / shares_held / actual_entry_price

User approved a one-time manual UPDATE on `copy_trades` to make the
Angels row reflect the real on-chain position. Pre-update the row
had `actual_size=$1.00, shares_held=1.7318, actual_entry_price=0.578`
— the one buy the DB successfully INSERTed before the UNIQUE race
swallowed the next 48. Post-update the row matches the data-api
snapshot at 21:17 UTC: `actual_size=$48.37, shares_held=84.8558,
actual_entry_price=0.5699`.

Why manual not auto: DB writes against copy_trades require explicit
user consent per memory/user_preferences. The partial-ghost detection
from 39b5f22 was read-only by design; healing is a separate deliberate
operation. Backup taken at `/tmp/db_backups/scanner.db.pre_id3547_
heal.1776201426` before the UPDATE in case revert is needed.

SQL applied:

```sql
BEGIN;
UPDATE copy_trades
   SET actual_size=48.3677,
       shares_held=84.8558,
       actual_entry_price=0.5699
 WHERE id=3547;
COMMIT;
```

Revert SQL (stashed here in case needed):

```sql
UPDATE copy_trades
   SET actual_size=1.0,
       shares_held=1.731753,
       actual_entry_price=0.57745
 WHERE id=3547;
```

Verification post-UPDATE:
- `SELECT * FROM copy_trades WHERE id=3547` → new values confirmed
- `sum_open_shares_held_by_cid_side(cid, "Under")` → 84.8558 (matches
  chain)
- Manual `reconcile_db_vs_wallet()` trigger no longer flags this
  market as partial-ghost (still reports the 28 set-level ghosts
  which are a different category).

Side effects by design:
- Post-resolution `close_copy_trade` math will compute against the
  real basis: Under wins → `pnl_realized = 84.86 - 48.37 = +$36.49`,
  Over wins → `pnl_realized = 0 - 48.37 = -$48.37`. Pre-heal the row
  would have shown ±$1 which is meaningless for trader_performance
  stats, ml_scorer training, and the Filter Precision Audit.
- `trader_performance.total_pnl` for sovereign2013 will re-aggregate
  correctly on the next brain cycle.
- `ml_copy` training at the next 6h retrain will see this row with
  the real outcome magnitude (±$48) instead of the truncated ±$1.

What was deliberately NOT updated:
- `entry_price` stays at 0.55 (the original signal price from the
  copy_scan `[NEW]` log, not the effective fill price).
- `size` stays at 1.00 (the original planned bet size from
  BET_SIZE_PCT * equity). Keeping these two untouched preserves the
  distinction between "what the bot planned" and "what actually
  happened on chain". The `actual_*` fields carry the real state.

### 2. dashboard/app.py: SELECT missing columns from copy_trades

Unrelated pre-existing bug discovered while verifying the heal:
`/api/live-data` was computing `_open_match.get("actual_size")` but
the SQL query at line 211 only selected `id, condition_id,
wallet_username, created_at, closed_at, size, entry_price, status`.
It never selected `actual_size`, `actual_entry_price`, or
`shares_held`, so `_open_match.get("actual_size")` always returned
None and the fallback chain fell through to the planned `size` field
and `entry_price` instead of the effective values.

Post-fix the SELECT also pulls `actual_size, actual_entry_price,
shares_held, side`, and the downstream code in the same endpoint
(which already has `_actual_size = _open_match.get("actual_size") if
_open_match else None` and a fallback cascade) now gets real values
instead of None.

Measured effect on the healed Angels row:

```
pre-fix:   size=1.00   entry_price=0.55   pnl_unrealized=-0.05
post-fix:  size=48.37  entry_price=0.5699 pnl_unrealized=-3.81
```

The `pnl_unrealized=-3.81` number is exactly the mark-to-market loss
on $48.37 at a price drop 0.57 → 0.525. Matches chain `pnl=-3.82`
within rounding.

This bug has been latent probably forever (or since `actual_*`
columns were added to the schema), but only became visible now
because the Angels row is the first one in production with a large
delta between the "planned" `size` (1.00) and the "actual"
`actual_size` (48.37). Normal trades have size ≈ actual_size so the
fallback happened to produce the right number by accident.

---

## 2026-04-14 (later) — Partial-ghost share detection in reconcile + dashboard

Backstop for the blind spot that hid the 2026-04-14 Angels ghost
position. Commit `0d6f2be` prevents future ghost races via the
`has_open_trade_for_market` pre-check. This commit **surfaces
existing partial-ghost shares** so an incident can never again hide
at $44-level on a $106 equity account.

### The blind spot

- Chain: `size=84.86 shares Under, avgPrice=0.5699, currentValue=$44.55`
- DB: `id=3547 status='open' shares_held=1.73 actual_size=$1.00`
- `reconcile_db_vs_wallet()` did `ghost_cids = chain_cids - db_cids`.
  Because id=3547's `condition_id` matched the chain row, the Angels
  market was classified "tracked"; the 84 untracked shares were
  invisible.
- `/api/live-data` joined by `condition_id` and preferred DB
  `actual_size` as the display size, so the UI showed
  `pnl_unrealized=-$0.05` for the $1 tracked slice instead of
  `-$3.82` on the full 85 shares.

### Why the obvious helper doesn't work

First attempt: `sum_open_shares_held_for_market(wallet_address,
condition_id)` keyed on `(wallet, cond)` — same semantics as
`has_open_trade_for_market`. Always returned `0.0` for every followed
trader. Root cause: **`copy_trades.wallet_address` stores the SOURCE
TRADER's wallet** (sovereign2013 at `0xee613b...`), not our executing
wallet (`POLYMARKET_FUNDER` at `0x53fe4db...`). Chain `/positions`
returns holdings at the FUNDER. The wallet-keyed helper was being
called with the wrong wallet semantic.

Second layer: multiple followed traders can independently open the
same `(market, side)`. Chain token balance aggregates them into a
single row per asset. Correct comparison key is `(condition_id,
side)` across ALL source traders, case-insensitive on side because
chain API returns mixed capitalization.

### Fix

New helper `database/db.py::sum_open_shares_held_by_cid_side(cid, side)`:

```python
SELECT COALESCE(SUM(shares_held), 0) FROM copy_trades
WHERE condition_id=? AND LOWER(side)=LOWER(?) AND status='open'
```

`main.py::reconcile_db_vs_wallet()` gets a partial-ghost pass
alongside the existing set-based ghost/orphan check:

```python
GHOST_SHARE_TOLERANCE_PCT = 1.10   # chain must exceed DB sum by >10%
GHOST_SHARE_TOLERANCE_USD = 2.0    # AND value delta must exceed $2

for p in chain_positions:
    if p.cid in ghost_cids: continue  # full ghost already logged
    db_sum = sum_open_shares_held_by_cid_side(p.cid, p.outcome)
    if db_sum <= 0: continue
    if p.size <= db_sum * TOLERANCE_PCT: continue
    untracked = p.size - db_sum
    value = untracked * p.curPrice
    if value < TOLERANCE_USD: continue
    partial_ghosts.append(...)
```

New log format (additive, existing `[RECONCILE]` lines unchanged):

```
[RECONCILE] 1 partial-ghost markets ($43.64 untracked shares on otherwise-tracked positions)
[RECONCILE]   partial: 0x396b5a2de32f Under (chain=84.86 db=1.73 untracked=83.12 ~= $43.64) — Los Angeles Angels vs. New York Yankees: O/U 9.5
```

`dashboard/app.py::/api/live-data` gets two new fields per
`open_trades` entry: `ghost_shares` and `ghost_value_usd`. Both
`0.0` for clean positions. Non-zero means untracked on-chain
exposure. Frontend (style session) decides how to badge them.

### TDD coverage

9 tests in `tests/test_partial_ghost_detection.py`, two classes:

**`TestSumOpenSharesHeld`** (5 tests for the wallet-keyed helper,
kept around for future use cases like matching the UNIQUE index
semantics directly):

- `test_sum_shares_held_zero_when_no_rows`
- `test_sum_shares_held_includes_multiple_open_rows`
- `test_sum_shares_held_excludes_closed_and_baseline_rows`
- `test_sum_shares_held_excludes_different_wallet_or_market`
- `test_sum_shares_held_handles_null_shares_field`

**`TestSumByConditionIdSide`** (4 tests for the cid+side helper that
reconcile and dashboard actually use):

- `test_sums_across_wallets_same_cid_and_side`
- `test_excludes_other_side_of_same_market`
- `test_case_insensitive_side_match`
- `test_returns_zero_when_no_match`

All 9 RED-verified before GREEN (AttributeError: function missing).
Full suite post-fix: 83 pass / 1 pre-existing failure unrelated.

### Live verification (server, 20:26 UTC)

Manually triggered reconcile in subprocess:

```
[RECONCILE] 27 ghost (on-chain not in DB, $21.69 value), 0 orphan. DB open=9, chain=36
[RECONCILE]   ghost: 0xfc9d03a593a9 ($1.66) — Will The Left ...
[RECONCILE]   ghost: 0xe6a8bdcd0a55 ($3.87) — Will Iran strike ...
[RECONCILE]   ghost: 0xae6d3d20bc8f ($3.16) — Will Rafael López ...
[RECONCILE] 1 partial-ghost markets ($43.64 untracked shares on otherwise-tracked positions)
[RECONCILE]   partial: 0x396b5a2de32f Under (chain=84.86 db=1.73 untracked=83.12 ~= $43.64) — Los Angeles Angels vs. New York Yankees: O/U 9.5
```

`/api/live-data` Angels O/U 9.5 row:

```json
{
  "market_question": "Los Angeles Angels vs. New York Yankees: O/U 9.5",
  "side": "Under",
  "size": 1.0,
  "pnl_unrealized": -0.05,
  "ghost_shares": 83.12,
  "ghost_value_usd": 43.64
}
```

Other positions (5 sampled) all show `ghost_shares: 0.0,
ghost_value_usd: 0.0` — no false positives.

### What this commit does NOT do

- **Does not heal the $48 Angels ghost.** Those shares resolve with
  today's game; no code change undoes them. Healing historical
  ghosts requires a manual DB UPDATE and is deferred to a separate
  tool.
- **Does not update the frontend HTML.** Style session will consume
  `ghost_shares` / `ghost_value_usd` to draw a visual badge.
- **Does not change the existing `pnl_unrealized`.** The DB-slice
  PnL stays as the primary number. Ghost fields are additive
  context.
- **Does not touch `shares_held` semantics.** Frozen at buy time.
  No migration, no auto-recalc.

### Revert path

- Raise `GHOST_SHARE_TOLERANCE_USD` in `main.py` to `1e9` to silence
  detection without reverting the code.
- OR drop the `ghost_shares` / `ghost_value_usd` fields from the
  JSON — additive, safe to remove if a consumer breaks.
- `git revert <hash>` for full rollback.

---

## 2026-04-14 (ui) — Nav: rename DailyReport button to AI-Report and disable it

Follow-up to the header harmonisation. User wants the DailyReport nav entry
renamed and deactivated while the feature is being reworked.

- `_nav.html`: replaced `<a href="/reports">DailyReport</a>` with
  `<span class="dis" aria-disabled="true" title="Coming soon">AI-Report</span>`.
  Span has no href so it's not clickable; the `pointer-events:none` in the CSS
  makes it fully inert including hover.
- `terminal.css`: new `.nav .dis{...}` rule — dim text, transparent border,
  `opacity:.4`, `cursor:not-allowed`, `pointer-events:none`, `user-select:none`.
  Layout matches the other nav items so the button still fills the same slot.
- The `/reports` route itself and the `reports.html` template are UNCHANGED —
  only the nav entry is gone. The page is still reachable by direct URL.
- `active='dailyreport'` logic in `_nav.html` became dead but was removed as
  part of the span replacement.

Deployed live to walter@10.0.0.20, polybot restarted, curl verified on all 5
routes: each returns 200 and contains exactly one `AI-Report` label and one
`class="dis"` span. Remaining "DailyReport" string hits in brain.html (two CSS
comments) and reports.html (in-page footer "Super Sauna Club — DailyReport")
are user-invisible or intentional and were left alone.

## 2026-04-14 (ui) — Frontend harmonisation: remove grid background + unify header across pages

Visual-only pass, no bot-backend changes. Two fixes:

1. **Remove animated gold grid background** on all dashboard pages except Dailyreport.
   - Added a per-page opt-out in `dashboard/static/terminal.css`:
     `body[data-no-grid]::before{display:none !important}`
     (sits next to the existing `body::before` `gridDrift` rule).
   - Added `<body data-no-grid>` on `dashboard.html`, `brain.html`, `logs.html`,
     `index.html`, `history.html`, `wallet_detail.html`. `reports.html` (Dailyreport)
     stays untouched and still shows the grid. Matrix-rain canvas is unaffected and
     still renders everywhere except Dailyreport (pre-existing `data-no-matrix` there).

2. **Unified header across every page** — same HTML, same CSS, same desktop + mobile behavior.
   - Moved the canonical `.hd` header CSS from the `dashboard.html` inline `<style>` into
     `terminal.css` (`.hd`, `.hd-left`, `.hd-center` with `margin:0 auto`, `.hd-right`,
     `.digiclock` hidden by default / `html.wide .digiclock` visible, `.logo`, `.logo-sub`,
     `.live-tag`, `.status-tags`, `.st` dots). Single source of truth now.
   - Added explicit `.hd{flex-direction:row}` so `style.css`'s legacy
     `@media(max-width:782px){header{flex-direction:column}}` tag-rule can no longer
     hijack the new header on history/wallet.
   - Added a proper mobile breakpoint `@media(max-width:720px)` for the header: reorders
     logo/buttons to top row, pushes clock/live/status into a centered row below,
     shrinks fonts, buttons stay tappable.
   - Moved the `.fi` fade-in animation + `@keyframes fu` into terminal.css (was inline
     in dashboard.html).
   - Added `.hd-right{display:flex;align-items:center;gap:8px}` so button alignment is
     identical on every page.

3. **New shared header partial** `dashboard/templates/_header.html`. Uses the
   terminal.js-compatible IDs (`#digiClock`, `#sscSound`, `#sscWide`, `#stBot`,
   `#stApi`, `#stPoll`) that `terminal.js` already wires. Includes `_nav.html` for the
   nav links. Does **not** include the Lock button or Sauna scene — those stay
   dashboard-only because their JS handlers (`togLock`, sauna animation loop) live in
   `dashboard.html` inline.

4. **Page migrations** — replaced each page's hand-rolled `<header class="hd">…</header>`
   block with `{% with active='...' %}{% include '_header.html' %}{% endwith %}`:
   - `brain.html` → `active='brain'`
   - `logs.html` → `active='logs'`
   - `index.html` → `active='wallets'`
   - `history.html` → `active='copy'` (also added `<link terminal.css>` after style.css,
     `<script terminal.js>`, `<div class="scan">`)
   - `wallet_detail.html` → `active=None` (same style/js additions; the old `<h1>Wallet
     Detail</h1>` is now a dim `h2`-ish label below the header so the page keeps its
     section title without competing with the canonical logo wordmark)
   - `dashboard.html` keeps its own inline header (Lock button + sauna + extra buttons
     are bound by its own inline JS), but the duplicated header CSS block in its inline
     `<style>` was deleted now that the rules live in terminal.css. `.conn-dot` helper
     stays because it's dashboard-only.

### What **did not** change

- `reports.html` is untouched (both background and header).
- `terminal.js` logic unchanged — its expected IDs were already the canonical ones.
- No backend / bot / DB changes.

### Verification

- Jinja templates all parse.
- Flask `test_client` renders: `/copy` (dashboard) 200, `/brain` 200, `/logs` 200,
  `/wallets` 200, `/reports` 200 — all include the expected header HTML + `data-no-grid`
  (reports has no data-no-grid, as intended).
- `wallet_detail.html` and `history.html` render cleanly via direct `render_template`
  with mock data and include terminal.css + terminal.js + the shared header partial +
  the `.scan` line + `data-no-grid`.
- Browser verification (desktop 1440 + mobile 375/414 + toggle Wide mode + click
  Sound/Desktop buttons) is still **pending and should be done by the user** — Claude
  cannot browse headlessly here.

### Files touched

- `dashboard/static/terminal.css` — grid opt-out, header-CSS consolidation, mobile
  @media, `.fi` + `@keyframes fu`, explicit `flex-direction:row` on `.hd`.
- `dashboard/templates/_header.html` — **new** shared header partial.
- `dashboard/templates/dashboard.html` — `data-no-grid`, header CSS removed from inline
  `<style>`, `.fi` keyframe removed from inline `<style>`.
- `dashboard/templates/brain.html`, `logs.html`, `index.html` — `data-no-grid`, header
  replaced with partial include.
- `dashboard/templates/history.html`, `wallet_detail.html` — additionally load
  `terminal.css` + `terminal.js`, add `.scan` div, replace minimal legacy header with
  the partial include.
- `dashboard/templates/reports.html` — **untouched**.
- `dashboard/static/terminal.js`, `style.css` — **untouched**.

---

## 2026-04-14 (latest) — Prevent buy_shares ghost-share race + fix paper_follow for promoted candidates

Two related fixes spawned by a live incident earlier this afternoon.
While investigating a sudden $3.57 wallet drop, I traced the cause to
a single Angels vs Yankees O/U 9.5 chain position worth **$44.55**
against an **initialValue of $48.37** — far larger than any of our
documented buy paths should have produced at $1/trade size.

### The Angels ghost-share incident

Timeline of 2026-04-14 16:34-16:43 UTC:

- 16:29:12 — restart with `MAX_DAILY_TRADES=0` (commit `b41e76e`),
  but `_get_max_copies` hard-cap (commit `6acbe00`) not yet deployed.
- 16:32-16:33 — sovereign2013 opens ~14 position signals on the
  Angels/Yankees market (alternating Under @ 55c and Over @ 43c).
  copy_trader logs `[NEW]` for each.
- 16:34:16 — first ORDER BUY: `$1.00 @ 55c (limit 57c) | Under | FILLED`.
  `db.create_copy_trade` succeeds → `id=3547` row created.
- 16:35:20 → 16:42:56 — **40+ more buys fire at $1/10s cadence**,
  every single one `Under @ 55c FILLED`. Each one:
  1. `count_copies_for_market(sov, cid)` returned 1 (the existing
     id=3547 row).
  2. `_get_max_copies('sovereign2013')` returned **2** (from the
     auto-tuner-written `MAX_COPIES_PER_MARKET_MAP=sovereign2013:2.0`,
     before our hard-cap commit).
  3. Check `1 < 2` passed, scan proceeds to `buy_shares`.
  4. **`buy_shares` succeeds on-chain** — real USDC spent, shares
     credited to the wallet at `POLYMARKET_FUNDER`.
  5. `db.create_copy_trade(trade)` fires → raises
     `sqlite3.IntegrityError: UNIQUE constraint failed:
     copy_trades.condition_id, copy_trades.wallet_address` from
     `idx_copy_trades_open_dedup`.
  6. copy_scan's outer try/except swallows the exception and logs
     `[ERROR] Error in copy scan: UNIQUE constraint failed`.
  7. Next 10s tick repeats. Over and over.
- 16:43:08 — restart with `_get_max_copies` hard-cap (commit `6acbe00`).
  Now returns 1 → `count < 1` is False → every buy attempt skips
  before reaching `buy_shares`. Bleeding stopped.

**Damage audit**: Chain snapshot at 19:43 shows `size=84.8558` shares
of Under with `avgPrice=0.5699, initialValue=48.3677`. DB
`copy_trades` has ONE row for this market (`id=3547`, shares=1.73).

  ~47 missing shares ≈ ~$47 of on-chain spend with no DB tracking.

The shares are real, held in the wallet, and will resolve along with
the Angels/Yankees game today. Three possible outcomes:

  Under wins (total < 9.5):  85 × $1 = +$84.86 → +$36.49 profit
  Over wins (total >= 9.5):  85 × $0 =    $0   → -$48.37 loss
  Unresolved / timeout:      price drift continues

At the time of analysis (19:43 UTC) the market was pricing Under at
52.5c, implying ~52% chance of payout. This is effectively a 45%-of-
portfolio coinflip we never intended.

### Root cause (architectural)

`bot/copy_trader.py` has FIVE buy_shares call sites (pending queue,
position diff, event_wait, hedge_wait, activity scan). All five follow
the same pattern:

```python
with _buy_lock:
    if count_copies_for_market(...) >= _get_max_copies(...):
        continue
    if LIVE_MODE:
        order_resp = buy_shares(...)          # <-- spends real USDC
        if not order_resp: continue
        _apply_fill_details(...)
    trade_id = db.create_copy_trade(trade)    # <-- can raise IntegrityError
```

The order is: check, buy on-chain, write DB. If the DB write raises
`IntegrityError` (because the existing open row pre-check used
`count_copies_for_market` which doesn't match the UNIQUE partial
index exactly), the on-chain buy has already happened.

Why didn't the count check match? `count_copies_for_market` counts
`status='open' OR (status='closed' AND closed_at > -NO_REBUY_MINUTES)`.
It's compared against `_get_max_copies(trader)` which pulls from the
auto-tuner-written map. Before this morning's `min(val, 1)` hard-cap,
auto_tuner was writing `sovereign2013:2.0`, `KING7777777:3.0` etc.
based on tier classification. The DB `idx_copy_trades_open_dedup` is
a hard UNIQUE partial index on `(condition_id, wallet_address) WHERE
status='open'`, which allows **exactly 1** open row. So `max=2` with
`count=1` green-lights an insert that the DB will then reject.

The `_get_max_copies` hard-cap I deployed at 16:43 prevents this
today by forcing max=1 regardless of what auto_tuner writes, but the
**underlying race is still latent**: if anyone ever lifts the hard-
cap (e.g. to enable YES+NO hedging after a schema migration to
`(cond, wallet, side)`), or if the count check drifts from the DB
index semantics for any other reason, the same `buy_shares → fail
INSERT → ghost shares` cascade returns.

### Fix (this commit, defense-in-depth)

New helper `database/db.py::has_open_trade_for_market(wallet, cid)`
which matches the UNIQUE partial index semantics EXACTLY:

```python
SELECT 1 FROM copy_trades
WHERE wallet_address=? AND condition_id=? AND status='open'
LIMIT 1
```

Returns `True` iff an INSERT with `status='open'` for the same
`(wallet_address, condition_id)` would violate the index. Both the
2-column (current live) and 3-column (models.py) index variants are
compatible — the 3-column just needs an additional side filter which
can be added later.

Applied as a pre-check at all 5 buy paths BEFORE `buy_shares()` is
called. Each site now looks like:

```python
with _buy_lock:
    if count_copies_for_market(...) >= _get_max_copies(...):
        continue
    if db.has_open_trade_for_market(wallet, cid):   # NEW
        continue                                     # skip before spending USDC
    if LIVE_MODE:
        order_resp = buy_shares(...)                 # safe now
        ...
    trade_id = db.create_copy_trade(trade)           # INSERT cannot raise
```

The 5 patched call sites in `bot/copy_trader.py`:

  - 657  pending-buy queue (`[PENDING]` log)
  - 961  position-diff / DIFF path
  - 1326 event_wait path
  - 1520 hedge_wait / conviction path
  - 2197 activity scan / main path

### Piff's paper_follow_candidates fix (bundled)

Parallel report from piff during the incident: his side has `denizz`
in `status='promoted'` but 0 new paper_trades. Traced to
`bot/auto_discovery.py:256`:

```python
candidates = db.get_all_candidates("observing")
```

Hard-filtered to observing only, so candidates never get paper-
scanned once we decide to promote them. Fixed with a new
`db.get_active_candidates()` that returns both `observing` and
`promoted` statuses, excluding `inactive`. The intent of the paper-
scan loop is "track our highest-confidence candidates most closely",
and promoted are exactly that group, so this matches the original
intent.

`auto_discovery.paper_follow_candidates()` now calls
`get_active_candidates()` in place of the hardcoded observing filter.

### TDD coverage

Two new test files, all strict-RED-before-GREEN cycled:

`tests/test_has_open_trade.py` — 6 tests:
  - `test_returns_true_when_open_row_exists`
  - `test_returns_false_when_no_rows_exist`
  - `test_returns_false_when_only_closed_row_exists`
  - `test_returns_false_when_only_baseline_row_exists`
  - `test_returns_false_when_open_row_is_different_wallet`
  - `test_returns_false_when_open_row_is_different_market`

`tests/test_active_candidates.py` — 1 test:
  - `test_get_active_candidates_returns_observing_and_promoted`

All 7 new tests RED-verified (AttributeError: function doesn't exist)
before implementation, all GREEN after. Full test suite post-fix:
73 pass / 2 pre-existing failures unrelated to this change
(`test_brain_dedup.py` + `test_log_dedup.py` — both tracing back to
this morning's `brain._execute_loss_actions` disable commit
`11ed9e8`).

### Live verification

Post-deploy at 19:56:00 UTC:

  - `db.has_open_trade_for_market('0xdead', 'nonexistent')` → False ✓
  - `db.get_active_candidates()` → 38 rows
    (35 observing + 3 promoted, inactive excluded)
  - Promoted sample: `0x3e5b23e9f7 status=promoted paper_trades=88`,
    `0x6bab41a0dc status=promoted paper_trades=118`,
    `0xbaa2bcb5 status=promoted paper_trades=41`
  - 0 errors / 0 tracebacks since restart
  - update_prices cycle stable

### What this fix does NOT do

- **Does not recover the $48 Angels ghost exposure.** Those shares
  are real, held in the wallet, and will resolve with the game
  today. At 52.5c implied probability, expected value is roughly
  `0.525 × $84.86 + 0.475 × $0 - $48.37 = +$-3.82`, which is
  literally the current mark-to-market delta — the loss is already
  priced in. If Under wins it's +$36.49 vs the drawn position.
  Nothing in code can undo the ghost shares.

- **Does not add compensation on IntegrityError.** If the DB INSERT
  still somehow raises (e.g. a different index we don't know about),
  the bot will log the exception and continue but the on-chain buy
  stays. A more defensive version would `sell_shares` on the orphan
  immediately, but that adds its own risks (slippage, cascading
  fills) and the pre-check should make this path cold-dead.

- **Does not touch the `_get_max_copies` hard-cap.** That stays as
  belt-and-suspenders even with the pre-check. Two layers cheap.

- **Does not audit autonomous_scan / paper_follow buy paths for the
  same race.** Those paths don't call `buy_shares` directly
  (paper_follow is genuinely paper, autonomous_trades table is
  empty, meaning autonomous_signals never fires a real buy). If that
  ever changes, the same pre-check must be added there.

### Revert path

- Delete both `has_open_trade_for_market` calls in a buy path →
  pre-check gone, structure-of-latent-race returns.
- Remove the `get_active_candidates` call in auto_discovery →
  paper-scan goes back to observing-only.
- Both reverts are safe at runtime (bot continues to function), they
  just re-introduce the bugs the fix is addressing.

---

## 2026-04-14 (later) — Magnitude-aware price range calibrator + B3 staged rollout

Option B for the MIN/MAX_ENTRY_PRICE_MAP problem (Option A was the
earlier commit that disabled the auto-write as a stopgap). Replaces
the tier-based WR heuristic with a per-bucket verified-PnL compute,
but gates auto-application behind a conservative sample-size
threshold so small-sample false positives can't nuke the $106 equity.

### What the new compute does

`bot/price_range_calibrator.py::compute_verified_price_range` —
new module, TDD-built with 4 unit tests. For each trader:

1. Fetch all closed copy_trades with `usdc_received IS NOT NULL AND
   actual_size IS NOT NULL` (verified slice only — unverified rows
   with formula-based pnl_realized are excluded).
2. Bucket by 10c on actual_entry_price (fall back to entry_price).
3. A bucket is "good" if `n >= min_samples_per_bucket (2)` and
   `pnl > min_bucket_pnl (-2.0)`.
4. Require `len(rows) >= min_total_trades` and `len(good) >= 2`,
   else return None (caller falls back to tier/manual).
5. Return `(min_good_bucket / 10, (max_good_bucket + 1) / 10)`.

Known suboptimality: the "lowest good to highest good" heuristic
absorbs bad middle buckets into the range. For KING7777777's actual
distribution the Kadane-optimal window would be ~$3 better per 53
trades. The simple algorithm is retained for clarity; documented in
the calibrator docstring as an upgrade path if the gap grows.

### TDD tests (bot/price_range_calibrator.py via tests/test_price_range_calibration.py)

Four RED→GREEN cycles verified the compute:

1. `test_xsaghav_like_bimodal_absorbs_middle_gap` — synthetic data
   matching xsaghav's real pattern, asserts (0.30, 0.90).
2. `test_insufficient_total_trades_returns_none` — 9 trades across
   3 good buckets but below the 20-trade minimum → None.
3. `test_all_losing_buckets_returns_none` — every bucket below the
   pnl threshold → None.
4. `test_unverified_trades_excluded_from_count` — 15 verified + 30
   unverified; without the `usdc_received IS NOT NULL` filter the
   45 rows would pass the count guard and produce a spurious range.
   Strict-TDD verified by temporarily removing the filter and
   watching the test fail with `(0.3, 0.8) is not None` before
   reinstating it.

All 4 green in 0.05s. Full suite: 67/68 pass (the 1 failure is the
pre-existing test_brain_dedup.py::test_bad_category_losses_log_once
from this morning's _execute_loss_actions disable, unrelated).

### Auto-tuner integration (bot/auto_tuner.py)

Reverts Option A's "disable the write" and re-enables writing
MIN_ENTRY_PRICE_MAP / MAX_ENTRY_PRICE_MAP, with three structural
guards stacked around the calibrator call:

1. **Pre-read existing settings.env values** before tier-default
   seeding so manual overrides (like Option A's xsaghav:0.30-0.85)
   are not silently clobbered. The loop at the top of `auto_tune`
   now does `min_entry_map[name] = _pre_existing_min.get(name,
   s["min_entry"])` instead of always using the tier default. First
   dry-run of Option B missed this guard and produced a regression
   that reset xsaghav to tier (0.45-0.65) — caught in the dry-run
   diff, fixed before deploy.

2. **Calibrator gate via `PRICE_CALIBRATOR_MIN_TRADES = 100`** —
   only traders with ≥100 verified trades get auto-updated. Today
   that's sovereign2013 alone (n=130). xsaghav (n=73), KING7777777
   (n=53), fsavhlc (n=18), Jargs (n=13) fall through to their
   existing values (manual or previous).

3. **Non-followed wallet preservation** — auto-tuner only classifies
   followed traders, but the settings.env MIN/MAX maps contain
   discovered wallets (0x3e5b23e9f7, aenews2, 0x6bab41a0dc) that
   the brain/filter logic uses for emerging trader tracking. The
   merge loop at the bottom of the update block pulls those entries
   back in so they aren't dropped when the new map is written.

### Staged rollout plan (documented inline in auto_tuner.py)

```
  Today     threshold=100 → only sovereign2013 auto-updates
  Week 2    lower to 50 if sovereign stable → xsaghav + KING qualify
  Week 4    lower to 30 → fsavhlc + Jargs qualify
  Week 6+   lower to 20 → full autopilot
```

Each step is a 1-line edit to `PRICE_CALIBRATOR_MIN_TRADES` in
`bot/auto_tuner.py`. If any step shows a verified regression, raise
the number back up — the guard is bidirectional. This is
evidence-gated rollout: the system proves itself trader by trader
over weeks rather than the all-at-once option.

### Why conservative over aggressive

Worst-case blast radius on a 2-sample false-positive bucket: ~$75
per 2h cycle of mis-configured window. At $106 equity with 6 cycles
per day, that's -$450/day worst case — game-over in hours if the
window is wrong. B1 (threshold=20) would have auto-opened KING's
10-20c bucket based on 2 samples, exactly the noise vulnerability.
B3 (threshold=100) only trusts sovereign's 130-sample distribution,
where bucket-level n ≈ 15-25 and means start stabilizing.

### Dry-run + live verification

Dry-run on server (copy settings.env to /tmp, run patched auto_tune
in subprocess, diff, restore) showed exactly:

  sovereign2013:  0.38-0.75 → 0.30-0.90  (calibrator, n=130)
  xsaghav:        0.30-0.85 → 0.30-0.85  (manual preserved)
  KING7777777:    0.53-0.60 → 0.53-0.60  (preserved, n<100)
  fsavhlc:        0.45-0.65 → 0.45-0.65  (preserved, n<100)
  Jargs:          0.38-0.75 → 0.38-0.75  (preserved, n<100)
  Non-followed wallets: unchanged (0x3e5b23e9f7, aenews2, 0x6bab41a0dc)

Post-deploy + manual auto_tune() trigger in production, verified
via `ct._reload_maps()` live values match expectations. No errors
in journalctl since restart at 19:15:02 UTC.

### How to monitor this change

- `ssh walter@10.0.0.20 'curl -s http://localhost:8090/api/upgrade/tuner-settings'`
  shows the live map after each auto_tune cycle.
- Look for `[TUNER] <trader> price range: tier=X.XX-X.XX -> verified=X.XX-X.XX`
  log lines in journalctl — sovereign should be the only match for the
  next 1-2 weeks.
- `[TUNER] <trader> price range: <100 verified trades, keeping tier default`
  shows the other 4 traders falling through.
- Check DB verified PnL for sovereign specifically after 24-48h in
  the expanded 30-90c range — if it turns negative, raise threshold
  back to 200 or revert the one line.

### Revert path

- Raise `PRICE_CALIBRATOR_MIN_TRADES` to e.g. 1000 in auto_tuner.py
  and redeploy → calibrator stops applying, fall back to existing
  values.
- OR manually edit settings.env `sovereign2013:0.3 -> 0.38` and
  `sovereign2013:0.9 -> 0.75` — hot-reloaded on next copy_scan.
- No code revert needed in either direction.

---

## 2026-04-14 (later) — Disable MIN/MAX_ENTRY_PRICE_MAP auto-write + loosen xsaghav to 30-85c

After the outcome_tracker DESC fix started populating Filter Precision
Audit with fresh labels, I spot-checked whether xsaghav's tight 45-65c
price window was actually justified. Queried the 73 verified closed
trades for xsaghav grouped by 10c entry-price bucket. Result was
damning for the auto-tuner's WR-based approach:

```
BUCKET         N    W    L     WR%     VOL    NET P&L
------------------------------------------------------
10-20c         1    1    0  100.0%  $ 2.76  $  +3.22
20-30c         1    0    1    0.0%  $ 9.37  $  -3.76
30-40c         6    4    2   66.7%  $38.64  $ +41.65   BLOCKED (best bucket!)
40-50c        13    4    9   30.8%  $32.32  $  -1.00
50-60c        14    6    8   42.9%  $58.69  $ +25.43   inside
60-70c        23   13   10   56.5%  $95.68  $  +7.77   partial
70-80c         9    4    5   44.4%  $23.40  $  +7.82   BLOCKED
80-90c         6    5    1   83.3%  $62.27  $  +9.76   BLOCKED (83% WR!)
------------------------------------------------------
TOTAL         73                              $+90.89

INSIDE 45-65c (let through):  n=37 WR=51.4% vol=$154 pnl=$+33.20 ROI=+21.5%
OUTSIDE (blocked):            n=36 WR=50.0% vol=$169 pnl=$+57.69 ROI=+34.2%
```

The 30-40c bucket alone contributes +$41.65 — **46% of xsaghav's all-time
verified profit** — and was fully blocked by MIN_ENTRY_PRICE=0.45. The
80-90c bucket had 83% WR and was fully blocked by MAX_ENTRY_PRICE=0.65.
The auto-tuner optimized for win-rate bounds inside a narrow band and
in the process amputated the trader's two most profitable zones.

### Why the WR-heuristic is wrong

Polymarket has asymmetric payoffs: a 30c bet paying out 100c is 3.3x
profit, while a 80c bet paying out 100c is 0.25x profit. A trader can
have 30% WR at 30c and still be net profitable (need only 25% to break
even), but a 60% WR at 80c is marginal. The tier_defaults table in
auto_tuner.py uses a single min/max band per tier based on "safe" WR
zones, which ignores this magnitude asymmetry entirely. Same bug we
fixed for CATEGORY_BLACKLIST_MAP this morning.

### Fix (Option A — quick, this commit)

- `bot/auto_tuner.py::auto_tune` — disable the `_update_map_setting`
  calls for `MIN_ENTRY_PRICE_MAP` and `MAX_ENTRY_PRICE_MAP`. The tier-
  based computation still runs and logs `[TUNER] Would set ... (DISABLED,
  manual managed)` so the intent is visible in journalctl. Removed the
  PATCH-024 "keep tighter" merge block since it's no longer needed.
  Other maps (BET_SIZE_MAP, TRADER_EXPOSURE_MAP, etc.) continue to be
  auto-written unchanged.

- `settings.env` on server — manually set `xsaghav:0.30` (MIN) and
  `xsaghav:0.85` (MAX). All other trader bounds unchanged. The bounds
  were picked to capture the 30-40c bucket (+$41.65) and the 70-90c
  range (+$17.58) without extending into 20-30c (1 sample, -$3.76) or
  90-100c (no sample, unknown risk).

### Verification

- `ct._reload_maps(); ct._MIN_ENTRY_PRICE_MAP.get('xsaghav')` returned
  `0.3`; `_MAX_ENTRY_PRICE_MAP.get('xsaghav')` returned `0.85`.
- Other traders unchanged (sovereign2013 still 0.38/0.75).
- Manually called `auto_tuner.auto_tune()` in a subprocess and re-read
  `settings.env` — xsaghav MIN/MAX values preserved (0.30/0.85). The
  disabled writer honored the manual values.
- Price-range simulation at 15c/32c/40c/50c/60c/70c/82c/95c confirms
  the new window blocks only 15c and 95c, passing the 30-85c band.
- `0` errors / `0` UNIQUE fails / `0` max-daily skips since restart
  at 18:50:49 UTC (`sudo journalctl -u polybot --since '18:50:49'`
  filtered for error/traceback/exception).

### Expected uplift (extrapolation from verified history)

If xsaghav's next 73 trades mirror his verified distribution and the
per-bucket PnL is stable, the loosened window should capture roughly
+$58 additional profit on top of whatever the 45-65c band would have
produced. In practice the sample is small (6 trades in 30-40c, 6 in
80-90c) so real uplift will vary — could be +$30 or +$90 depending
on which buckets his next runs land in. The point is the current
window mechanically clips the profitable tails, which is
correctable regardless of the precise expected value.

### What this does NOT do

- Does **not** touch sovereign2013, KING7777777, fsavhlc, Jargs. Their
  verified samples also deserve the same bucket analysis — deferred to
  Option B (magnitude-aware auto-tuner compute). This commit is
  xsaghav-only because that is where the spot analysis pointed.
- Does **not** fix the root cause in auto_tuner.py's tier table. That
  table still maps tier → tight WR-optimized bands. Option B will
  replace the computation with verified-PnL per-bucket logic and
  re-enable the writer. Until then, MIN/MAX_ENTRY_PRICE_MAP joins
  CATEGORY_BLACKLIST_MAP on the manual-managed list.
- Does **not** change `MAX_ENTRY_PRICE_CAP=0.97` (the global absolute
  hard cap). That stays as a post-slippage safety.

### Revert

If xsaghav's trades at 30-40c or 80-90c turn out to bleed money in
the live window, revert is a 2-second edit in settings.env:
`xsaghav:0.30 -> xsaghav:0.45` and `xsaghav:0.85 -> xsaghav:0.65`.
Hot-reloaded on next copy_scan tick. No code revert needed — the
disabled writer stays disabled regardless.

---

## 2026-04-14 (later) — Outcome tracker: DESC order + limit 500 to unblock Filter Precision Audit

After the MAX_DAILY_TRADES removal (earlier today) turned on the full
block-logging flow again, I looked at the Filter Precision Audit panel
to see if it would start recommending "LOOSEN" on aggressive filters
(e.g. xsaghav getting 60 blocks per 25 minutes, mostly `price_range`
and `conviction_ratio`, even though xsaghav is all-time verified +$91).
The panel only showed 3 buckets: `category_blacklist` KEEP,
`event_timing` NO_CONFIDENT, `min_trader_usd` INSUFFICIENT. Everything
else missing.

### Why

`outcome_tracker.track_outcomes` calls
`db.get_blocked_trades_unchecked(limit=100)` every 30 min. The DB
function used `ORDER BY created_at ASC LIMIT ?` — FIFO. With blocked
volume at 6500-150k/day and tracker capacity at 4800/day, the queue
was in permanent overflow. The tracker was stuck on the oldest 2026-04-10
`category_blacklist` rows from id≈17k onwards, never reaching the more
recent reasons. Evidence:

```
Unchecked blocked_trades per reason (2026-04-14):
  category_blacklist  307628   min_id=17221  oldest=Apr 10 23:16
  event_timing        102446   min_id=17233  oldest=Apr 10 23:16
  price_range          65373   min_id=36296  oldest=Apr 11 00:35  (never reached)
  exposure_limit       37144   min_id=40112  oldest=Apr 11 00:51  (never reached)
  no_rebuy             14073   min_id=42368  oldest=Apr 11 01:00  (never reached)
  min_trader_usd        3218
  conviction_ratio      2180   min_id=442771 oldest=Apr 12 20:53  (far out of reach)
```

Label rate per reason showed the damage:
```
  category_blacklist    4.5%  labeled
  event_timing          2.4%  labeled
  min_trader_usd        2.2%  labeled
  max_copies           13.0%  labeled
  price_range         0.003%  labeled  (2 rows out of 65k)
  exposure_limit        0%    labeled
  conviction_ratio      0%    labeled
  no_rebuy              0%    labeled
```

Because `_build_block_training_data` filters `WHERE would_have_won IS
NOT NULL`, the block-model and Filter Precision Audit literally could
not see any of the zero-labeled reasons. They were structurally blind.

### Fix

- `database/db.py::get_blocked_trades_unchecked` — flip `ORDER BY` from
  `ASC` to `DESC`. Tracker now processes newest unchecked rows first.
  Historical backlog from Apr 10-11 stays unlabeled, which is fine —
  those markets are long resolved, and the Filter Precision Audit
  already drops stale `category_blacklist` rows via its own stale-filter
  anyway (13,619 stale rows dropped per audit call today).

- `bot/outcome_tracker.py::track_outcomes` — bump `limit` from 100 to
  500 per run. At 2 runs/hour that's ~24k labels/day vs old 4800/day.
  Each row costs ~0.2s (rate limit sleep), so 500 rows take ~100s per
  30min run — 94% idle on the interval, no overlap risk.

### Expected effects

- Tracker starts labeling NEW unchecked rows at ~1000/hour across all
  reasons proportionally to their current inflow rate.
- Within 1-2 hours the reasons that currently have 0 labels
  (`price_range`, `exposure_limit`, `conviction_ratio`, `no_rebuy`)
  should have 50-200 labels each.
- Next `ml_train` cycle (6h interval from main.py apscheduler, or
  manually triggerable) retrains `ml_block` on the expanded dataset
  and re-exposes the new reasons to Filter Precision Audit's
  `min_samples=100` threshold.
- Filter Precision Audit panel on `/brain` dashboard should show 6-8
  buckets instead of 3 within ~1 day.
- Actionable signal for xsaghav: if `price_range`/`exposure_limit`
  blocks on winning traders show high WR in the labeled slice, the
  LOOSEN recommendation will surface automatically.

### What was not done

- **No stratified sampling** (e.g. round-robin by reason) — kept the
  single-field ORDER BY for simplicity. The DESC flip already solves
  the visibility problem for new reasons, and we can revisit if
  coverage skew becomes an issue later.
- **No deletion of the historical backlog** — the 480k stale rows
  stay in the table. They're not processed but don't interfere with
  the audit (stale-filter already handles them) and serve as
  historical record.

---

## 2026-04-14 (later) — Hard-cap _get_max_copies at 1 to match DB UNIQUE index

Immediately after removing `MAX_DAILY_TRADES=30` and fixing the compound slowdown (previous entry), the bot resumed active copying and uncapped a latent bug that the daily cap had been masking for 2 days: `sqlite3.IntegrityError: UNIQUE constraint failed: copy_trades.condition_id, copy_trades.wallet_address` firing every 10s on sovereign2013 positions.

### Why

Root cause is a contradiction between two config paths:

- **Schema**: `idx_copy_trades_open_dedup` is a UNIQUE partial index on `(condition_id, wallet_address) WHERE status='open'`. The DB permits **exactly one** open row per (market, trader).
- **Auto-tuner**: `MAX_COPIES_PER_MARKET_MAP` is auto-written per trader tier. STAR/SOLID traders get values >1 (observed: `sovereign2013:2.0` post-restart).
- **Pre-insert check** (`bot/copy_trader.py:1504`): `if count_copies_for_market(...) >= _get_max_copies(...): continue` — with `count=1, max=2` the check green-lights a second INSERT, which then trips IntegrityError.

The whole `copy_scan` tick is wrapped in try/except that catches the error and aborts the cycle, so other markets in the same scan are skipped. Not data-corrupting, but it silently stops progress whenever the affected trader opens a repeat position.

### Why it was hidden until now

`MAX_DAILY_TRADES=30` returned from `copy_scan` before reaching the hedge-wait buy path in most cycles (cap was usually hit by noon). With the cap removed, sovereign's 2.0 setting got a chance to fire and immediately started bleeding errors. Memory says "MAX_COPIES_PER_MARKET bug cost $420 previously" — this is the same bug surface.

### Fix

`bot/copy_trader.py::_get_max_copies` — hard-cap return value at `min(val, 1)` for both the mapped and global-fallback branches. Auto-tuner can continue writing 2.0/3.0 into settings (separate scope), the reader just ignores anything above 1 until the schema is fixed. Comment explains the reasoning so a future reader doesn't "optimize" it away.

### Verification post-deploy (2nd restart 16:43:08 UTC)

- `_get_max_copies('sovereign2013')` → **1** (was 2)
- `_get_max_copies(...)` for all followed traders → **1**
- 0 `UNIQUE constraint failed` in logs since 16:43:08
- `update_prices` cycles still at 3-4s (no regression from the `closed_limit` fix)
- `blocked_trades` + `copy_trades` still being written normally

### What still needs attention

- **Auto-tuner** continues to write `sovereign2013:2.0` etc. into settings.env every 2h. Harmless with the hard cap but misleading in the dashboard. Proper fix: patch `bot/auto_tuner.py` to emit `min(tier_val, 1)` OR change the schema to allow per-side open rows (cond, wallet, side) — deferred.
- **Root schema question**: is there any scenario where we want multiple open rows per (market, trader)? If the intent was "average in" on double-downs, the correct model is to UPDATE the existing row's size, not INSERT a second row. That's a bigger refactor.

### ⚠️ Heads-up for piff: schema drift between models.py and live DB

While validating that piff can pull main cleanly, I found a drift that has nothing directly to do with my fixes but changes how the hard-cap lands on different DBs:

**`database/models.py:233`** (source of truth):
```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_copy_trades_open_dedup
  ON copy_trades(condition_id, wallet_address, side) WHERE status='open';
```

**Server live DB** (as of 2026-04-14 on kohle.supersauna.club):
```sql
CREATE UNIQUE INDEX idx_copy_trades_open_dedup
  ON copy_trades(condition_id, wallet_address) WHERE status='open';
```

The index was widened to include `side` in the code at some point, but `CREATE INDEX IF NOT EXISTS` is a no-op when an index with that name already exists — so the old 2-column version sticks on any DB that wasn't freshly initialized after the code change. Our server DB was initialized earlier and still has the 2-column version.

**Why it matters for piff**:

- **Intent of the code**: a trader can legitimately hold YES + NO on the same market (hedge position). Auto-tuner writing `sovereign2013:2.0` into `MAX_COPIES_PER_MARKET_MAP` reflects that intent.
- **Reality on 2-column DBs** (ours): only 1 open row per (cond, wallet) — ANY second insert trips the UNIQUE constraint regardless of side. Hard-cap at 1 is correct.
- **Reality on 3-column DBs** (fresh init after the code change): 1 YES + 1 NO can coexist. Hard-cap at 1 silently *reduces* functionality — you'd lose the ability to hedge.

**How piff should check their own DB state**:

```bash
sqlite3 database/scanner.db \
  "SELECT sql FROM sqlite_master WHERE name='idx_copy_trades_open_dedup'"
```

- **Output ends with `(condition_id, wallet_address)`** → same as our server, pull + run is correct, the hard-cap matches your DB reality.
- **Output ends with `(condition_id, wallet_address, side)`** → your DB is already side-aware. My hard-cap will block hedging. You have two choices:
  1. Revert the `_get_max_copies` hard-cap locally on your branch and keep the auto-tuner's 2.0 values working as intended.
  2. Accept the reduction for now — it's only a loss if you actually make use of YES+NO hedging.

**Recommended path forward (both sides)**: a proper migration that DROPs and re-CREATEs the index with `side`, plus reverting the hard-cap. That's a separate, deliberate session — not bundled into the current perf fix — because it changes observable behavior (YES+NO coexistence enabled) and needs a careful scan of all queries that assume "1 open row per (cond, wallet)" (e.g. `update_copy_positions` fallback lookup at `main.py:319-322` uses `WHERE condition_id=? AND status='open'` with `.fetchone()` — this would become ambiguous under side-aware coexistence).

No action required from piff for the current commits. Read this heads-up, check your DB, decide on your side.


---

## 2026-04-14 (later) — Remove MAX_DAILY_TRADES cap + fix update_prices compound slowdown

Two related fixes after a rigorous audit of "why is the bot idle?" and "why does apscheduler keep skipping update_prices?"

### Why

**1. MAX_DAILY_TRADES cap was silently crippling learning.** The cap was added 2026-04-12 as a "safety rail against runaway scans" after the MAX_COPIES_PER_MARKET bug. That bug was fixed in the big 2026-04-14 session, but the cap stayed. The early `return 0` in `bot/copy_trader.copy_scan` sits BEFORE the block-logging logic, so hitting 30/30 skips the entire scan — including `blocked_trades` INSERTs that feed `ml_block` and the Filter Precision Audit. Observed on Apr 14: cap hit at 13:46 UTC → **10+ hours/day idle** with ~90% drop in `blocked_trades` rows (e.g. 336k rows on Apr 11 unrestricted → 4.9k today). The magnitude-aware blacklist rebalance from this morning cannot prove itself with so little data. Pre-cap avg P&L per trade was +$1.07 (Apr 5-11, 209 verified trades, +$223); post-cap has been −$0.51 (77 trades, −$39) but that correlates with a regime change that started Apr 10 — the cap didn't help either way, it just silenced the feedback loop.

**2. `update_prices` took 50-73s with a 60s interval** → apscheduler `max_instances=1` caused 7 skips per 95min. Root cause measured in-process: `fetch_wallet_closed_positions(wallet, limit=last_count+100)` in `bot/copy_trader.update_copy_positions`. The API page size is hard-capped at 50 per page, and `last_closed_count` grows monotonically as we observe the trader's history. For the currently-open Keiko Fujimori trade (wallet's 2405 historical closures), this translates to 50 serial API calls = **13.87s per cycle** spent just on ONE wallet's closed-history fetch. For sovereign2013 (5050 historical closures) it would compound to ~28s. In-process timing breakdown:

```
db.get_open_copy_trades (n=2)                0.01s
price_tracker.subscribe x2                   0.65s
fetch_wallet_positions (n=50)                0.20s
fetch_wallet_closed_positions (limit=506)    2.34s   (fsavhlc)
fetch_wallet_closed_positions (limit=2505)  13.87s   (Keiko trader)
chain /positions paginated (n=94)            0.22s
DB lookups for 94 positions                  0.01s
get_wallet_balance + snapshot                0.32s
----------------------------------------------------
TOTAL isolated                              18.22s
TOTAL production (+dashboard contention)    49-73s
```

The fix is to fetch a bounded window instead of the full history. New closures per cycle are 0-3 in practice; 50 is a comfortable margin.

### Changes

- `bot/copy_trader.py::update_copy_positions` — `closed_limit = 50` (hard cap). Comment explains why the growing `last_closed_count+100` pattern was removed. The call to `db.update_closed_count(wallet, len(closed_positions))` is retained (harmless) so we can restore the old logic if needed. Measured: 13.87s → 0.37s for the Keiko wallet (37× faster on that one call).

- `settings.env` — `MAX_DAILY_TRADES=0` (disabled). `settings.example.env` matched. Other safety rails (`MAX_OPEN_POSITIONS`, `MAX_DAILY_LOSS`, per-trader `TRADER_EXPOSURE_MAP`, `BET_SIZE_MAP`) remain active.

### Expected effects

- Bot resumes copy_scan immediately (no more `[SKIP] Max daily trades reached`).
- `blocked_trades` table grows again at natural rate (tens of thousands/day), feeding ml_block and Filter Precision Audit with fresh data.
- `update_prices` cycle drops from ~52s to ~30-40s (the 13s closed-fetch savings plus residual dashboard-contention overhead). Skip rate should drop to near zero with 2 open trades. Still a scaling risk if 3+ open trades on high-history wallets — deferred to a later session (proper fix: use `trader_closed_positions` DB cache for incremental sync instead of API polling).

### What was NOT changed

- `filter_audit` log spam (926 lines/95min from dashboard polling hitting `/api/brain/filter-precision`). Lower priority, defer.
- The `last_closed_count` tracking column in `trader_scan_config`. Still written by the retained `db.update_closed_count` call. Unused now but harmless and allows quick revert.

---

## 2026-04-14 (earlier) — Disable auto-write of CATEGORY_BLACKLIST_MAP

Per piff-philosophy: `CATEGORY_BLACKLIST_MAP` joins `PAUSE_TRADER` / `THROTTLE_TRADER` / `KICK_TRADER` on the list of brain auto-actions that are disabled and must be managed manually. Manual edits were being overwritten every 2h cycle.

### Why

After the backfill session, manually cleaning `CATEGORY_BLACKLIST_MAP` to match verified per-(trader, category) PnL (xsaghav:cs +$70.51, sovereign2013:nba +$41.39, fsavhlc:geopolitics +$19.80, etc.) and restarting the bot, the Brain Engine fired its startup-triggered cycle ~5 minutes later and re-added most of the removed entries. The brain classifier reads `pnl_realized` which for the majority of historical rows still reflects pre-backfill formula estimates, so the auto-add sees losses that are actually verified wins.

Two write paths were silently undoing the manual cleanup:

1. **`bot/brain.py::_add_category_blacklist`** — called from `_execute_loss_actions` when a loss is classified as `BAD_CATEGORY`. Writes directly to `settings.env::CATEGORY_BLACKLIST_MAP`.

2. **`bot/auto_tuner.py::_update_blacklist_setting`** — called from `auto_tune()` at the end of each brain cycle. Does a MERGE of (existing blacklist ∪ computed blacklist), so entries removed from the map get re-added if `_get_category_blacklist(trader)` returns them based on full-history `pnl_realized`.

### Changes

- `bot/brain.py::_execute_loss_actions` — no longer calls `_add_category_blacklist`; instead logs a `BLACKLIST_RECOMMENDED` row in `brain_decisions` for dashboard visibility. The computed recommendation is preserved, only the settings write is disabled.

- `bot/auto_tuner.py::auto_tune` — the call to `_update_blacklist_setting(content, blacklist_map)` is replaced with an info log `[TUNER] Would blacklist (DISABLED, manual): {...}`. The per-trader bl computation still runs (visible in the existing `[TUNER] trader: TIER | ... bl: [...]` log line), just not persisted.

### What still works

- `_revert_obsolete_blacklists` in brain.py still runs each cycle and removes entries where 7d verified data shows the category is actually profitable (≥3 trades, WR ≥50%, PnL ≥0). That's the auto-recovery side and stays enabled — it can only REMOVE stale entries, not add new ones.

- Per-trader blacklists are shown on `/brain` dashboard's `[TUNER]` section so the user can see what the brain would recommend.

- `BLACKLIST_RECOMMENDED` rows in `brain_decisions` table are visible in the stream for post-hoc review.

### Manual blacklist applied this session

After cleanup based on verified post-backfill per-(trader, category) PnL:

```
CATEGORY_BLACKLIST_MAP=KING7777777:lol|valorant,sovereign2013:nhl,xsaghav:dota
```

Reduced from 13 entries to 4. The removed entries all had verified positive PnL (xsaghav:cs +$70.51, sovereign2013:nba +$41.39 / :tennis +$14.90 / :mlb +$7.14, fsavhlc:geopolitics +$19.80, etc.). Only verified losers retained: xsaghav:dota −$38.81, KING:lol −$19.65, KING:valorant −$9.14, sovereign:nhl −$5.92.

### Files touched

- `bot/brain.py` — `_execute_loss_actions` no longer auto-writes blacklist
- `bot/auto_tuner.py` — `auto_tune` no longer calls `_update_blacklist_setting`
- `settings.env` (on server, not committed) — `CATEGORY_BLACKLIST_MAP` manually cleaned per above

## 2026-04-14 — Close-path atomic usdc_received (audit Step 1+4)

Structural fix for the 87%-NULL `usdc_received` bug that drove the backfill work earlier in the session. The root cause (per `docs/close_logic_audit.md`) was that `close_copy_trade()` didn't accept `usdc_received`, forcing callers into a 2-step `close_copy_trade() → update_closed_trade_pnl()` pattern. Any error or early-return between the two steps left the row with `usdc_received=NULL` permanently.

### `close_copy_trade()` signature

`database/db.py::close_copy_trade(trade_id, pnl_realized, close_price=None, usdc_received=None)`. When `usdc_received` is provided, the UPDATE stores it AND recomputes `pnl_realized` from `usdc_received - COALESCE(actual_size, size, 0)` in the same statement so the column always reflects the real wallet delta. `pnl_realized` passed by the caller is ignored in that branch. The legacy 2-step path (pass `usdc_received=None`) is preserved for paper-mode and reconcile callers.

### 7 close-paths migrated

All 7 sell+close paths now extract `usdc_received` from the `sell_shares()` response and pass it directly into the single close call:

1. `FAST-SELL` (`update_copy_positions` around line 1636)
2. `STOP-LOSS` (normal path, ~2438)
3. `TRAILING-STOP` (~2485)
4. `TAKE-PROFIT` (~2517)
5. `STOP-LOSS` trader-closed path (~2555)
6. `trader-closed-it` (~2609)
7. `miss-close` (~2715)

Plus the two HIGH-severity paths in `main.py`:

8. `AUTO-CLOSE-lost` (`main.py:335`) — passes `usdc_received=0.0` atomically
9. `AUTO-CLOSE-won` (`main.py:361`) — passes `usdc_received=_usdc_won` atomically

Each migrated call also updates the local `pnl` variable via a new `_real_pnl_from_sell(trade, sell_resp)` helper so the `logger.info` line below logs the verified PnL instead of the formula estimate.

### New helpers

`bot/copy_trader.py::_usdc_from_sell(sell_resp)` — extract the `usdc_received` field from a sell response, returning 0 for None/missing. `_real_pnl_from_sell(trade, sell_resp)` — compute verified PnL from the sell response without any DB side effects. The old `_correct_sell_pnl(trade, sell_resp, trade_id)` helper is kept as a thin compat shim (no longer called by anything in the codebase, but retained in case external code depends on it).

### What this prevents going forward

Every new close now writes `usdc_received` in the same atomic UPDATE that sets `status='closed'`. There is no intermediate state where `status='closed' AND usdc_received IS NULL`. Future trade_performance / ML / Brain calculations see verified data from the first observation — the backfill tool becomes unnecessary for rows created after this commit.

### Files touched

- `database/db.py::close_copy_trade` — signature + SQL expanded
- `bot/copy_trader.py` — 7 close paths + 2 new helpers, old `_correct_sell_pnl` demoted to compat shim
- `main.py` — 2 AUTO-CLOSE paths refactored to atomic
- `tests/test_ml_time_split.py` — assertions updated for the `[ML-COPY]` log tag and the 6-tuple `_build_training_data` return shape

### Verification

`close_copy_trade` signature verified on the live server (`usdc_received` is in the parameter list). `_usdc_from_sell(None)=0`, `_usdc_from_sell({"usdc_received":2.47})=2.47`, `_real_pnl_from_sell({actual_size:2.0}, ...)=0.47`. Test suite runs 63 passing + 1 pre-existing flake (`test_log_dedup.TestBrainOscillationMutex.test_revert_skips_trader_in_mutex` was failing before this commit too). Bot restarted cleanly on walter, no errors in the journal.

## 2026-04-14 — Two-model ML split + Filter Precision Audit

User insight: blocked_trades are not training data for the live predictor — they are audit data for the filters themselves. Each filter reason is a policy decision that should be measured: does it block losers (correct) or winners (wrong)?

### Architecture

`bot/ml_scorer.py` now has two specialized models instead of one merged:

- **`ml_copy.pkl`** — trained ONLY on `copy_trades`, labels `pnl_realized > 0`, sample_weight `|pnl|`. This is the live-decision model the trade_scorer consumes via `predict_copy()` (with `predict()` kept as an alias for backward compat so `bot/trade_scorer.py` needs no changes).

- **`ml_block.pkl`** — trained ONLY on `blocked_trades` with `would_have_won` labels. NOT called from trade_scorer. Exposed via `predict_block()`. Consumed only by the filter audit.

Shared helpers `_snapshot(trader_running, name)` and `_accumulate(trader_running, name, pnl, size)` extracted from the old inline closures to module level so both build functions can share them. `_build_copy_training_data()` and `_build_block_training_data()` each return a focused dataset; legacy `_build_training_data()` kept as a thin wrapper with the old 6-tuple shape for backward compat with tests. `train_model()` becomes a wrapper that calls both `train_copy_model()` and `train_block_model()` so the 6h scheduler keeps working unchanged.

`MODEL_PATH` split into `COPY_MODEL_PATH` and `BLOCK_MODEL_PATH`. Legacy `MODEL_PATH` points at the copy model for back-compat. `_load_copy_model()` has a fallback chain to the old `ml_model.pkl` location so an in-place upgrade doesn't break the live bot before the next retrain.

### Filter Precision Audit

New module `bot/filter_audit.py::compute_filter_precision()`:

1. Loads ml_block model
2. Pulls all `blocked_trades` with `would_have_won IS NOT NULL`
3. Slices off the 80/20 chronological TEST segment (same split as training) so the audit measures on rows the model has never seen — running predict_proba on the train segment trivially gives ~100% precision via memorization
4. Groups test rows by `block_reason` and computes: `n`, `actual_win_rate`, `confident_n` (ml_block proba >= 0.7), `confident_wins`, `precision_at_conf`
5. Recommendation per bucket:
   - `precision >= 70%` → **LOOSEN** (filter blocks real winners)
   - `precision <= 30%` → **KEEP** (filter correctly blocks losers)
   - in between → **REVIEW**
   - `n < 100` → **INSUFFICIENT**

Live result on `walter`:

| Reason | Test N | Actual WR | Confident@0.7 | Precision | Recommendation |
|---|---|---|---|---|---|
| category_blacklist | 2222 | 44.6% | 768 | 100.0% | **LOOSEN** |
| event_timing | 534 | 0.0% | 0 | — | NO_CONFIDENT_PREDICTIONS |
| min_trader_usd | 8 | 12.5% | 1 | 100.0% | INSUFFICIENT |

`category_blacklist` flagged as LOOSEN confirms the Cubs hypothesis: the blacklist was calibrated on corrupt DB PnL and is now blocking 768 test rows that the post-backfill model identifies as confident winners. `event_timing` correctly blocks all 534 test losses (0% actual WR). Other reasons like `exposure_limit`, `no_rebuy`, `conviction_ratio`, `score_block` have zero outcome-tracked rows (the outcome_tracker skips them) so they don't appear in the audit until the tracker is extended.

### Dashboard panel

New `/api/brain/filter-precision` endpoint in `dashboard/app.py`. New "Filter Precision Audit" table on `/brain` under the ML Model Health card, color-coded rows (red = LOOSEN, green = KEEP, yellow = REVIEW, grey = INSUFFICIENT) with a legend. Polls via the existing `refresh()` pipeline so it updates alongside the other panels.

### Schema migration

`ALTER TABLE ml_training_log ADD COLUMN model_name TEXT DEFAULT 'ml_copy'` + index `idx_ml_training_log_model_name`. `get_model_health(model_name='ml_copy')` now filters by model so the trade_scorer edge gate keeps reading only the copy model's metrics. Historical rows default to 'ml_copy' via the SQLite ALTER-with-default behavior.

### ML quality after the split

- **ml_copy**: 639 samples, train 79.8% / test 31.2% / baseline 76.6% / edge −45.3pp. Feature importance flipped to `trader_pnl_7d=17% / trader_trades_7d=14% / hour=13% / entry_price=12%` — the model is finally using trader signal instead of leaning on entry_price dominance. Test accuracy dropped because the old 97% was inflated by the blocked subset; the new number is the first honest measurement. Trade-scorer edge gate keeps the ML adjustment display-only as before.
- **ml_block**: 13820 samples, train 96.4% / test 99.2% / baseline 64.1% / edge +35pp. The block model finds strong signal (traders and categories split cleanly), which is exactly what makes it useful for the audit. The audit uses the held-out test slice so the precision numbers aren't self-referential.

### Files touched

- `bot/ml_scorer.py` — ~250 LOC: split build/train/predict functions, shared helpers, model load/save with both paths
- `bot/filter_audit.py` — new file, ~130 LOC
- `database/models.py` — `model_name` column in SCHEMA_UPGRADE
- `database/db.py` — ALTER TABLE migration + index; `get_model_health` filters by model_name
- `dashboard/app.py` — new `api_filter_precision` endpoint
- `dashboard/templates/brain.html` — panel HTML + CSS + renderer JS

No changes to `bot/trade_scorer.py` — the legacy `predict` / `_load_model` aliases keep it working unchanged.

## 2026-04-14 — Backfill usdc_received from activity API, reveal real winners

Root discovery: 87% of recent closed `copy_trades` rows had `usdc_received = NULL`, so every downstream consumer (Brain, ML Scorer, Trade Scorer, auto-tuner) was training on formula-computed P&L, not capital-verified wallet deltas. After backfilling 171 rows from Polymarket's `data-api /activity` endpoint, the real trader picture is:

| Trader | DB before | Verified after |
|---|---|---|
| xsaghav | −$6 | **+$91** (73 verified) |
| sovereign2013 | −$53 | **+$67** (96 verified) |
| KING7777777 | −$37 | **+$20** (53 verified) |
| fsavhlc | −$1 | +$2.51 |

The bot has been treating the two biggest winners (xsaghav, sovereign2013) as losers for weeks. The Brain's "throttle/kick" logic (disabled per piff-philosophy but still visible in UI) and the ML scorer's trader features were all computing against corrupt labels.

### `backfill_usdc_received.py` (new tool)

Walks `copy_trades WHERE usdc_received IS NULL`, pulls all TRADE activity events for our wallet (paginated via offset, 1922 events fetched), builds a `(condition_id, side) → [sell_events]` index, and matches each NULL row to its closing SELL via 1:1 greedy pairing (oldest row → oldest sell within the bucket). The 1:1 constraint is load-bearing: naive closest-by-timestamp matching double-assigns the same fill across multiple NULL rows, inflating the apparent recovery by ~2×. Bucket-overflow rows (more NULL rows than sells, 15 cases) fall through to second-pass redemption lookup (currently 400s on the API, handled gracefully).

Dry-run / --apply modes, --limit for sanity checks, per-trader summary before writing. Run `backfill_usdc_received.py --apply` on the server once 171 corrections landed, then a one-shot `UPDATE copy_trades SET actual_size = size WHERE actual_size IS NULL AND usdc_received IS NOT NULL` populated the 116 actual_size NULLs so the strict trader_performance query could see them.

### `PERFORMANCE_SINCE` rewind

Rewound from `2026-04-14T00:15:59` (the 2-hour-ago dashboard-reset marker) to `2026-04-05T00:00:00` (before the oldest backfilled close). Without this the `trader_performance` 7d view was silently empty — the filter was truncating 9 days of verified history down to a 2-hour window. Trade-off: the dashboard's "clean-slate post-reset" framing is gone, but Brain/ML can finally see who the real winners are.

### `bot/ml_scorer.py` — blocked downweight + class_weight removal

Two training fixes triggered by the backfill exposing clean copy labels:

1. **Blocked rows now weight 0.1** instead of 1.0. Blocked trades outnumber copy trades ~20:1 but came from a rejected-distribution (extreme prices, filtered traders) that doesn't transfer to live copy decisions. Downweighting lets them regularize without drowning out the magnitude-weighted copy rows. Feature importance flipped from `entry_price=70%` dominance to a healthier `entry_price=36% / trader_trades_7d=13% / trader_pnl_7d=10% / hour=10%` — the model is finally using trader-specific signal.

2. **`class_weight='balanced'` removed**. Combined with sample_weight it was double-counting: the per-row |pnl| weight already encodes which outcomes matter, and sklearn's class balancing on top pushed the model to over-predict wins (copy_only accuracy dropped from 48.1% → 38.3% when we added it post-backfill). Removing restored it to 44.9% with better feature diversity.

3. **`_load_trader_stats` bypass of trader_performance cache**: predict-time trader features now compute straight from `copy_trades` with verified-only filter (`usdc_received + actual_size IS NOT NULL`), ignoring the `PERFORMANCE_SINCE` dashboard marker. Decouples ML from dashboard-reset semantics — the model sees the same verified history regardless of which cutoff the user picks for display purposes.

### Current ML health

`copy_only=44.9% baseline=79.4% edge=−34.5pp` (copy baseline measured on test subset, not the overall). Model is still below baseline and the trade-scorer edge gate keeps its adjustment display-only. Feature importance is finally diverse, which means the next retrain cycle has a chance to find real signal instead of just leaning on entry_price.

### Not fixed this session

- **Close-logic audit Step 1-6** (structural fix for the NULL pipeline going forward) — deferred. Backfill handles the symptom for today; the root cause (close_copy_trade signature doesn't take usdc_received) means future closes will keep hitting NULL unless the signature is refactored and the 5 MEDIUM paths rewritten. Separate session.
- **382 unmatched NULL rows** — mostly old trades that fell outside the 1922-event API window. Would need on-chain Polygon RPC queries to reconstruct.
- **Profitability itself**. This session gave Brain clean data to make decisions on; it did NOT change the trading strategy. Whether the bot becomes profitable now depends on whether the user (or a future auto-tuner) acts on the revelation that sovereign2013 and KING7777777 are the actual winners, and xsaghav / fsavhlc need tightening.

## 2026-04-14 — ML sample weighting + class balance + self-disabling edge gate

Three linked fixes that make the ML scorer stop actively damaging the trade path when it's worse than baseline, and give it the means to eventually earn its keep.

### Fix 1 — Sample-weight by |pnl_realized|

`bot/ml_scorer.py::_build_training_data()` now returns a sixth element `weights` alongside `(X, y, is_copy, copy_count, blocked_count)`. Copy-trade rows get `weight = clamp(abs(pnl_realized), 0.1, 5.0)` so a $5 loss counts 50× a $0.10 win. Lower clamp keeps $0-PnL rows from vanishing; upper cap stops a single freak $10 loss from dominating tree splits. Blocked rows get a neutral `1.0` (no $ amount available). The model now optimizes for avoiding dollar losses instead of maximizing win frequency — the right objective for asymmetric Polymarket payoffs (small wins, big losses on resolve-to-0).

### Fix 2 — `class_weight='balanced'`

`RandomForestClassifier` in `train_model()` now uses `class_weight='balanced'`, so sklearn auto-compensates for the ~65/35 loss/win skew by weighting the minority (win) class ~1.86×. Combined with Fix 1, wins receive both magnitude weighting and class balancing. The visible effect: test `accuracy` can no longer be gamed by always predicting the majority class — `copy_only_accuracy` vs `baseline_accuracy` is now the only comparison that matters.

### Fix 3 — `get_model_health()` + trade-scorer edge gate

New helper `bot/ml_scorer.py::get_model_health()` reads the latest `ml_training_log` row and returns `{edge_vs_baseline, copy_only, baseline, trained_at}` where `edge_vs_baseline = (copy_only_accuracy - baseline_accuracy) * 100` in signed percentage points.

`bot/trade_scorer.py:208-235` now calls it before touching the score: when `edge_pp < 0` (model is worse than baseline) the `components["ml_prediction"]` field is still populated for the UI, but `total` is NOT modified. When `edge_pp >= 0` the adjustment runs with tighter thresholds (`<0.15 → -15`, `>0.85 → +15`) instead of the old noisy `0.30/0.70` boundary — only extreme-confidence predictions move the needle.

**State after retrain on walter**: copy_only=48.1%, baseline=64.7%, edge=-16.6pp → ML adjustment auto-disabled in live trade decisions. Once copy_only climbs above baseline (from more post-reset data or better features), it reactivates itself without a code change. Self-disabling safety.

### Files touched

- `bot/ml_scorer.py` — `_build_training_data` returns weights, `train_model` uses `class_weight='balanced'` + `sample_weight`, new `get_model_health()` helper
- `bot/trade_scorer.py` — edge-gate before ML adjustment, thresholds tightened 0.30/0.70 → 0.15/0.85

No schema changes, no frontend touch. Pickle compat preserved (same 11-feature layout).

## 2026-04-14 — Paper summary with 1-7d windows + paper events in the stream

### Paper summary table expanded per piff's spec

`/api/brain/paper-traders` now returns rolling windows `pnl_1d`, `pnl_2d`, `pnl_3d`, `pnl_5d`, `pnl_7d`, plus separate `open_trades` / `closed_trades` counts, `realized_pnl` (sum of closed), and `unrealized_pnl` (sum of `current_price - entry_price` across open positions). The frontend `paperSummaryTable` is now a 14-column table: Trader · State · Days · Open · Closed · WR · 1d · 2d · 3d · 5d · 7d · Unreal · Realized · Verdict. Verdict logic unchanged (PROMOTE at 3d positive with ≥10 closed, KICK at 7d flat/negative, HOLD / OBSERV otherwise).

### Brain Stream now includes paper events + lifecycle transitions

New endpoint `dashboard/app.py::api_paper_events` (`/api/brain/paper-events?limit=300`) returns a unified timeline of:

- `paper_buy` — each open `paper_trades` row (side, entry price, market question)
- `paper_win` / `paper_loss` — each closed `paper_trades` row (+/-$pnl, market)
- `observe` — `trader_candidates.discovered_at`
- `promote` — `trader_candidates.promoted_at`
- `kick` — `trader_candidates.demoted_at`

All sorted newest-first, capped at 300. Graceful fallback when the tables don't exist locally.

Frontend `refresh()` fetches `/api/brain/paper-events` and merges through new `normPaperEvent()` into `streamEvents`. A new **PAPER** filter button and matching matchFilter branch isolate the paper lifecycle events. CSS adds border-left + action-color rules for `paper_buy` (cyan), `paper_win` (green), `paper_loss` (red), `observe` (yellow), `promote` (green), `kick` (red) so they're visually distinct from the existing brain decisions / trade events.

Live server verification: the endpoint returns real rows like `PAPER BUY ScottyNooo NO @ 65c "Will the U.S. invade Iran before 2027?"`, `PAPER BUY 0x2a2C53... Atlanta Braves @ 56c "Miami Marlins vs. Atlanta Braves"`, etc. — these now flow into the brain stream alongside the existing decision / block / trade events.

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
