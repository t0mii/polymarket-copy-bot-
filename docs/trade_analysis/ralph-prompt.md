# Ralph Loop: Polybot Trade Analysis — Complete & Detailed

This is the prompt that the `/loop` skill runs on a timer. Each iteration is stateless — all state lives in `docs/trade_analysis/state.json` and `docs/trade_analysis/findings.md`.

**Invocation (pick one):**

```
/loop 30m Analyze polybot trades — follow docs/trade_analysis/ralph-prompt.md
/loop 1h  Analyze polybot trades — follow docs/trade_analysis/ralph-prompt.md
/loop Analyze polybot trades — follow docs/trade_analysis/ralph-prompt.md
```

Third form = dynamic pacing.

---

## Task Overview

Working directory: `/home/wisdom/Schreibtisch/polymarketscanner`. Server: `walter@10.0.0.20` (passwordless SSH, key-auth). Project path on server: `/home/walter/polymarketscanner`. DB: `database/scanner.db`. Venv: `venv/bin/python`.

**Complete, detailed, observational.** Analyze EVERY event since last iteration — every closed trade, every blocked trade, every brain decision, every score, every lifecycle transition, every settings change. Nothing aggregated away.

### Step 1 — Read prior state

`Read: docs/trade_analysis/state.json`

Fields (all persisted between iterations):
- `last_iteration_ts` — ISO timestamp
- `last_trade_id_seen` — max copy_trades.id analyzed
- `last_blocked_id_seen` — max blocked_trades.id analyzed
- `last_score_id_seen` — max trade_scores.id analyzed
- `last_brain_decision_id_seen` — max brain_decisions.id analyzed
- `last_lifecycle_log_ts` — last trader_lifecycle change seen
- `last_settings_mtime` — unix mtime of server settings.env
- `prior_portfolio_total` — last seen Wallet+Positions total (float)
- `prior_today_pnl` — last seen today's closed PnL sum
- `trader_snapshots` — `{username: {wr_7d, pnl_7d, trades_7d, tier}}` from last iteration
- `iteration_count`
- `prior_settings_hash` — sha256 of settings.env contents at last check

### Step 2 — Gather COMPLETE current state

Run this exact query script over SSH. Substitute `<VALUES>` from state.json first:

```bash
ssh walter@10.0.0.20 "cd /home/walter/polymarketscanner && venv/bin/python <<'PY'
import sqlite3, json, os, hashlib, subprocess
con = sqlite3.connect('database/scanner.db')
con.row_factory = sqlite3.Row

# === Portfolio (current snapshot from live log) ===
r = subprocess.run(['sudo','journalctl','-u','polybot','--since','3 minutes ago','--no-pager'], capture_output=True, text=True)
for line in r.stdout.split('\n')[::-1]:
    if 'PORTFOLIO:' in line:
        print('PORTFOLIO_LINE:', line.split('PORTFOLIO:',1)[1].strip())
        break

# === Today PnL + counts ===
r = con.execute('''SELECT COUNT(*) n, ROUND(SUM(COALESCE(pnl_realized,0)),2) pnl,
                          SUM(CASE WHEN pnl_realized>0 THEN 1 ELSE 0 END) wins
                   FROM copy_trades WHERE status=\"closed\" AND pnl_realized IS NOT NULL
                     AND closed_at >= date(\"now\",\"localtime\")''').fetchone()
print('TODAY:', json.dumps(dict(r)))

# === EVERY new closed trade since last iteration (FULL DETAIL) ===
LAST_TRADE_ID = <LAST_TRADE_ID>
print('--- NEW_CLOSED_TRADES ---')
for row in con.execute('''SELECT id, wallet_username, wallet_address, side, category, 
                                 actual_entry_price, actual_size, shares_held, 
                                 usdc_received, pnl_realized, current_price,
                                 condition_id, event_slug, market_question,
                                 created_at, closed_at
                          FROM copy_trades WHERE id > ? 
                            AND status='closed' AND pnl_realized IS NOT NULL
                          ORDER BY id''', (LAST_TRADE_ID,)):
    d = dict(row)
    # Classify close type
    size = d.get('actual_size') or 0
    recv = d.get('usdc_received')
    if recv is None:
        d['close_type'] = 'NO_USDC'
    elif recv == 0:
        d['close_type'] = 'ZERO'
    elif size > 0 and recv < size * 0.1:
        d['close_type'] = 'NEAR_ZERO'
    else:
        d['close_type'] = 'REAL_SELL'
    print(json.dumps(d, default=str))

# === EVERY new blocked trade since last iteration (FULL DETAIL) ===
LAST_BLOCKED_ID = <LAST_BLOCKED_ID>
print('--- NEW_BLOCKED_TRADES ---')
for row in con.execute('''SELECT id, trader, market_question, condition_id, side,
                                 trader_price, block_reason, block_detail, buy_path,
                                 outcome_price, would_have_won, category, created_at, checked_at
                          FROM blocked_trades WHERE id > ?
                          ORDER BY id''', (LAST_BLOCKED_ID,)):
    print(json.dumps(dict(row), default=str))

# === EVERY new trade score since last iteration ===
LAST_SCORE_ID = <LAST_SCORE_ID>
print('--- NEW_SCORES ---')
for row in con.execute('''SELECT id, condition_id, trader_name, side, entry_price,
                                 market_question, score_total, score_trader_edge,
                                 score_category_wr, score_price_signal, score_conviction,
                                 score_market_quality, score_correlation, action,
                                 trade_id, outcome_pnl, created_at
                          FROM trade_scores WHERE id > ?
                          ORDER BY id''', (LAST_SCORE_ID,)):
    print(json.dumps(dict(row), default=str))

# === EVERY new brain decision since last iteration ===
LAST_BD_ID = <LAST_BD_ID>
print('--- NEW_BRAIN_DECISIONS ---')
for row in con.execute('''SELECT id, action, target, reason, data, expected_impact, created_at
                          FROM brain_decisions WHERE id > ?
                          ORDER BY id''', (LAST_BD_ID,)):
    print(json.dumps(dict(row), default=str))

# === CURRENT trader_lifecycle state (full) ===
print('--- LIFECYCLE_CURRENT ---')
for row in con.execute('''SELECT username, address, status, source, pause_count, pause_until,
                                 paper_trades, paper_pnl, paper_wr, live_pnl, kick_reason,
                                 status_changed_at
                          FROM trader_lifecycle ORDER BY status, username'''):
    print(json.dumps(dict(row), default=str))

# === CURRENT trader_status (soft throttle layer) ===
print('--- TRADER_STATUS_CURRENT ---')
for row in con.execute('SELECT trader_name, status, bet_multiplier, reason, updated_at FROM trader_status'):
    print(json.dumps(dict(row), default=str))

# === 7d rolling per-trader stats (all distinct wallets in copy_trades 7d) ===
print('--- TRADER_ROLLING_7D ---')
traders = con.execute('''SELECT DISTINCT wallet_username FROM copy_trades 
                         WHERE wallet_username IS NOT NULL AND wallet_username != ''
                           AND created_at >= datetime('now','-7 days')''').fetchall()
for t in traders:
    name = t['wallet_username']
    r = con.execute('''SELECT COUNT(*) n,
                              SUM(CASE WHEN pnl_realized>0 THEN 1 ELSE 0 END) wins,
                              SUM(CASE WHEN pnl_realized<0 THEN 1 ELSE 0 END) losses,
                              ROUND(SUM(COALESCE(pnl_realized,0)),2) pnl,
                              ROUND(AVG(COALESCE(actual_size,0)),2) avg_size,
                              ROUND(AVG(COALESCE(actual_entry_price,0)),4) avg_entry
                       FROM copy_trades 
                       WHERE wallet_username=? AND status='closed' AND pnl_realized IS NOT NULL
                         AND closed_at >= datetime('now','-7 days')''', (name,)).fetchone()
    cat_rows = con.execute('''SELECT category, COUNT(*) n, 
                                     ROUND(SUM(COALESCE(pnl_realized,0)),2) pnl
                              FROM copy_trades WHERE wallet_username=? AND status='closed'
                                AND closed_at >= datetime('now','-7 days')
                                AND category IS NOT NULL AND category != ''
                              GROUP BY category ORDER BY pnl DESC''', (name,)).fetchall()
    cats = [dict(c) for c in cat_rows]
    print(json.dumps({'trader': name, **dict(r), 'cats': cats}, default=str))

# === Scorer action distribution since last iteration ===
print('--- SCORER_ACTIONS ---')
for row in con.execute('''SELECT action, COUNT(*) n, ROUND(AVG(score_total),1) avg_score,
                                 MIN(score_total) mn, MAX(score_total) mx
                          FROM trade_scores WHERE id > ?
                          GROUP BY action''', (LAST_SCORE_ID,)):
    print(json.dumps(dict(row), default=str))

# === Feedback loop coverage ===
r = con.execute('''SELECT COUNT(*) total, 
                          SUM(CASE WHEN outcome_pnl IS NOT NULL THEN 1 ELSE 0 END) with_outcome,
                          SUM(CASE WHEN outcome_pnl IS NOT NULL AND outcome_pnl > 0 THEN 1 ELSE 0 END) winners,
                          SUM(CASE WHEN outcome_pnl IS NOT NULL AND outcome_pnl < 0 THEN 1 ELSE 0 END) losers
                   FROM trade_scores''').fetchone()
print('SCORER_FEEDBACK:', json.dumps(dict(r)))

# === Score range performance (brain's tuning signal) ===
print('--- SCORE_RANGE_PERF ---')
for row in con.execute('''SELECT 
    CASE WHEN score_total < 40 THEN '00-39'
         WHEN score_total < 60 THEN '40-59'
         WHEN score_total < 80 THEN '60-79'
         ELSE '80-100' END bucket,
    COUNT(*) n,
    SUM(CASE WHEN outcome_pnl>0 THEN 1 ELSE 0 END) wins,
    SUM(CASE WHEN outcome_pnl<0 THEN 1 ELSE 0 END) losses,
    ROUND(AVG(COALESCE(outcome_pnl,0)),2) avg_pnl,
    ROUND(SUM(COALESCE(outcome_pnl,0)),2) tot_pnl
    FROM trade_scores WHERE outcome_pnl IS NOT NULL
    GROUP BY bucket ORDER BY bucket'''):
    print(json.dumps(dict(row), default=str))

# === Signal performance (CLV + any other signals) ===
print('--- SIGNAL_PERF ---')
for row in con.execute('SELECT signal_type, trades_count, wins, losses, total_pnl, is_active, updated_at FROM signal_performance'):
    print(json.dumps(dict(row), default=str))

# === auto_discovery new candidates since last iteration ===
print('--- DISCOVERY_CANDIDATES ---')
for row in con.execute('''SELECT address, pnl, win_rate, trades, source, 
                                 notes, discovered_at
                          FROM trader_candidates 
                          WHERE discovered_at >= datetime('now','-1 day')
                          ORDER BY pnl DESC LIMIT 20'''):
    print(json.dumps(dict(row), default=str))

# === Category performance snapshot ===
print('--- CATEGORY_PERF ---')
for row in con.execute('''SELECT category, period, trades_count, wins, losses, 
                                 ROUND(total_pnl,2) pnl, winrate
                          FROM category_performance WHERE period='7d' ORDER BY pnl DESC'''):
    print(json.dumps(dict(row), default=str))

# === Settings.env hash + mtime ===
try:
    with open('settings.env','rb') as f:
        content = f.read()
    print('SETTINGS_HASH:', hashlib.sha256(content).hexdigest())
    print('SETTINGS_MTIME:', os.path.getmtime('settings.env'))
    # Key scalar values
    for line in content.decode().split('\n'):
        line = line.strip()
        if line.startswith('FOLLOWED_TRADERS=') or line.startswith('MAX_DAILY_LOSS=') or \
           line.startswith('MAX_DAILY_TRADES=') or line.startswith('STOP_LOSS_PCT=') or \
           line.startswith('MIN_ENTRY_PRICE_MAP=') or line.startswith('MAX_ENTRY_PRICE_MAP=') or \
           line.startswith('MIN_TRADER_USD_MAP=') or line.startswith('CATEGORY_BLACKLIST_MAP=') or \
           line.startswith('MIN_CONVICTION_RATIO_MAP='):
            print('SETTING:', line[:200])
except Exception as e:
    print('SETTINGS_ERR:', e)

# === Latest ML training log entry ===
print('--- ML_LATEST ---')
for row in con.execute('SELECT * FROM ml_training_log ORDER BY id DESC LIMIT 3'):
    print(json.dumps(dict(row), default=str))

# === Recent copy_scan log ERRORS (bot health) ===
print('--- RECENT_ERRORS ---')
r = subprocess.run(['sudo','journalctl','-u','polybot','--since','10 minutes ago','--no-pager'], capture_output=True, text=True)
err_count = 0
for line in r.stdout.split('\n'):
    if 'ERROR' in line or 'Traceback' in line or 'Exception' in line:
        err_count += 1
        if err_count <= 10:
            print(line[-200:])
print(f'ERROR_TOTAL_10MIN: {err_count}')
PY
"
```

### Step 3 — Analyze (EVERY event, not just aggregates)

**For each NEW_CLOSED_TRADE:** log one line in findings with id / trader / category / entry→exit / size / pnl / close_type. Example: `#2799 Jargs cs 0.51→$2.42 size=$2.68 pnl=-$0.26 REAL_SELL`.

**For each NEW_BLOCKED_TRADE:** log one line with id / reason / trader / trader_price / market_question[:50]. Example: `#B4521 conviction_ratio sovereign2013 49c "Spread: Spurs (-11.5)"`. Include the full block_detail if present.

**For each NEW_SCORE:** note if action was EXECUTE/BOOST/BLOCK/QUEUE, the total score, and the weakest/strongest component. Link to copy_trades if a buy eventually happened.

**For each NEW_BRAIN_DECISION:** verbatim (action, target, reason). Flag any that contradict prior decisions (e.g. BLACKLIST followed by REVERT_BLACKLIST in the same cycle).

**Lifecycle diff:** any trader that moved between states (LIVE_FOLLOW → PAUSED, PAPER_FOLLOW → LIVE_FOLLOW, etc.) since last iteration.

**Trader snapshots diff:** for each trader in state.trader_snapshots, compute:
- `wr_delta = current_wr - prior_wr` (flag if |delta| >= 5pp)
- `pnl_delta = current_pnl - prior_pnl` (flag if |delta| >= $5)
- `trades_delta = current_trades - prior_trades` (flag if 0 for 2+ iterations in a row — trader idle)
- `tier_changed` (flag if different tier than last time)

**Score-range performance:** do the buckets show signal? If `80-100` bucket has WR >= 60% and `40-59` has WR <= 40%, the scorer is discriminating. If all buckets cluster around 50%, the scorer isn't learning anything useful yet.

**Settings drift:** if `SETTINGS_HASH` changed, diff the key scalar lines against state.prior_settings_hash context. Note WHO probably wrote them (auto_tuner/brain/manual) based on timing.

**Portfolio reconciliation:**
- `wallet_delta = current_portfolio - prior_portfolio_total`  
- `db_pnl_delta = TODAY.pnl - prior_today_pnl`  
- If `|wallet_delta - db_pnl_delta| > $5`, FLAG as phantom drift (DB PnL and wallet diverging — resolved-to-zero inflation or ghost trades)

**Filter pressure:**
- `blocks_per_close_ratio = len(NEW_BLOCKED) / max(len(NEW_CLOSED), 1)`
- If ratio > 20 AND len(NEW_CLOSED) == 0, filters are too tight → flag
- Breakdown block reasons: if one reason dominates (>80%), the bot has an unbalanced filter profile

**Flags to explicitly raise:**

| Flag | Condition |
|---|---|
| `WR_DROP` | Any trader 7d WR dropped ≥5pp since last iteration |
| `PNL_CROSSOVER` | Any trader 7d PnL crossed zero (plus→minus or vice versa) |
| `TIER_CHANGED` | auto_tuner reclassified a trader to different tier |
| `IDLE_TRADER` | A followed trader made 0 new buys for ≥3 consecutive iterations |
| `FILTER_TOO_TIGHT` | Blocks >20x copies AND zero copies this iteration |
| `PHANTOM_DRIFT` | wallet_delta and db_pnl_delta disagree by >$5 |
| `BRAIN_SPAM` | >5 brain_decisions with same (action,target) in last hour |
| `BRAIN_OSCILLATION` | BLACKLIST and REVERT_BLACKLIST for same pair in same hour |
| `FEEDBACK_DYING` | trade_scores.with_outcome / total < 20% AND total > 100 |
| `SCORER_NON_DISCRIMINATING` | Score buckets all cluster within 10pp WR of each other |
| `BOT_CRASHING` | ERROR_TOTAL_10MIN >= 3 |
| `MAX_DAILY_LOSS_TRIGGER` | `[SKIP] Max daily loss reached` appears in logs |
| `STOP_LOSS_CASCADE` | ≥3 stop-loss closes in one iteration |
| `SETTINGS_CHANGED` | SETTINGS_HASH differs from prior |
| `ML_NOT_RETRAINING` | last ml_training_log row >12h old |

### Step 4 — Write detailed findings

Prepend new section to `docs/trade_analysis/findings.md`:

```markdown
## Iteration N — YYYY-MM-DD HH:MM (Δt=XXm)

### Snapshot
- Portfolio: $X.XX (Δ $±Y.YY since iter N-1)
- Today PnL: $Z.ZZ (Δ $±A.AA)
- Today closes: n total, w wins, l losses
- Followed traders live: n
- Bot errors last 10min: N

### Every new closed trade (Details)
- #2799 Jargs cs entry=0.51 exit=$2.42 size=$2.68 pnl=-$0.26 REAL_SELL
  "VfB Stuttgart vs. Hamburger SV: O/U 3.5" — closed 16:11:56
- #2793 KING7777777 valorant entry=0.43 exit=$1.20 size=$3.07 pnl=-$1.87 REAL_SELL
  ...

### Every new blocked trade (Details)
- #B4521 conviction_ratio sovereign2013 49c trader_size=$0.96
  "Spread: Spurs (-11.5)" (detail: Conviction 0.0x < 0.5x min)
- ... (list every single one, no aggregation)

### Scorer activity
- SCORES n new: x EXECUTE (avg A), y BOOST (avg B), z BLOCK (avg C)
- Score-range performance:
  - 00-39: n, WR%, $pnl
  - 40-59: n, WR%, $pnl
  - 60-79: n, WR%, $pnl
  - 80-100: n, WR%, $pnl
- Feedback coverage: X / Y total scores have outcome_pnl (Z%)

### Brain decisions
- 21:34:12 PAUSE_TRADER xsaghav "7d PnL $-129 < -$10"
- 21:34:13 TIGHTEN_FILTER KING7777777 "..."
- 21:34:13 RELAX_FILTER KING7777777 "..."
(verbatim action+target+reason for each)

### Trader deltas (7d rolling)
| Trader | Trades Δ | WR Δ | PnL Δ | Tier |
|---|---|---|---|---|
| Jargs | +2 | -1.5pp | -$0.45 | WEAK |
| KING7777777 | 0 | 0 | 0 | STAR (idle) |
| sovereign2013 | +5 | +2.1pp | +$3.20 | WEAK |

### Lifecycle transitions
- xsaghav LIVE_FOLLOW → PAUSED (72h) at 21:34:13
- (none if quiet)

### Flags
- [x] IDLE_TRADER: KING7777777 (3 iterations, 0 buys)
- [x] FILTER_TOO_TIGHT: 17 blocks / 0 copies
- [ ] no PHANTOM_DRIFT
- [ ] no BRAIN_SPAM
- (list every flag explicitly, checked or not)

### One-line summary
Iter N: portfolio $X (Δ $±Y). n closes (w/l). Top concern: [top flag or observation].

---
```

Keep last 10 iterations above `## Archive` marker. Move older ones below. If total file exceeds 1000 lines, truncate archive to last 50.

### Step 5 — Update state.json

```json
{
  "last_iteration_ts": "<ISO>",
  "last_trade_id_seen": <max from NEW_CLOSED + all copy_trades>,
  "last_blocked_id_seen": <max from NEW_BLOCKED>,
  "last_score_id_seen": <max from NEW_SCORES>,
  "last_brain_decision_id_seen": <max from NEW_BRAIN>,
  "last_lifecycle_log_ts": "<current time>",
  "last_settings_mtime": <from SETTINGS_MTIME>,
  "prior_portfolio_total": <current total>,
  "prior_today_pnl": <current TODAY.pnl>,
  "trader_snapshots": { ... },
  "iteration_count": <prev + 1>,
  "prior_settings_hash": "<from SETTINGS_HASH>",
  "notes": "<brief context>"
}
```

Must be ID-based max, not ts-based, so we catch everything. Use `SELECT MAX(id) FROM <table>` after the gather step to avoid missing fresh inserts.

### Step 6 — Output to chat

ONE paragraph, ≤150 words, no markdown:

> Iter N (Δ30m): Portfolio $X (Δ$±Y), today $Z PnL, n new closes (w/l), m new blocks. Flags: [list 2 worst]. Top concern: [one]. See findings.md for full detail.

If quiet: `Iter N: quiet — 0 new closes, m blocks (reason: X), 0 brain actions. Portfolio unchanged.`

---

## Hard rules (don't violate)

- **Read-only.** Never restart bot, never change settings, never commit anything.
- **No API calls to Polymarket.** All data from local SQLite.
- **No questions to user mid-iteration.** Autonomous.
- **No prescriptive recommendations.** Observational only.
- **EVERY event logged in findings.md.** Not aggregates — individual entries. Including all blocked trades.
- **Chat output capped at 150 words.** Details go to findings.md, chat gets summary.
- **Keep state.json compact.** Just IDs, hashes, snapshots — not full trade history.
- **No Polymarket-API calls.** No external requests at all.
- **If ERROR_TOTAL_10MIN >= 3**, output the error in chat even if it exceeds word cap (safety).
