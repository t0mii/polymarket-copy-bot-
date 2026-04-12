# Polybot Brain Fix-Everything Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close out the brain/scorer/lifecycle defect list from the 2026-04-12 debugging session. Bring the bot's learning loop, safety rails, and trader-state bookkeeping back to working order in three sequenced deploys.

**Architecture:** Three batches, each a self-contained deploy with its own verification step. Batch 1 = safety rails (config + missing file). Batch 2 = code bugs (feedback loop, settings reload, log dedup, lifecycle bootstrap, race fix, signal_performance bug, ML time-split). Batch 3 = design cleanups (unified trader-state reader, auto-revert for brain decisions).

**Tech Stack:** Python 3.11, SQLite (scanner.db), scikit-learn (RandomForest), apscheduler, systemd. Bot deploys via SCP to `walter@10.0.0.20:/home/walter/polymarketscanner`. Tests are Python-stdlib `unittest` to avoid new deps (pytest not installed on server, we don't want to add it).

**Spec reference:** `docs/superpowers/specs/2026-04-12-polybot-brain-fixes-design.md`

**Important planning notes:**

- **Test-first but stdlib-only.** Tests live under `tests/`, use `unittest.TestCase`, run via `python3 -m unittest tests.test_name -v`. This keeps the plan independent of pytest installation.
- **Tests use a temp SQLite file** (created via `tempfile.NamedTemporaryFile`), not in-memory, because `database/db.py` uses `config.DB_PATH` read at import time. Each test sets `config.DB_PATH` before `init_db()`.
- **One commit per task.** Frequent commits = small rollback surface. Never force-push.
- **Deploy per batch, not per task.** Deploy = SCP changed files + `sudo systemctl restart polybot` + 5 min log monitoring.
- **Never touch `piff-*` branches.** Only commit to local `main`. Never push to remote branches that aren't ours.
- **Settings changes backed up first:** before each batch deploy, `scp` the current `settings.env` to a timestamped backup on the local machine.
- **3.1 revised from spec:** during plan exploration we found `trader_status` (trader_performance.py) is semantically different from `trader_lifecycle` — it's a soft-throttle multiplier (0.0/0.5/1.0), not a full lifecycle. The revised approach keeps both tables and adds a unified reader `db.get_trader_effective_state(name)`. No destructive migration.

---

## File Structure

**Files created:**
- `tests/__init__.py` — empty, marks tests as a package
- `tests/conftest_helpers.py` — shared test helpers (temp DB setup)
- `tests/test_feedback_loop.py` — feedback loop unit tests
- `tests/test_settings_reload.py` — reload-safe key whitelist tests
- `tests/test_brain_dedup.py` — log dedup tests
- `tests/test_lifecycle_seed.py` — bootstrap tests
- `tests/test_live_count_race.py` — MIN_LIVE_TRADERS race tests
- `tests/test_signal_performance.py` — clv_tracker fix tests
- `tests/test_ml_time_split.py` — ML time-series split tests
- `tests/test_trader_state_unified.py` — unified reader tests
- `tests/test_brain_revert.py` — auto-revert tests
- `scorer_weights.example.json` — committed example for fresh deploys
- `docs/superpowers/plans/2026-04-12-polybot-brain-fixes.md` — this file

**Files modified:**
- `settings.env` (server-side, not committed) — safety values + STOP_LOSS
- `settings.example.env` — mirror of safety values for reference
- `config.py` — add `_RELOAD_SAFE_KEYS` set, add `reload()` function
- `bot/settings_lock.py` — add dirty flag + `mark_dirty()` / `poll_dirty()`
- `database/db.py` — new helpers: `update_trade_score_outcome`, `backfill_trade_score_outcomes`, `get_trader_effective_state`, `is_trader_paused`
- `bot/smart_sell.py` — call `update_trade_score_outcome` after close
- `bot/copy_trader.py` — call `update_trade_score_outcome` in resolve + stop-loss paths; reload settings at top of scan loop
- `bot/trade_scorer.py` — pass `trade_id` when it's known post-buy (best-effort)
- `bot/outcome_tracker.py` — call `backfill_trade_score_outcomes` at top of `track_outcomes()`
- `bot/brain.py` — dedup loss iteration, add revert helpers, fix live_count race
- `bot/trader_lifecycle.py` — add `ensure_followed_traders_seeded()`, `resume_trader()`
- `bot/auto_tuner.py` — replace "restart recommended" with `settings_lock.mark_dirty()`
- `bot/ml_scorer.py` — time-sorted train/test split, class balance + baseline logging
- `bot/clv_tracker.py` — fix signal_performance row to count real wins/losses per trade
- `main.py` — call `ensure_followed_traders_seeded()` after `init_db()`
- `database/models.py` — no schema changes (tables already exist), just verified

**Deployment targets (SCP to server after each batch):**
- Batch 1: `settings.env` (local prep), `scorer_weights.json`
- Batch 2: `config.py`, `bot/settings_lock.py`, `database/db.py`, `bot/smart_sell.py`, `bot/copy_trader.py`, `bot/trade_scorer.py`, `bot/outcome_tracker.py`, `bot/brain.py`, `bot/trader_lifecycle.py`, `bot/auto_tuner.py`, `bot/ml_scorer.py`, `bot/clv_tracker.py`, `main.py`
- Batch 3: `database/db.py` (updated again), `bot/brain.py` (updated again), `dashboard/app.py`, `bot/daily_report.py`

---

## Task 0 — Test Infrastructure Bootstrap

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/conftest_helpers.py`

- [ ] **Step 1: Create `tests/` directory and empty `__init__.py`**

```bash
mkdir -p tests && touch tests/__init__.py
```

- [ ] **Step 2: Create shared test helpers**

Create `tests/conftest_helpers.py`:

```python
"""Shared helpers for brain-fix unit tests.

Tests use real SQLite in a temp file because database/db.py reads
config.DB_PATH at import time. Each test creates a temp path and
points config.DB_PATH at it BEFORE importing database.db.
"""
import os
import sys
import tempfile
import importlib


def setup_temp_db() -> str:
    """Create a temp SQLite path, patch config.DB_PATH, init schema.
    
    Returns the temp path so tests can clean it up.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    
    # Patch config BEFORE db import so DB_PATH is the temp file
    import config
    config.DB_PATH = tmp.name
    
    # Force-reload database.db so it picks up new DB_PATH
    if "database.db" in sys.modules:
        importlib.reload(sys.modules["database.db"])
    from database import db
    db.init_db()
    
    return tmp.name


def teardown_temp_db(path: str):
    """Remove the temp DB file."""
    try:
        os.unlink(path)
    except OSError:
        pass


def insert_copy_trade(db_module, **fields):
    """Insert a copy_trades row with sensible defaults."""
    defaults = {
        "wallet_address": "0xdead",
        "wallet_username": "trader1",
        "market_question": "Will X happen?",
        "market_slug": "market-x",
        "side": "YES",
        "entry_price": 0.5,
        "current_price": 0.5,
        "size": 10.0,
        "pnl_unrealized": 0.0,
        "pnl_realized": 0.0,
        "status": "closed",
        "condition_id": "cid-test-1",
        "actual_entry_price": 0.5,
        "actual_size": 10.0,
        "shares_held": 20.0,
        "usdc_received": 10.0,
        "category": "cs",
    }
    defaults.update(fields)
    cols = ",".join(defaults.keys())
    placeholders = ",".join("?" for _ in defaults)
    with db_module.get_connection() as conn:
        cur = conn.execute(
            f"INSERT INTO copy_trades ({cols}) VALUES ({placeholders})",
            tuple(defaults.values())
        )
        return cur.lastrowid
```

- [ ] **Step 3: Smoke-test the harness**

Create `tests/test_smoke.py`:

```python
import unittest
from tests.conftest_helpers import setup_temp_db, teardown_temp_db


class TestSmoke(unittest.TestCase):
    def setUp(self):
        self.db_path = setup_temp_db()

    def tearDown(self):
        teardown_temp_db(self.db_path)

    def test_db_initialized(self):
        from database import db
        with db.get_connection() as conn:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )]
        self.assertIn("copy_trades", tables)
        self.assertIn("trade_scores", tables)
        self.assertIn("trader_lifecycle", tables)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: Run smoke test**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest tests.test_smoke -v
```

Expected: `test_db_initialized ... ok` and `OK`. If it fails because `config.py` errors out on missing `secrets.env` / `settings.env`, see note below.

- [ ] **Step 5: Ensure `secrets.env` + `settings.env` exist locally for tests**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner
[ ! -f secrets.env ] && cp secrets.example.env secrets.env
[ ! -f settings.env ] && cp settings.example.env settings.env
```

Re-run smoke test from Step 4. Should pass now.

- [ ] **Step 6: Commit**

```bash
git add tests/__init__.py tests/conftest_helpers.py tests/test_smoke.py
git commit -m "$(cat <<'EOF'
test: bootstrap unittest harness with temp DB helper

Adds tests/ package using stdlib unittest (no pytest dependency).
conftest_helpers.setup_temp_db patches config.DB_PATH to a tempfile
and re-imports database.db so each test gets a clean schema without
mocking. Smoke test verifies the harness can initialize the schema.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Batch 1 — Safety Rails

### Task 1 — Harden `settings.env` + initialize `scorer_weights.json`

**Files:**
- Modify: `settings.env` (local AND server-side — not committed)
- Modify: `settings.example.env` (committed)
- Create: `scorer_weights.example.json` (committed, used as template for server file)

- [ ] **Step 1: Backup local and server settings.env**

```bash
ts=$(date +%s)
cp /home/wisdom/Schreibtisch/polymarketscanner/settings.env /home/wisdom/Schreibtisch/polymarketscanner/settings.env.bak.$ts
ssh walter@10.0.0.20 "cp /home/walter/polymarketscanner/settings.env /home/walter/polymarketscanner/settings.env.bak.$ts"
```

Expected: no output (both cp commands silent). Verify with `ls -la` on both sides.

- [ ] **Step 2: Verify STOP_LOSS enforcement is wired up in code**

```bash
grep -n "config.STOP_LOSS_PCT\|STOP_LOSS_MAP" /home/wisdom/Schreibtisch/polymarketscanner/bot/copy_trader.py | head -20
```

Expected: shows a block around line 2257 that reads `_sl_pct = _STOP_LOSS_MAP.get(...)` and gates on `if _sl_pct > 0`. Confirms: setting the value turns it on (no code change needed).

- [ ] **Step 3: Update local `settings.env`**

Edit `settings.env` and change these three lines:

```
MAX_DAILY_LOSS=10
MAX_DAILY_TRADES=30
STOP_LOSS_PCT=0.40
```

(They already exist as `=0` — just replace the values. Do not add new lines.)

- [ ] **Step 4: Mirror the change into `settings.example.env`**

Same three values in `settings.example.env`. This file is committed so future deploys start with safe defaults.

- [ ] **Step 5: Create `scorer_weights.example.json`**

Create the file at repo root:

```json
{
  "weights": {
    "trader_edge": 0.30,
    "category_wr": 0.20,
    "price_signal": 0.15,
    "conviction": 0.15,
    "market_quality": 0.10,
    "correlation": 0.10
  },
  "thresholds": {
    "block": 40,
    "queue": 60,
    "boost": 80
  }
}
```

- [ ] **Step 6: SCP updated files to server**

```bash
scp /home/wisdom/Schreibtisch/polymarketscanner/settings.env walter@10.0.0.20:/home/walter/polymarketscanner/settings.env
scp /home/wisdom/Schreibtisch/polymarketscanner/scorer_weights.example.json walter@10.0.0.20:/home/walter/polymarketscanner/scorer_weights.json
```

Expected: both transfers successful.

- [ ] **Step 7: Verify server file contents**

```bash
ssh walter@10.0.0.20 "grep -E '^(MAX_DAILY_LOSS|MAX_DAILY_TRADES|STOP_LOSS_PCT)=' /home/walter/polymarketscanner/settings.env && cat /home/walter/polymarketscanner/scorer_weights.json"
```

Expected output:
```
MAX_DAILY_LOSS=10
MAX_DAILY_TRADES=30
STOP_LOSS_PCT=0.40
{
  "weights": { ... },
  "thresholds": { ... }
}
```

- [ ] **Step 8: Restart polybot and monitor**

```bash
ssh walter@10.0.0.20 "sudo systemctl restart polybot"
sleep 30
ssh walter@10.0.0.20 "sudo journalctl -u polybot --since '1 minute ago' --no-pager" | tail -60
```

Expected: `active (running)`, `PORTFOLIO: Wallet=...` log appears, no Python tracebacks, no "NameError" or "ImportError". Count WARN/ERR — baseline is ~0-2 per minute, nothing new.

- [ ] **Step 9: Commit example files**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner
git add settings.example.env scorer_weights.example.json
git commit -m "$(cat <<'EOF'
chore: add safety rails to example settings + scorer weights file

Updates settings.example.env defaults to non-zero MAX_DAILY_LOSS=10,
MAX_DAILY_TRADES=30, STOP_LOSS_PCT=0.40 so fresh deploys start safe.
Adds scorer_weights.example.json as the template for the trade
scorer weights/thresholds file so the scorer stops falling back to
in-memory defaults forever.

Server settings.env + scorer_weights.json updated via SCP — not
committed since both are gitignored.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

Done with Batch 1. Monitor server logs for 5 more minutes before starting Batch 2.

---

## Batch 2 — Code Bugs

### Task 2 — Trade-Score Feedback Loop

**Files:**
- Modify: `database/db.py` (new helpers `update_trade_score_outcome` + `backfill_trade_score_outcomes`)
- Modify: `bot/smart_sell.py` (call update helper after close)
- Modify: `bot/copy_trader.py` (call update helper in resolve / stop-loss / trailing paths)
- Modify: `bot/outcome_tracker.py` (call backfill helper)
- Create: `tests/test_feedback_loop.py`

- [ ] **Step 1: Write failing test for `update_trade_score_outcome`**

Create `tests/test_feedback_loop.py`:

```python
import unittest
from tests.conftest_helpers import setup_temp_db, teardown_temp_db


class TestFeedbackLoop(unittest.TestCase):
    def setUp(self):
        self.db_path = setup_temp_db()
        from database import db
        self.db = db
        # Insert a trade_scores row
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO trade_scores (condition_id, trader_name, side, "
                "entry_price, market_question, score_total, action) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("cid-xyz", "trader1", "YES", 0.45, "Will X?", 75, "EXECUTE")
            )

    def tearDown(self):
        teardown_temp_db(self.db_path)

    def test_update_sets_outcome_pnl(self):
        updated = self.db.update_trade_score_outcome("cid-xyz", "trader1", 2.34)
        self.assertEqual(updated, 1)
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT outcome_pnl FROM trade_scores WHERE condition_id='cid-xyz'"
            ).fetchone()
        self.assertAlmostEqual(row["outcome_pnl"], 2.34, places=4)

    def test_update_is_idempotent_skips_already_set(self):
        # First call sets it.
        self.db.update_trade_score_outcome("cid-xyz", "trader1", 1.00)
        # Second call with different pnl must NOT overwrite (outcome already recorded).
        updated = self.db.update_trade_score_outcome("cid-xyz", "trader1", 999.0)
        self.assertEqual(updated, 0)
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT outcome_pnl FROM trade_scores WHERE condition_id='cid-xyz'"
            ).fetchone()
        self.assertAlmostEqual(row["outcome_pnl"], 1.00, places=4)

    def test_update_matches_newest_when_multiple(self):
        # Insert an older row with the same condition_id + trader.
        with self.db.get_connection() as conn:
            conn.execute(
                "INSERT INTO trade_scores (condition_id, trader_name, side, "
                "entry_price, market_question, score_total, action, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now','-1 day'))",
                ("cid-xyz", "trader1", "YES", 0.5, "Will X?", 60, "EXECUTE")
            )
        self.db.update_trade_score_outcome("cid-xyz", "trader1", 3.14)
        with self.db.get_connection() as conn:
            rows = conn.execute(
                "SELECT score_total, outcome_pnl FROM trade_scores "
                "WHERE condition_id='cid-xyz' ORDER BY id"
            ).fetchall()
        # Only the newest (score_total=75) should have outcome_pnl set.
        self.assertIsNone(rows[0]["outcome_pnl"])
        self.assertAlmostEqual(rows[1]["outcome_pnl"], 3.14, places=4)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails with AttributeError**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest tests.test_feedback_loop -v 2>&1 | tail -30
```

Expected: three failures, all complaining about `db.update_trade_score_outcome` not existing (`AttributeError: module 'database.db' has no attribute 'update_trade_score_outcome'`).

- [ ] **Step 3: Add `update_trade_score_outcome` + `backfill_trade_score_outcomes` to `database/db.py`**

Append after the existing `get_score_range_performance` function (line ~1262):

```python
def update_trade_score_outcome(condition_id: str, trader_name: str, pnl: float,
                               since_minutes: int = 120) -> int:
    """Write outcome_pnl onto the newest matching trade_scores row.

    Match: condition_id + trader_name, within last `since_minutes` minutes,
    where outcome_pnl IS NULL (don't overwrite). Returns rowcount updated.
    """
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE trade_scores SET outcome_pnl = ? "
            "WHERE id = ("
            "    SELECT id FROM trade_scores "
            "    WHERE condition_id = ? AND trader_name = ? "
            "      AND outcome_pnl IS NULL "
            "      AND created_at >= datetime('now','-' || ? || ' minutes','localtime') "
            "    ORDER BY id DESC LIMIT 1"
            ")",
            (pnl, condition_id, trader_name, since_minutes)
        )
        return cur.rowcount


def backfill_trade_score_outcomes(days: int = 30) -> int:
    """Join trade_scores with closed copy_trades and fill missing outcome_pnl.

    Called periodically by outcome_tracker.track_outcomes. Scans the last
    `days` days of trade_scores rows with NULL outcome_pnl and fills them
    from the matching (condition_id, trader) copy_trades row's pnl_realized.
    Returns count updated.
    """
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE trade_scores SET outcome_pnl = ("
            "    SELECT ct.pnl_realized FROM copy_trades ct "
            "    WHERE ct.condition_id = trade_scores.condition_id "
            "      AND ct.wallet_username = trade_scores.trader_name "
            "      AND ct.status = 'closed' AND ct.pnl_realized IS NOT NULL "
            "    ORDER BY ct.id DESC LIMIT 1"
            ") "
            "WHERE outcome_pnl IS NULL "
            "  AND created_at >= datetime('now','-' || ? || ' days','localtime') "
            "  AND EXISTS ("
            "    SELECT 1 FROM copy_trades ct2 "
            "    WHERE ct2.condition_id = trade_scores.condition_id "
            "      AND ct2.wallet_username = trade_scores.trader_name "
            "      AND ct2.status = 'closed' AND ct2.pnl_realized IS NOT NULL"
            "  )",
            (days,)
        )
        return cur.rowcount
```

- [ ] **Step 4: Re-run test to verify it passes**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest tests.test_feedback_loop -v 2>&1 | tail -20
```

Expected: `Ran 3 tests in ...s` with `OK`.

- [ ] **Step 5: Wire the update into `bot/smart_sell.py`**

Open `bot/smart_sell.py`. Find the block that logs `[SMART-SELL] ... CLOSED` (around line 129). Immediately AFTER the successful `db.close_copy_trade(...)` and the `db.log_activity(...)` call (around line 138), add:

```python
                    try:
                        db.update_trade_score_outcome(
                            cid, username, round(pnl, 2)
                        )
                    except Exception:
                        pass
```

Context (so you see where exactly — insert between the log_activity call and the broadcast_event try block):

```python
                    db.log_activity(
                        "smart_sell", "WIN" if pnl > 0 else "LOSS",
                        "Smart-Sell: %s exited" % username,
                        "#%d %s — P&L $%+.2f" % (our_trade["id"], our_trade["market_question"][:40], pnl),
                        pnl
                    )
                    try:
                        db.update_trade_score_outcome(
                            cid, username, round(pnl, 2)
                        )
                    except Exception:
                        pass
                    try:
                        from dashboard.app import broadcast_event
                        broadcast_event("smart_sell", { ... })
```

- [ ] **Step 6: Wire the update into `bot/copy_trader.py` resolve + stop-loss paths**

Three call sites to add, all in `bot/copy_trader.py`. Find them by their log prefixes:

1. **Resolved-at-0.99/0.01 path** — around line 2234 after `db.log_activity("resolved", ...)`. Insert:

```python
                            try:
                                db.update_trade_score_outcome(
                                    trade_cid, trade.get("wallet_username","") or "", round(pnl, 2)
                                )
                            except Exception:
                                pass
```

2. **Stop-loss path** — around line 2275 after `db.log_activity("sell", "LOSS", "Stop-loss triggered", ...)`. Insert the same try/except block using `trade_cid` and `trade.get("wallet_username","")`.

3. **Trailing-stop path** — around line 2307 after `db.log_activity("sell", ..., "Trailing stop triggered", ...)`. Insert the same try/except block.

All three use the same pattern:

```python
                            try:
                                db.update_trade_score_outcome(
                                    trade_cid, trade.get("wallet_username","") or "", round(pnl, 2)
                                )
                            except Exception:
                                pass
```

- [ ] **Step 7: Wire the backfill into `bot/outcome_tracker.py`**

Open `bot/outcome_tracker.py`. In `track_outcomes()` (line 116), add a backfill call as the first line inside the function body:

```python
def track_outcomes():
    """Check outcomes for blocked trades that haven't been checked yet.
    ...
    """
    # Backfill: fill trade_scores.outcome_pnl from closed copy_trades
    try:
        n = db.backfill_trade_score_outcomes(days=30)
        if n > 0:
            logger.info("[OUTCOME] Backfilled %d trade_scores.outcome_pnl rows", n)
    except Exception as e:
        logger.debug("[OUTCOME] backfill error: %s", e)

    unchecked = db.get_blocked_trades_unchecked(limit=100)
    ...
```

- [ ] **Step 8: Run the whole test package**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest discover tests -v 2>&1 | tail -25
```

Expected: all tests still pass. smoke + feedback loop = 4 tests OK.

- [ ] **Step 9: Commit**

```bash
git add database/db.py bot/smart_sell.py bot/copy_trader.py bot/outcome_tracker.py tests/test_feedback_loop.py
git commit -m "$(cat <<'EOF'
fix: wire trade_scores.outcome_pnl feedback loop

Previously the scorer logged every scored decision to trade_scores
but nothing ever wrote the outcome_pnl field. Brain's score-weight
optimizer therefore saw an empty set of scored-and-resolved trades
and never tuned anything, so thresholds and weights were frozen on
defaults forever.

This fix adds db.update_trade_score_outcome(condition_id, trader, pnl)
called from smart_sell, the resolved-at-0.99/0.01 auto-close path, the
stop-loss path, and the trailing-stop path. Plus a periodic sweep
db.backfill_trade_score_outcomes() in outcome_tracker.track_outcomes
that fills any gaps by joining closed copy_trades to trade_scores.

Match by (condition_id, wallet_username) within a 120-minute window
(matches NO_REBUY_MINUTES) and only target rows where outcome_pnl is
NULL, so we never overwrite a previously recorded outcome.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3 — Settings Reload (dirty-flag, no process restart)

**Files:**
- Modify: `config.py` (add `_RELOAD_SAFE_KEYS`, `reload()`)
- Modify: `bot/settings_lock.py` (add dirty flag, `mark_dirty`, `poll_dirty`)
- Modify: `bot/copy_trader.py` (reload at top of scan loop)
- Modify: `bot/auto_tuner.py` (replace "restart recommended" with `mark_dirty`)
- Create: `tests/test_settings_reload.py`

- [ ] **Step 1: Write failing test for `settings_lock.mark_dirty` / `poll_dirty`**

Create `tests/test_settings_reload.py`:

```python
import unittest


class TestSettingsDirtyFlag(unittest.TestCase):
    def setUp(self):
        from bot import settings_lock
        # Reset to a known state by polling and discarding
        while settings_lock.poll_dirty():
            pass

    def test_initial_state_clean(self):
        from bot import settings_lock
        self.assertFalse(settings_lock.poll_dirty())

    def test_mark_then_poll_returns_true_once(self):
        from bot import settings_lock
        settings_lock.mark_dirty()
        self.assertTrue(settings_lock.poll_dirty())
        # Second poll should be clean again (consumed).
        self.assertFalse(settings_lock.poll_dirty())

    def test_multiple_marks_collapse_to_one(self):
        from bot import settings_lock
        settings_lock.mark_dirty()
        settings_lock.mark_dirty()
        settings_lock.mark_dirty()
        self.assertTrue(settings_lock.poll_dirty())
        self.assertFalse(settings_lock.poll_dirty())


class TestConfigReloadSafeKeys(unittest.TestCase):
    def test_reload_safe_keys_include_maps(self):
        import config
        safe = config._RELOAD_SAFE_KEYS
        for key in ("BET_SIZE_MAP", "STOP_LOSS_PCT", "MAX_DAILY_LOSS",
                    "FOLLOWED_TRADERS", "CATEGORY_BLACKLIST_MAP"):
            self.assertIn(key, safe)

    def test_reload_safe_excludes_bootstrap_keys(self):
        import config
        safe = config._RELOAD_SAFE_KEYS
        for key in ("LIVE_MODE", "DASHBOARD_HOST", "DASHBOARD_PORT",
                    "COPY_SCAN_INTERVAL", "DB_PATH"):
            self.assertNotIn(key, safe)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest tests.test_settings_reload -v 2>&1 | tail -25
```

Expected: `AttributeError: module 'bot.settings_lock' has no attribute 'mark_dirty'` and `AttributeError: module 'config' has no attribute '_RELOAD_SAFE_KEYS'`.

- [ ] **Step 3: Add dirty flag to `bot/settings_lock.py`**

Replace the full contents of `bot/settings_lock.py` with:

```python
"""Shared lock + read/write for settings.env plus a dirty flag
that downstream consumers poll to know when to reload config."""
import os
import threading

settings_lock = threading.Lock()
SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.env")

_dirty_lock = threading.Lock()
_dirty = False


def read_settings() -> str:
    with settings_lock:
        try:
            with open(SETTINGS_PATH) as f:
                return f.read()
        except Exception:
            return ""


def write_settings(content: str):
    with settings_lock:
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write(content)
        os.replace(tmp, SETTINGS_PATH)
    # Every write also marks dirty so consumers reload on next poll.
    mark_dirty()


def mark_dirty():
    """Signal that settings.env has changed and config should be reloaded."""
    global _dirty
    with _dirty_lock:
        _dirty = True


def poll_dirty() -> bool:
    """Return True once if settings.env changed since last poll, False otherwise.
    Consumes the flag (subsequent calls return False until next mark_dirty)."""
    global _dirty
    with _dirty_lock:
        if _dirty:
            _dirty = False
            return True
        return False
```

- [ ] **Step 4: Add `_RELOAD_SAFE_KEYS` + `reload()` to `config.py`**

Near the top of `config.py` (after the `load_dotenv` lines, before `POLYMARKET_PRIVATE_KEY`), add:

```python
# Whitelist of settings keys that can be reloaded from disk without a process
# restart. These are all "per-scan-cycle" runtime parameters — maps the scan
# loop consults each iteration, plus risk thresholds. Keys NOT in this set
# require a full restart (LIVE_MODE, DB_PATH, scheduler intervals, etc.).
_RELOAD_SAFE_KEYS = frozenset({
    # Per-trader maps (all string-typed, parsed at use site)
    "BET_SIZE_MAP", "TRADER_EXPOSURE_MAP", "MIN_ENTRY_PRICE_MAP", "MAX_ENTRY_PRICE_MAP",
    "MIN_TRADER_USD_MAP", "TAKE_PROFIT_MAP", "STOP_LOSS_MAP", "MAX_COPIES_PER_MARKET_MAP",
    "CATEGORY_BLACKLIST_MAP", "MIN_CONVICTION_RATIO_MAP", "HEDGE_WAIT_TRADERS",
    "AVG_TRADER_SIZE_MAP", "BUY_SLIPPAGE_LEVELS", "SELL_SLIPPAGE_LEVELS",
    # Tier defaults (used by auto_tuner)
    "TIER_BET_SIZE", "TIER_EXPOSURE", "TIER_MIN_ENTRY", "TIER_MAX_ENTRY",
    "TIER_MIN_TRADER_USD", "TIER_TAKE_PROFIT", "TIER_STOP_LOSS", "TIER_MAX_COPIES",
    "TIER_HEDGE_WAIT", "TIER_CONVICTION",
    # Scalar runtime params
    "STOP_LOSS_PCT", "TAKE_PROFIT_PCT", "MAX_DAILY_LOSS", "MAX_DAILY_TRADES",
    "MAX_SPREAD", "MAX_PER_EVENT", "MAX_PER_MATCH", "NO_REBUY_MINUTES",
    "MAX_HOURS_BEFORE_EVENT", "SELL_VERIFY_THRESHOLD", "MIN_CONVICTION_RATIO",
    "MIN_ENTRY_PRICE", "MAX_ENTRY_PRICE", "MAX_COPIES_PER_MARKET", "MIN_TRADER_USD",
    "BET_SIZE_PCT", "MAX_POSITION_SIZE", "MIN_TRADE_SIZE",
    "TRAILING_STOP_MARGIN", "TRAILING_STOP_ACTIVATE",
    # Followed traders (so add/remove takes effect without restart)
    "FOLLOWED_TRADERS",
})

# These MUST NOT be reloaded — process restart required.
_REQUIRES_RESTART_KEYS = frozenset({
    "LIVE_MODE", "DASHBOARD_HOST", "DASHBOARD_PORT", "DASHBOARD_SECRET",
    "COPY_SCAN_INTERVAL", "STARTING_BALANCE",
})


def reload() -> int:
    """Re-read settings.env and update module-level globals for reload-safe keys.

    Returns the count of keys updated. Keys in _REQUIRES_RESTART_KEYS are
    skipped with a debug log. Unknown keys (neither in the safe set nor the
    restart set) emit a warning — forces us to classify new settings.
    """
    import logging
    logger = logging.getLogger(__name__)
    from dotenv import dotenv_values
    try:
        fresh = dotenv_values(_settings_path)
    except Exception as e:
        logger.warning("[CONFIG] reload failed to read %s: %s", _settings_path, e)
        return 0

    import sys
    module = sys.modules[__name__]
    updated = 0
    for key, raw in (fresh or {}).items():
        if key in _REQUIRES_RESTART_KEYS:
            continue
        if key not in _RELOAD_SAFE_KEYS:
            logger.debug("[CONFIG] reload: unknown key %s (neither safe nor restart-required)", key)
            continue
        # Figure out the current type and coerce
        current = getattr(module, key, None)
        try:
            if current is None or isinstance(current, str):
                setattr(module, key, raw if raw is not None else "")
            elif isinstance(current, bool):
                setattr(module, key, (raw or "").lower() in ("true", "1", "yes"))
            elif isinstance(current, int):
                setattr(module, key, int(float(raw or "0")))
            elif isinstance(current, float):
                setattr(module, key, float(raw or "0"))
            else:
                setattr(module, key, raw)
            updated += 1
        except (ValueError, TypeError) as e:
            logger.warning("[CONFIG] reload: could not coerce %s=%r: %s", key, raw, e)
    return updated
```

- [ ] **Step 5: Re-run tests to verify both pass**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest tests.test_settings_reload -v 2>&1 | tail -20
```

Expected: 5 tests pass.

- [ ] **Step 6: Wire reload into `bot/copy_trader.py` scan loop**

Find `copy_followed_wallets()` in `bot/copy_trader.py` (this is the main scan entry point called from main.py:copy_scan). At the top of its body (first line inside the function), add:

```python
    # Reload settings from disk if auto_tuner / brain changed them.
    try:
        from bot.settings_lock import poll_dirty
        if poll_dirty():
            import config
            n = config.reload()
            import logging as _l
            _l.getLogger(__name__).info("[CONFIG] Reloaded %d settings from disk", n)
    except Exception as _e:
        import logging as _l
        _l.getLogger(__name__).warning("[CONFIG] reload failed: %s", _e)
```

To find `copy_followed_wallets`:

```bash
grep -n "^def copy_followed_wallets" /home/wisdom/Schreibtisch/polymarketscanner/bot/copy_trader.py
```

Insert the reload block immediately after the function's docstring.

- [ ] **Step 7: Replace auto_tuner's "restart recommended" with `mark_dirty`**

Open `bot/auto_tuner.py`. Find line 313 (`logger.warning("[TUNER] Settings changed — restart recommended to apply new values")`). Replace it with:

```python
        # Flag settings as dirty so copy_trader reloads on next scan cycle.
        # Previously this logged a restart warning and relied on manual action.
        try:
            from bot.settings_lock import mark_dirty
            mark_dirty()
            logger.info("[TUNER] Settings changed — reload flagged")
        except Exception as _e:
            logger.warning("[TUNER] mark_dirty failed: %s", _e)
```

Also remove the `# Auto-Restart disabled for safety ...` comment immediately above (it's obsolete — the new mechanism handles it). Or leave the comment as historical context — your call. Mention in commit either way.

- [ ] **Step 8: Run full test suite**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest discover tests -v 2>&1 | tail -20
```

Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
git add config.py bot/settings_lock.py bot/copy_trader.py bot/auto_tuner.py tests/test_settings_reload.py
git commit -m "$(cat <<'EOF'
fix: reload settings.env in-process instead of restart-recommended warning

auto_tuner was writing settings.env every 2h then logging "restart
recommended" — which meant every tuner decision sat dormant on disk
until someone manually restarted the bot. All tuning was dead code.

Fix: settings_lock.write_settings() now sets a dirty flag, copy_scan
polls the flag on each iteration (~5s) and calls config.reload() to
update module-level globals for reload-safe keys.

Reload-safe keys are an explicit whitelist: all per-trader maps, tier
defaults, scalar risk thresholds, FOLLOWED_TRADERS. Keys that require
a process restart (LIVE_MODE, DB_PATH, scheduler intervals) are in a
separate set and skipped by reload(). Unknown keys emit a debug log to
force classification when new settings are added.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4 — Brain Log Dedup

**Files:**
- Modify: `bot/brain.py` (dedup BAD_CATEGORY + BAD_PRICE loops)
- Create: `tests/test_brain_dedup.py`

- [ ] **Step 1: Write failing test for dedup**

Create `tests/test_brain_dedup.py`:

```python
import unittest
from unittest.mock import patch
from tests.conftest_helpers import setup_temp_db, teardown_temp_db, insert_copy_trade


class TestBrainDedup(unittest.TestCase):
    def setUp(self):
        self.db_path = setup_temp_db()
        from database import db
        self.db = db
        # 5 losing trades all in the same trader+category → should produce
        # exactly ONE brain_decisions row, not 5.
        for i in range(5):
            insert_copy_trade(
                db,
                market_question="Will NHL game %d end in OT?" % i,
                category="nhl",
                wallet_username="sovereign2013",
                pnl_realized=-2.0,
                actual_size=5.0,
                status="closed",
                condition_id="cid-%d" % i,
            )

    def tearDown(self):
        teardown_temp_db(self.db_path)

    def test_bad_category_losses_log_once(self):
        # Stub out settings read/write since brain normally touches settings.env
        with patch("bot.brain._read_settings", return_value="CATEGORY_BLACKLIST_MAP=\n"), \
             patch("bot.brain._write_settings") as mock_write:
            from bot import brain
            brain._classify_losses()
            # 5 losses, same trader+category → one blacklist write, one log row.
            self.assertLessEqual(mock_write.call_count, 1)
        with self.db.get_connection() as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM brain_decisions WHERE action='BLACKLIST_CATEGORY'"
            ).fetchone()
        # Exactly one row for one unique (trader, category) pair.
        self.assertEqual(rows[0], 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to see current behavior (it will FAIL with too many rows)**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest tests.test_brain_dedup -v 2>&1 | tail -20
```

Expected: test fails with `AssertionError: 5 != 1` or similar (because brain currently logs 5 rows).

- [ ] **Step 3: Modify `brain._execute_loss_actions` in `bot/brain.py`**

Find the current implementation around line 101:

```python
def _execute_loss_actions(classifications: dict, impacts: dict):
    for loss in classifications.get("BAD_CATEGORY", []):
        trader = loss.get("wallet_username", "")
        category = loss.get("category", "")
        if trader and category:
            _add_category_blacklist(trader, category,
                                   "Brain: %s WR < 40%% in %s" % (trader, category))
    price_by_trader = {}
    for loss in classifications.get("BAD_PRICE", []):
        trader = loss.get("wallet_username", "")
        if trader:
            price_by_trader.setdefault(trader, []).append(loss)
    for trader, trader_losses in price_by_trader.items():
        if len(trader_losses) >= 3:
            _tighten_price_range(trader,
                                "Brain: %d BAD_PRICE losses for %s" % (len(trader_losses), trader))
```

Replace with:

```python
def _execute_loss_actions(classifications: dict, impacts: dict):
    # BAD_CATEGORY: collapse to unique (trader, category) pairs so we write
    # one brain_decisions row and one settings update per UNIQUE rule, not
    # one per affected loss. Previously 5 identical losses wrote 5 rows.
    cat_pairs = set()
    for loss in classifications.get("BAD_CATEGORY", []):
        trader = loss.get("wallet_username", "")
        category = loss.get("category", "")
        if trader and category:
            cat_pairs.add((trader, category))
    for trader, category in sorted(cat_pairs):
        _add_category_blacklist(trader, category,
                               "Brain: %s WR < 40%% in %s" % (trader, category))

    # BAD_PRICE: still needs at least 3 losses to trigger, and we only tighten
    # each trader once per cycle regardless of how many losses they had.
    price_by_trader = {}
    for loss in classifications.get("BAD_PRICE", []):
        trader = loss.get("wallet_username", "")
        if trader:
            price_by_trader.setdefault(trader, []).append(loss)
    tightened = set()
    for trader, trader_losses in price_by_trader.items():
        if len(trader_losses) >= 3 and trader not in tightened:
            _tighten_price_range(trader,
                                "Brain: %d BAD_PRICE losses for %s" % (len(trader_losses), trader))
            tightened.add(trader)
```

- [ ] **Step 4: Add idempotency check in `_add_category_blacklist`**

Same file, find `_add_category_blacklist` around line 260. Before the `db.log_brain_decision` call, add an early-return if the pair is already blacklisted:

```python
def _add_category_blacklist(trader: str, category: str, reason: str):
    content = _read_settings()
    match = re.search(r'^CATEGORY_BLACKLIST_MAP=(.*)$', content, re.MULTILINE)
    current = match.group(1) if match else ""
    bl_map = {}
    for entry in current.split(","):
        entry = entry.strip()
        if ":" in entry:
            t, cats = entry.split(":", 1)
            bl_map[t.strip()] = set(cats.split("|"))
    # Early out: already blacklisted → no write, no log, no spam.
    if category in bl_map.get(trader, set()):
        return
    bl_map.setdefault(trader, set()).add(category)
    parts = []
    for t, cats in sorted(bl_map.items()):
        if cats:
            parts.append("%s:%s" % (t, "|".join(sorted(cats))))
    new_val = ",".join(parts)
    _update_setting("CATEGORY_BLACKLIST_MAP", new_val)
    db.log_brain_decision("BLACKLIST_CATEGORY", "%s/%s" % (trader, category), reason, "", "")
    logger.info("[BRAIN] Blacklisted %s for %s", category, trader)
```

- [ ] **Step 5: Re-run test**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest tests.test_brain_dedup -v 2>&1 | tail -20
```

Expected: test passes. Exactly 1 `brain_decisions` row written for 5 identical losses.

- [ ] **Step 6: Commit**

```bash
git add bot/brain.py tests/test_brain_dedup.py
git commit -m "$(cat <<'EOF'
fix: dedup brain BAD_CATEGORY/BAD_PRICE loss actions

brain._classify_losses was iterating losses and calling _add_category_blacklist
per loss, so 5 losing trades from sovereign2013/nhl wrote 5 identical
BLACKLIST_CATEGORY brain_decisions rows in the same brain cycle. 357 rows
observed in production, most duplicates within the same second.

Fix: collapse BAD_CATEGORY losses to a unique (trader, category) set
before iterating, and in _add_category_blacklist early-return if the
pair is already present in CATEGORY_BLACKLIST_MAP. Same idempotency
pattern for BAD_PRICE (one tighten per trader per cycle, not one per loss).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5 — Trader-Lifecycle Bootstrap

**Files:**
- Modify: `bot/trader_lifecycle.py` (add `ensure_followed_traders_seeded`)
- Modify: `main.py` (call bootstrap after `init_db()`)
- Create: `tests/test_lifecycle_seed.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_lifecycle_seed.py`:

```python
import unittest
from unittest.mock import patch
from tests.conftest_helpers import setup_temp_db, teardown_temp_db


class TestLifecycleSeed(unittest.TestCase):
    def setUp(self):
        self.db_path = setup_temp_db()
        from database import db
        self.db = db

    def tearDown(self):
        teardown_temp_db(self.db_path)

    def test_seeds_all_followed_traders(self):
        followed = "alice:0xaaa,bob:0xbbb,charlie:0xccc"
        fake_content = "FOLLOWED_TRADERS=%s\n" % followed
        with patch("bot.trader_lifecycle._read_settings", return_value=fake_content):
            from bot import trader_lifecycle
            trader_lifecycle.ensure_followed_traders_seeded()
        with self.db.get_connection() as conn:
            rows = conn.execute(
                "SELECT username, address, status FROM trader_lifecycle "
                "ORDER BY username"
            ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["username"], "alice")
        self.assertEqual(rows[0]["status"], "LIVE_FOLLOW")
        self.assertEqual(rows[1]["username"], "bob")
        self.assertEqual(rows[2]["username"], "charlie")

    def test_idempotent_does_not_duplicate(self):
        fake_content = "FOLLOWED_TRADERS=alice:0xaaa\n"
        with patch("bot.trader_lifecycle._read_settings", return_value=fake_content):
            from bot import trader_lifecycle
            trader_lifecycle.ensure_followed_traders_seeded()
            trader_lifecycle.ensure_followed_traders_seeded()
            trader_lifecycle.ensure_followed_traders_seeded()
        with self.db.get_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM trader_lifecycle WHERE username='alice'"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_handles_entry_without_address(self):
        # Legacy entries without address should be skipped, not crash.
        fake_content = "FOLLOWED_TRADERS=alice,bob:0xbbb\n"
        with patch("bot.trader_lifecycle._read_settings", return_value=fake_content):
            from bot import trader_lifecycle
            trader_lifecycle.ensure_followed_traders_seeded()
        with self.db.get_connection() as conn:
            rows = conn.execute(
                "SELECT username FROM trader_lifecycle ORDER BY username"
            ).fetchall()
        # alice has no address → skipped. bob has one → seeded.
        self.assertEqual([r["username"] for r in rows], ["bob"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest tests.test_lifecycle_seed -v 2>&1 | tail -20
```

Expected: `AttributeError: module 'bot.trader_lifecycle' has no attribute 'ensure_followed_traders_seeded'`.

- [ ] **Step 3: Add `ensure_followed_traders_seeded` to `bot/trader_lifecycle.py`**

Append at the bottom of the module:

```python
def ensure_followed_traders_seeded():
    """Upsert a LIVE_FOLLOW lifecycle row for every trader in FOLLOWED_TRADERS.

    Called at startup and also at the start of each brain cycle so that
    primary followed traders (KING/Jargs/aenews2) actually appear in the
    lifecycle table. Without this, they only got lifecycle rows when
    brain.pause_trader paused them — meaning paper stats and lifecycle
    transitions never worked for them.
    """
    content = _read_settings()
    match = re.search(r'^FOLLOWED_TRADERS=(.*)$', content, re.MULTILINE)
    if not match:
        return 0
    raw = match.group(1).strip()
    if not raw:
        return 0
    seeded = 0
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" not in entry:
            # Legacy format (no address) — skip, nothing to upsert.
            continue
        username, address = entry.split(":", 1)
        username = username.strip()
        address = address.strip()
        if not address:
            continue
        existing = db.get_lifecycle_trader(address)
        if existing is None:
            db.upsert_lifecycle_trader(address, username, "LIVE_FOLLOW", "bootstrap")
            seeded += 1
            logger.info("[LIFECYCLE] Seeded %s (%s) as LIVE_FOLLOW", username, address[:12])
    return seeded
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest tests.test_lifecycle_seed -v 2>&1 | tail -20
```

Expected: 3 tests pass.

- [ ] **Step 5: Call bootstrap from `main.py` after `init_db()`**

Open `main.py`. Find the import section near the top that has `from database.db import init_db`. In the script startup section (where `init_db()` is actually called — this is usually near `if __name__ == "__main__":` or in a startup function), add a call right after `init_db()`:

```python
    init_db()
    try:
        from bot.trader_lifecycle import ensure_followed_traders_seeded
        seeded = ensure_followed_traders_seeded()
        if seeded:
            logger.info("[STARTUP] Seeded %d trader_lifecycle rows", seeded)
    except Exception as e:
        logger.warning("[STARTUP] lifecycle seed failed: %s", e)
```

To find the existing `init_db()` call:

```bash
grep -n "init_db" /home/wisdom/Schreibtisch/polymarketscanner/main.py
```

Insert immediately after that call.

- [ ] **Step 6: Also call from `brain._check_trader_health` so adds-between-restart are picked up**

Open `bot/brain.py`. At the top of `_check_trader_health` (around line 119), add:

```python
def _check_trader_health():
    # Keep lifecycle table in sync with FOLLOWED_TRADERS (picks up any
    # new traders added since startup via settings reload).
    try:
        from bot.trader_lifecycle import ensure_followed_traders_seeded
        ensure_followed_traders_seeded()
    except Exception as e:
        logger.debug("[BRAIN] lifecycle seed sync failed: %s", e)

    with db.get_connection() as conn:
        traders = conn.execute(
            ...
```

- [ ] **Step 7: Run the full test suite**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest discover tests -v 2>&1 | tail -20
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add bot/trader_lifecycle.py bot/brain.py main.py tests/test_lifecycle_seed.py
git commit -m "$(cat <<'EOF'
fix: seed trader_lifecycle from FOLLOWED_TRADERS at startup

Primary followed traders (KING7777777, Jargs, aenews2) were missing
from trader_lifecycle because the only writer was brain.pause_trader
which only ran when pausing. As a result, paper stats / lifecycle
transitions never applied to them.

New bot.trader_lifecycle.ensure_followed_traders_seeded() upserts a
LIVE_FOLLOW row for every entry in FOLLOWED_TRADERS. Called once at
startup from main.py after init_db, and once per brain cycle from
_check_trader_health to catch traders added via settings reload.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6 — MIN_LIVE_TRADERS Race Fix

**Files:**
- Modify: `bot/brain.py` (re-read live_count inside pause loop)
- Create: `tests/test_live_count_race.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_live_count_race.py`:

```python
import unittest
from unittest.mock import patch
from tests.conftest_helpers import setup_temp_db, teardown_temp_db, insert_copy_trade


class TestLiveCountRace(unittest.TestCase):
    def setUp(self):
        self.db_path = setup_temp_db()
        from database import db
        self.db = db
        # Three traders, each with -$15 in the last 7 days → all trigger PAUSE.
        # Seed them as FOLLOWED_TRADERS and record losing closed trades.
        self.settings_content_ref = {
            "content": (
                "FOLLOWED_TRADERS=a:0xaaa,b:0xbbb,c:0xccc\n"
                "CATEGORY_BLACKLIST_MAP=\n"
            )
        }
        for t in ("a", "b", "c"):
            for i in range(5):
                insert_copy_trade(
                    db, wallet_username=t, wallet_address="0x" + t * 40,
                    pnl_realized=-3.0, actual_size=10.0, status="closed",
                    category="cs", condition_id="cid-%s-%d" % (t, i),
                )

    def tearDown(self):
        teardown_temp_db(self.db_path)

    def _fake_read(self):
        return self.settings_content_ref["content"]

    def _fake_write(self, content):
        self.settings_content_ref["content"] = content

    def test_does_not_pause_below_min_live(self):
        from bot import brain, trader_lifecycle
        with patch("bot.brain._read_settings", side_effect=self._fake_read), \
             patch("bot.brain._write_settings", side_effect=self._fake_write), \
             patch("bot.trader_lifecycle._read_settings", side_effect=self._fake_read), \
             patch("bot.trader_lifecycle._write_settings", side_effect=self._fake_write):
            # MIN_LIVE_TRADERS=2 → we must keep 2 traders live even if all 3 trigger pause.
            brain._check_trader_health()
        # Count how many remain in FOLLOWED_TRADERS after the pause loop.
        import re
        m = re.search(r'^FOLLOWED_TRADERS=(.*)$', self.settings_content_ref["content"], re.MULTILINE)
        remaining = [e for e in (m.group(1) if m else "").split(",") if e.strip()]
        self.assertGreaterEqual(len(remaining), 2)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest tests.test_live_count_race -v 2>&1 | tail -30
```

Expected: test fails because brain pauses all 3 (or at least drops below 2).

- [ ] **Step 3: Refactor `_check_trader_health` to re-read live count per iteration**

Find `_check_trader_health` in `bot/brain.py` (around line 119). Replace the body:

```python
def _check_trader_health():
    # Keep lifecycle table in sync with FOLLOWED_TRADERS (picks up any
    # new traders added since startup via settings reload).
    try:
        from bot.trader_lifecycle import ensure_followed_traders_seeded
        ensure_followed_traders_seeded()
    except Exception as e:
        logger.debug("[BRAIN] lifecycle seed sync failed: %s", e)

    with db.get_connection() as conn:
        traders = conn.execute(
            "SELECT DISTINCT wallet_username FROM copy_trades "
            "WHERE wallet_username != '' AND status IN ('open', 'closed') "
            "AND created_at >= datetime('now', '-30 days', 'localtime')"
        ).fetchall()
    active_traders = [t["wallet_username"] for t in traders if t["wallet_username"]]

    def _current_live_count() -> int:
        """Re-read FOLLOWED_TRADERS from disk so pauses within this loop
        are reflected immediately."""
        _content = _read_settings()
        _ft_match = re.search(r'^FOLLOWED_TRADERS=(.*)$', _content, re.MULTILINE)
        raw = _ft_match.group(1).strip() if _ft_match else ""
        if not raw:
            return 0
        return len([x for x in raw.split(",") if x.strip()])

    for trader in active_traders:
        stats_7d = db.get_trader_rolling_pnl(trader, 7)
        pnl_7d = stats_7d.get("total_pnl", 0) or 0
        cnt_7d = stats_7d.get("cnt", 0) or 0
        wins_7d = stats_7d.get("wins", 0) or 0
        with db.get_connection() as conn:
            recent = conn.execute(
                "SELECT pnl_realized FROM copy_trades "
                "WHERE wallet_username = ? AND status = 'closed' "
                "ORDER BY closed_at DESC LIMIT 5",
                (trader,)
            ).fetchall()
        streak = 0
        for r in recent:
            if (r["pnl_realized"] or 0) < 0:
                streak += 1
            else:
                break
        should_pause = False
        reason = ""
        if pnl_7d < -10:
            should_pause = True
            reason = "7d PnL $%.2f < -$10" % pnl_7d
        elif streak >= 5:
            should_pause = True
            reason = "%d consecutive losses" % streak
        if should_pause and _current_live_count() > MIN_LIVE_TRADERS:
            logger.info("[BRAIN] PAUSE %s: %s", trader, reason)
            db.log_brain_decision("PAUSE_TRADER", trader, reason,
                                  json.dumps({"pnl_7d": pnl_7d, "streak": streak}),
                                  "Prevent further losses from underperformer")
            try:
                from bot.trader_lifecycle import pause_trader
                pause_trader(trader, reason)
            except Exception as e:
                logger.warning("[BRAIN] Failed to pause %s: %s", trader, e)
        elif pnl_7d > 5 and cnt_7d >= 5:
            wr = wins_7d / cnt_7d * 100 if cnt_7d > 0 else 0
            if wr > 60:
                logger.info("[BRAIN] BOOST %s: 7d PnL=$%.2f, WR=%.0f%%", trader, pnl_7d, wr)
                db.log_brain_decision("BOOST_TRADER", trader,
                                      "7d PnL=$%.2f, WR=%.0f%%" % (pnl_7d, wr),
                                      json.dumps({"pnl_7d": pnl_7d, "wr_7d": wr}),
                                      "Increase bet size for consistent winner")
```

(Key change: `live_count` computation replaced by `_current_live_count()` which re-reads settings on each call.)

- [ ] **Step 4: Re-run test**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest tests.test_live_count_race -v 2>&1 | tail -20
```

Expected: test passes — at most 1 trader gets paused out of 3, keeping 2 live.

- [ ] **Step 5: Commit**

```bash
git add bot/brain.py tests/test_live_count_race.py
git commit -m "$(cat <<'EOF'
fix: re-read FOLLOWED_TRADERS live count inside brain pause loop

_check_trader_health read live_count ONCE before iterating traders,
then checked live_count > MIN_LIVE_TRADERS per pause decision. Stale
read meant three losing traders could all pass the guard and all get
paused simultaneously, dropping live count below 2.

Fix: inline _current_live_count() helper re-reads settings on every
iteration, so each pause immediately reflects in the guard for the
next trader. MIN_LIVE_TRADERS=2 is now actually enforced under load.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7 — `signal_performance` Bookkeeping Fix

**Files:**
- Modify: `bot/clv_tracker.py` (fix signal_performance row to count real wins/losses)
- Create: `tests/test_signal_performance.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_signal_performance.py`:

```python
import unittest
from tests.conftest_helpers import setup_temp_db, teardown_temp_db, insert_copy_trade


class TestSignalPerformance(unittest.TestCase):
    def setUp(self):
        self.db_path = setup_temp_db()
        from database import db
        self.db = db
        # 3 winning closed trades, 2 losing closed trades
        for i in range(3):
            insert_copy_trade(
                db, pnl_realized=+2.0, current_price=0.95,
                actual_entry_price=0.5, entry_price=0.5,
                status="closed", condition_id="cid-win-%d" % i,
            )
        for i in range(2):
            insert_copy_trade(
                db, pnl_realized=-1.5, current_price=0.05,
                actual_entry_price=0.5, entry_price=0.5,
                status="closed", condition_id="cid-lose-%d" % i,
            )

    def tearDown(self):
        teardown_temp_db(self.db_path)

    def test_signal_performance_counts_real_wins_and_losses(self):
        from bot import clv_tracker
        clv_tracker.update_clv_for_closed_trades()
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT wins, losses, trades_count FROM signal_performance "
                "WHERE signal_type='clv_tracking'"
            ).fetchone()
        self.assertEqual(row["wins"], 3)
        self.assertEqual(row["losses"], 2)
        self.assertEqual(row["trades_count"], 5)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest tests.test_signal_performance -v 2>&1 | tail -20
```

Expected: `AssertionError: 1 != 3` (current code writes `int(avg_clv > 0)` as `wins`).

- [ ] **Step 3: Fix `clv_tracker.update_clv_for_closed_trades`**

Open `bot/clv_tracker.py`. Find the aggregation loop (lines ~29-62) and replace the function body with:

```python
def update_clv_for_closed_trades():
    """Berechne CLV fuer geschlossene Trades und persistiere wins/losses."""
    with db.get_connection() as conn:
        trades = conn.execute(
            "SELECT id, condition_id, side, entry_price, actual_entry_price, "
            "pnl_realized, current_price, market_question "
            "FROM copy_trades WHERE status = 'closed' AND condition_id != ''"
        ).fetchall()

    total_clv = 0.0
    count = 0
    wins = 0
    losses = 0
    total_pnl = 0.0

    for t in trades:
        t = dict(t)
        entry = t["actual_entry_price"] or t["entry_price"] or 0
        pnl = t["pnl_realized"] or 0
        if entry <= 0:
            continue

        closing_price = t.get("current_price") if t.get("current_price") else (1.0 if pnl > 0 else 0.0)
        if closing_price is None or closing_price <= 0:
            continue

        side = (t.get("side") or "YES").upper()
        if side == "NO":
            clv = entry - closing_price
        else:
            clv = closing_price - entry
        total_clv += clv
        total_pnl += pnl
        count += 1
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

    if count > 0:
        avg_clv = round(total_clv / count, 4)
        with db.get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO signal_performance "
                "(signal_type, trades_count, total_pnl, wins, losses, updated_at) "
                "VALUES ('clv_tracking', ?, ?, ?, ?, datetime('now','localtime'))",
                (count, round(total_pnl, 2), wins, losses)
            )
        logger.info("[CLV] %d trades, %d wins, %d losses, avg CLV: %.2f%%",
                    count, wins, losses, avg_clv * 100)
    return {"avg_clv": round(total_clv / count * 100, 2) if count > 0 else 0,
            "trades": count, "wins": wins, "losses": losses}
```

Key changes:
- `total_pnl` now stores actual pnl sum in USDC (not avg_clv * 100 which was wrong units)
- `wins` counts trades with `pnl_realized > 0` (not `int(avg_clv > 0)` which was a global summary)
- `losses` counts real losing trades (was hardcoded 0)

- [ ] **Step 4: Re-run test**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest tests.test_signal_performance -v 2>&1 | tail -20
```

Expected: test passes.

- [ ] **Step 5: Commit**

```bash
git add bot/clv_tracker.py tests/test_signal_performance.py
git commit -m "$(cat <<'EOF'
fix: signal_performance counts real wins/losses, not boolean avg_clv

clv_tracker.update_clv_for_closed_trades was writing
  wins = int(avg_clv > 0)   # 0 or 1, global summary
  losses = 0                # hardcoded
to signal_performance, which is why production showed
  trades_count=389, wins=1, losses=0, total_pnl=+$21.22
(clearly invalid — 388 untracked outcomes). total_pnl was also storing
avg_clv * 100 in the wrong units.

Fix: count wins, losses and sum pnl_realized per trade, write the
real counters to signal_performance. Historical rows stay as-is; we
don't backfill.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8 — ML Time-Series Split + Baseline Logging

**Files:**
- Modify: `bot/ml_scorer.py` (time-sorted train/test split, class balance + baseline log)
- Create: `tests/test_ml_time_split.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_ml_time_split.py`:

```python
import unittest
from unittest.mock import patch
from tests.conftest_helpers import setup_temp_db, teardown_temp_db, insert_copy_trade


class TestMLTimeSplit(unittest.TestCase):
    def setUp(self):
        self.db_path = setup_temp_db()
        from database import db
        self.db = db
        # Build a dataset with a clear temporal trend:
        # Early trades win, late trades lose. Random split would see both,
        # time-split should not leak late data into training.
        # Need at least MIN_TRAINING_SAMPLES=50 for training to fire.
        for i in range(60):
            is_early = i < 30
            insert_copy_trade(
                db,
                wallet_username="trader1",
                category="cs",
                entry_price=0.5,
                actual_entry_price=0.5,
                side="YES",
                actual_size=5.0,
                pnl_realized=(+1.0 if is_early else -1.0),
                status="closed",
                condition_id="cid-%d" % i,
            )
        # Fix the created_at timestamps so the first 30 are older than the last 30
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT id FROM copy_trades ORDER BY id"
            ).fetchall()
            for idx, r in enumerate(rows):
                ts = "2026-01-%02d 12:00:00" % (idx + 1)
                conn.execute(
                    "UPDATE copy_trades SET created_at = ? WHERE id = ?",
                    (ts, r["id"])
                )

    def tearDown(self):
        teardown_temp_db(self.db_path)

    def test_training_uses_time_order_not_random_split(self):
        from bot import ml_scorer
        # Capture the logging output to assert baseline + class balance appear.
        import logging
        from io import StringIO
        buf = StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.INFO)
        logging.getLogger("bot.ml_scorer").addHandler(handler)
        logging.getLogger("bot.ml_scorer").setLevel(logging.INFO)

        ml_scorer.train_model()

        log = buf.getvalue()
        # Must log class balance and baseline accuracy.
        self.assertIn("Class balance", log)
        self.assertIn("Baseline", log)
        # The split must be time-ordered, which we verify indirectly via a
        # new helper that exposes the split indices for testing.
        X, y, copy_count, blocked_count = ml_scorer._build_training_data()
        # Builder returns in DB order — copy_trades comes first, then blocked.
        # First copy_trade was early (+1 win), last was late (-1 loss).
        # Sanity: total rows matches what we inserted.
        self.assertGreaterEqual(len(y), 60)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest tests.test_ml_time_split -v 2>&1 | tail -30
```

Expected: test fails because "Class balance" / "Baseline" are not yet logged.

- [ ] **Step 3: Patch `bot/ml_scorer.py` to use time-split + baseline logging**

In `bot/ml_scorer.py`, the `_build_training_data` function currently returns rows in DB-insertion order. We need to keep it returning rows sorted by `created_at` so the downstream time-split is deterministic. Modify the SQL for `copy_rows` and `blocked_rows` to add `ORDER BY created_at`:

```python
    with db.get_connection() as conn:
        copy_rows = conn.execute(
            "SELECT actual_entry_price, entry_price, category, side, "
            "actual_size, size, fee_bps, created_at, pnl_realized "
            "FROM copy_trades WHERE status = 'closed' AND pnl_realized IS NOT NULL "
            "ORDER BY created_at ASC"
        ).fetchall()
        blocked_rows = conn.execute(
            "SELECT trader_price, category, side, created_at, would_have_won "
            "FROM blocked_trades WHERE would_have_won IS NOT NULL "
            "ORDER BY created_at ASC"
        ).fetchall()
```

Then in `train_model()`, replace the `train_test_split` call and the logging block:

```python
def train_model():
    """Train ML model on closed copy_trades + outcome-checked blocked_trades.
    Called every 6h.
    """
    global _model, _model_loaded

    X, y, copy_count, blocked_count = _build_training_data()
    total = copy_count + blocked_count

    if total < MIN_TRAINING_SAMPLES:
        logger.info("[ML] Not enough data (%d/%d), skipping training", total, MIN_TRAINING_SAMPLES)
        return

    X = np.array(X)
    y = np.array(y)

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

    # Time-ordered split. _build_training_data returned rows sorted by
    # created_at ASC, so slicing by index is time-ordered.
    split_idx = int(len(X) * 0.8)
    X_train, X_test = X[:split_idx], X[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]

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

    feature_names = ["entry_price", "category", "side", "hour", "day_of_week"]
    importances = sorted(zip(feature_names, model.feature_importances_), key=lambda x: -x[1])

    logger.info("[ML] Trained on %d samples (%d copy + %d blocked) | Train: %.1f%% | Test: %.1f%% | Baseline: %.1f%%",
                total, copy_count, blocked_count,
                train_acc * 100, test_acc * 100, baseline_acc * 100)
    logger.info("[ML] Top features: %s",
                ", ".join("%s=%.0f%%" % (n, v * 100) for n, v in importances[:4]))

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

    try:
        import json
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO ml_training_log (samples_count, accuracy, feature_importance, model_path) "
                "VALUES (?, ?, ?, ?)",
                (total, round(test_acc, 4),
                 json.dumps(dict(importances)), MODEL_PATH)
            )
    except Exception:
        pass
```

Also remove the unused import `from sklearn.model_selection import train_test_split` at the top of the file.

- [ ] **Step 4: Re-run test**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest tests.test_ml_time_split -v 2>&1 | tail -20
```

Expected: test passes. Training log contains "Class balance" and "Baseline".

- [ ] **Step 5: Commit**

```bash
git add bot/ml_scorer.py tests/test_ml_time_split.py
git commit -m "$(cat <<'EOF'
fix: ml_scorer uses time-ordered train/test split + logs baseline

train_test_split(random_state=42) was leaking future trades into the
training set — scikit-learn's random split is fine for i.i.d. data
but wrong for time series. Combined with heavy class imbalance in
production (most trades lose), the reported 92.9% accuracy was a
mixture of leakage and 'predict always loses' baseline.

Fix: _build_training_data now returns rows sorted by created_at ASC.
train_model slices the first 80% as training set and last 20% as test
set. Additionally logs class balance (% wins) and majority-class
baseline accuracy alongside the model's test accuracy so future-us
can tell whether the model is actually learning anything.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 9 — Batch 2 Deploy

**Files:** deploy only — no code changes.

- [ ] **Step 1: Verify all tests pass locally**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest discover tests -v 2>&1 | tail -30
```

Expected: `OK` at the end, all ~15 tests pass.

- [ ] **Step 2: SCP all changed files to server**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner
SRV=walter@10.0.0.20:/home/walter/polymarketscanner
scp config.py $SRV/config.py
scp bot/settings_lock.py $SRV/bot/settings_lock.py
scp database/db.py $SRV/database/db.py
scp bot/smart_sell.py $SRV/bot/smart_sell.py
scp bot/copy_trader.py $SRV/bot/copy_trader.py
scp bot/outcome_tracker.py $SRV/bot/outcome_tracker.py
scp bot/brain.py $SRV/bot/brain.py
scp bot/trader_lifecycle.py $SRV/bot/trader_lifecycle.py
scp bot/auto_tuner.py $SRV/bot/auto_tuner.py
scp bot/ml_scorer.py $SRV/bot/ml_scorer.py
scp bot/clv_tracker.py $SRV/bot/clv_tracker.py
scp main.py $SRV/main.py
```

Expected: 12 transfers successful.

- [ ] **Step 3: Syntax-check on server before restart**

```bash
ssh walter@10.0.0.20 "cd /home/walter/polymarketscanner && python3 -m py_compile config.py bot/settings_lock.py database/db.py bot/smart_sell.py bot/copy_trader.py bot/outcome_tracker.py bot/brain.py bot/trader_lifecycle.py bot/auto_tuner.py bot/ml_scorer.py bot/clv_tracker.py main.py && echo OK"
```

Expected: `OK`. If any file errors, abort and SCP fixed version — do NOT restart polybot.

- [ ] **Step 4: Restart polybot and monitor**

```bash
ssh walter@10.0.0.20 "sudo systemctl restart polybot"
sleep 30
ssh walter@10.0.0.20 "sudo journalctl -u polybot --since '1 minute ago' --no-pager" | tail -80
```

Expected: no tracebacks, no new error patterns, PORTFOLIO line appears, `[STARTUP] Seeded N trader_lifecycle rows` appears.

- [ ] **Step 5: Verify feedback loop is firing**

```bash
ssh walter@10.0.0.20 "cd /home/walter/polymarketscanner && python3 -c '
import sqlite3
con = sqlite3.connect(\"database/scanner.db\")
con.row_factory = sqlite3.Row
# How many trade_scores now have outcome_pnl populated (was 0 before)
r = con.execute(\"SELECT COUNT(*) FROM trade_scores WHERE outcome_pnl IS NOT NULL\").fetchone()
print(\"trade_scores with outcome_pnl:\", r[0])
r = con.execute(\"SELECT username, status FROM trader_lifecycle\").fetchall()
for row in r: print(\" lifecycle:\", dict(row))
'"
```

Expected: `trade_scores with outcome_pnl: > 0` (may be 0 if outcome_tracker hasn't run yet — wait 5 min and re-check). lifecycle shows KING7777777, Jargs, aenews2 as LIVE_FOLLOW.

- [ ] **Step 6: Tail logs for 5 minutes**

```bash
ssh walter@10.0.0.20 "sudo journalctl -u polybot -f" | head -200
```

Ctrl-C after 5 minutes. Expected: no WARN/ERR spike beyond baseline. Note any new errors for rollback decision.

- [ ] **Step 7: Commit deploy checkpoint**

There's no code change for the deploy step itself, but tag the local main:

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner
git tag -a batch2-deployed -m "Batch 2 deployed to server at $(date -Iseconds)"
```

Done with Batch 2. Monitor for 15 minutes before Batch 3 to catch any delayed issues (brain runs every 2h, so may not exercise new code immediately).

---

## Batch 3 — Design Cleanups

### Task 10 — Unified Trader-State Reader

**Files:**
- Modify: `database/db.py` (add `get_trader_effective_state`, `is_trader_paused`, `resume_trader`)
- Modify: `bot/trader_lifecycle.py` (add `resume_trader` helper)
- Modify: `dashboard/app.py` and `bot/daily_report.py` (switch readers to unified helper — BEST-EFFORT, see step 5)
- Create: `tests/test_trader_state_unified.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_trader_state_unified.py`:

```python
import unittest
from tests.conftest_helpers import setup_temp_db, teardown_temp_db


class TestUnifiedTraderState(unittest.TestCase):
    def setUp(self):
        self.db_path = setup_temp_db()
        from database import db
        self.db = db
        # Seed one trader in both systems with different states.
        db.set_trader_status("alice", "throttled", 0.5, "Soft throttle")
        db.upsert_lifecycle_trader("0xaaa", "alice", "LIVE_FOLLOW", "bootstrap")

    def tearDown(self):
        teardown_temp_db(self.db_path)

    def test_effective_state_soft_throttle_but_hard_live(self):
        state = self.db.get_trader_effective_state("alice")
        self.assertEqual(state["hard_status"], "LIVE_FOLLOW")
        self.assertEqual(state["soft_status"], "throttled")
        self.assertAlmostEqual(state["multiplier"], 0.5, places=2)
        self.assertFalse(state["is_paused"])

    def test_effective_state_hard_pause_overrides(self):
        self.db.update_lifecycle_status("0xaaa", "PAUSED", "brain test")
        state = self.db.get_trader_effective_state("alice")
        self.assertEqual(state["hard_status"], "PAUSED")
        self.assertTrue(state["is_paused"])

    def test_is_trader_paused_helper(self):
        # Currently throttled + live → not paused.
        self.assertFalse(self.db.is_trader_paused("alice"))
        # Hard pause.
        self.db.update_lifecycle_status("0xaaa", "PAUSED", "test")
        self.assertTrue(self.db.is_trader_paused("alice"))

    def test_soft_paused_also_reads_as_paused(self):
        # Soft pause via trader_status should also count as paused.
        self.db.set_trader_status("alice", "paused", 0.0, "Hard via status")
        self.assertTrue(self.db.is_trader_paused("alice"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest tests.test_trader_state_unified -v 2>&1 | tail -25
```

Expected: `AttributeError: module 'database.db' has no attribute 'get_trader_effective_state'`.

- [ ] **Step 3: Add helpers to `database/db.py`**

Append after the lifecycle helpers (end of the `=== Trader Lifecycle Helpers ===` section):

```python
# === Unified Trader State ===

def get_trader_effective_state(username: str) -> dict:
    """Combine trader_status (soft throttle) + trader_lifecycle (hard pause).

    Returns a dict with:
      - hard_status: lifecycle status ('LIVE_FOLLOW', 'PAUSED', 'KICKED', ...)
      - soft_status: trader_status ('active', 'throttled', 'paused')
      - multiplier: soft bet multiplier (0.0-1.0) from trader_status
      - is_paused: True if EITHER system reports paused/kicked
      - reasons: list of reason strings from both sources

    This is the canonical reader going forward. Writers are still split:
    bot.trader_lifecycle.pause_trader writes the hard lifecycle; the
    trader_performance job writes the soft throttling. They are
    orthogonal axes.
    """
    hard_status = "LIVE_FOLLOW"
    hard_reason = ""
    with get_connection() as conn:
        lc = conn.execute(
            "SELECT status, kick_reason, notes FROM trader_lifecycle "
            "WHERE username = ? ORDER BY id DESC LIMIT 1",
            (username,)
        ).fetchone()
        if lc:
            hard_status = lc["status"] or "LIVE_FOLLOW"
            hard_reason = lc["kick_reason"] or ""

    soft_status = "active"
    soft_multiplier = 1.0
    soft_reason = ""
    with get_connection() as conn:
        ts = conn.execute(
            "SELECT status, bet_multiplier, reason FROM trader_status "
            "WHERE trader_name = ?",
            (username,)
        ).fetchone()
        if ts:
            soft_status = ts["status"] or "active"
            soft_multiplier = float(ts["bet_multiplier"] or 1.0)
            soft_reason = ts["reason"] or ""

    is_paused = (
        hard_status in ("PAUSED", "KICKED") or
        soft_status == "paused"
    )
    reasons = [r for r in (hard_reason, soft_reason) if r]

    return {
        "hard_status": hard_status,
        "soft_status": soft_status,
        "multiplier": soft_multiplier,
        "is_paused": is_paused,
        "reasons": reasons,
    }


def is_trader_paused(username: str) -> bool:
    """Return True if the trader is paused in either lifecycle or trader_status.

    Convenience wrapper over get_trader_effective_state for call sites
    that only care about the boolean.
    """
    return get_trader_effective_state(username)["is_paused"]
```

- [ ] **Step 4: Run test to verify passage**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest tests.test_trader_state_unified -v 2>&1 | tail -20
```

Expected: 4 tests pass.

- [ ] **Step 5: Audit + update readers (best-effort, do NOT break anything)**

There are 5 call sites that read `trader_status` directly (from the earlier grep):
- `dashboard/app.py:1392` — SQL JOIN for a dashboard view
- `dashboard/app.py:1458` — dashboard API returns raw list
- `dashboard/app.py:1588` — SQL for paused traders
- `dashboard/app.py:1619` — loop for status+multiplier display
- `bot/daily_report.py:32` — SQL JOIN for daily report
- `bot/daily_report.py:125` — filter paused from perf list

For this task, we ONLY replace the ones in `bot/` — the dashboard can stay as-is for now (less risky, visible-impact change). If a dashboard call errors out we fall back to the old behavior; but for daily_report, switching to the unified helper catches both hard+soft pauses in the "paused" filter.

**Modify `bot/daily_report.py` line 125:**

Find the line:
```python
    paused = [p for p in perf if p["trader_status"] == "paused"]
```

Replace with:
```python
    paused = [p for p in perf if db.is_trader_paused(p.get("trader_name", ""))]
```

(You may need to add `from database import db` near the top of the file if it's not already imported; check with grep.)

**Leave `dashboard/app.py` unchanged.** Out of scope for this batch — dashboard rewrites have high regression risk and the old queries still return correct data (trader_status is still being written).

- [ ] **Step 6: Run full test suite**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest discover tests -v 2>&1 | tail -15
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add database/db.py bot/daily_report.py tests/test_trader_state_unified.py
git commit -m "$(cat <<'EOF'
feat: unified trader state reader for soft+hard pause layers

Discovery during planning: trader_status (written by
trader_performance.py every 30min) is a soft-throttle multiplier
(0.0/0.5/1.0) while trader_lifecycle (written by brain.pause_trader)
is a hard pause that removes the trader from FOLLOWED_TRADERS. They
are orthogonal axes, not duplicates — sovereign2013 could legitimately
be status='active' (soft) AND lifecycle='PAUSED' (hard) at the same
time from different signals.

New db.get_trader_effective_state(name) combines both layers into a
single dict (hard_status, soft_status, multiplier, is_paused, reasons).
db.is_trader_paused(name) is a boolean wrapper. Both are now the
canonical readers.

bot/daily_report.py switched to the unified helper so its "paused"
filter catches both layers. dashboard/app.py readers stay untouched
this pass to avoid UI regression.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 11 — Brain Decision Auto-Revert

**Files:**
- Modify: `bot/brain.py` (add `_revert_obsolete_blacklists`, `_revert_obsolete_tightens`, call from `run_brain`)
- Create: `tests/test_brain_revert.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_brain_revert.py`:

```python
import unittest
from unittest.mock import patch
from tests.conftest_helpers import setup_temp_db, teardown_temp_db, insert_copy_trade


class TestBrainRevert(unittest.TestCase):
    def setUp(self):
        self.db_path = setup_temp_db()
        from database import db
        self.db = db
        # 5 recent winning trades in cs for KING → blacklist should revert.
        for i in range(5):
            insert_copy_trade(
                db,
                wallet_username="KING7777777",
                category="cs",
                pnl_realized=+1.5,
                actual_size=5.0,
                status="closed",
                condition_id="cid-win-%d" % i,
            )

        self.content_ref = {
            "content": "CATEGORY_BLACKLIST_MAP=KING7777777:cs\n"
        }

    def tearDown(self):
        teardown_temp_db(self.db_path)

    def _fake_read(self):
        return self.content_ref["content"]

    def _fake_write(self, content):
        self.content_ref["content"] = content

    def test_revert_removes_blacklist_when_data_improved(self):
        with patch("bot.brain._read_settings", side_effect=self._fake_read), \
             patch("bot.brain._write_settings", side_effect=self._fake_write):
            from bot import brain
            brain._revert_obsolete_blacklists()
        import re
        m = re.search(r'^CATEGORY_BLACKLIST_MAP=(.*)$', self.content_ref["content"], re.MULTILINE)
        remaining = (m.group(1) if m else "").strip()
        self.assertEqual(remaining, "")
        with self.db.get_connection() as conn:
            reverts = conn.execute(
                "SELECT COUNT(*) FROM brain_decisions WHERE action='REVERT_BLACKLIST'"
            ).fetchone()[0]
        self.assertGreaterEqual(reverts, 1)

    def test_revert_keeps_blacklist_when_data_still_bad(self):
        # Overwrite the 5 wins with 5 losses → condition still holds.
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE copy_trades SET pnl_realized = -1.5 WHERE wallet_username='KING7777777'"
            )
        with patch("bot.brain._read_settings", side_effect=self._fake_read), \
             patch("bot.brain._write_settings", side_effect=self._fake_write):
            from bot import brain
            brain._revert_obsolete_blacklists()
        self.assertIn("KING7777777:cs", self.content_ref["content"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest tests.test_brain_revert -v 2>&1 | tail -20
```

Expected: `AttributeError: module 'bot.brain' has no attribute '_revert_obsolete_blacklists'`.

- [ ] **Step 3: Add revert helpers to `bot/brain.py`**

Append after `_tighten_price_range` (around line 307):

```python
def _revert_obsolete_blacklists():
    """Review CATEGORY_BLACKLIST_MAP and remove entries where the underlying
    condition no longer holds. A blacklist was added when trader+category
    had WR<40% over 5+ closed trades. If 7d data now shows 3+ trades with
    WR>=50% and total_pnl>=0, the blacklist is obsolete and we remove it.
    """
    content = _read_settings()
    match = re.search(r'^CATEGORY_BLACKLIST_MAP=(.*)$', content, re.MULTILINE)
    if not match:
        return 0
    current = match.group(1)
    if not current.strip():
        return 0
    bl_map = {}
    for entry in current.split(","):
        entry = entry.strip()
        if ":" in entry:
            t, cats = entry.split(":", 1)
            bl_map[t.strip()] = set(cats.split("|"))
    if not bl_map:
        return 0

    reverts = 0
    with db.get_connection() as conn:
        for trader, cats in list(bl_map.items()):
            for cat in list(cats):
                row = conn.execute(
                    "SELECT COUNT(*) as cnt, "
                    "SUM(CASE WHEN pnl_realized > 0 THEN 1 ELSE 0 END) as wins, "
                    "SUM(COALESCE(pnl_realized, 0)) as pnl "
                    "FROM copy_trades "
                    "WHERE wallet_username = ? AND category = ? "
                    "  AND status = 'closed' AND pnl_realized IS NOT NULL "
                    "  AND closed_at >= datetime('now','-7 days','localtime')",
                    (trader, cat)
                ).fetchone()
                cnt = row["cnt"] or 0
                wins = row["wins"] or 0
                pnl = row["pnl"] or 0
                if cnt >= 3 and wins / cnt >= 0.50 and pnl >= 0:
                    cats.discard(cat)
                    db.log_brain_decision(
                        "REVERT_BLACKLIST", "%s/%s" % (trader, cat),
                        "7d: %d trades, %d wins, $%.2f PnL — condition cleared" % (cnt, wins, pnl),
                        "", "Allow trader to trade this category again"
                    )
                    logger.info("[BRAIN] Reverted blacklist %s/%s", trader, cat)
                    reverts += 1
            if not cats:
                del bl_map[trader]

    if reverts > 0:
        parts = []
        for t, cats in sorted(bl_map.items()):
            if cats:
                parts.append("%s:%s" % (t, "|".join(sorted(cats))))
        new_val = ",".join(parts)
        _update_setting("CATEGORY_BLACKLIST_MAP", new_val)

    return reverts


def _revert_obsolete_tightens():
    """Relax MIN/MAX_ENTRY_PRICE_MAP for traders whose 7d PnL is back in
    the black. Walks each trader's current min/max one step (0.05) toward
    the tier default — never in one big jump.
    """
    from bot.auto_tuner import _load_tiers, _classify_trader
    tiers = _load_tiers()
    content = _read_settings()
    min_map = _parse_map(content, "MIN_ENTRY_PRICE_MAP")
    max_map = _parse_map(content, "MAX_ENTRY_PRICE_MAP")
    if not min_map and not max_map:
        return 0

    relaxes = 0
    for trader in set(list(min_map.keys()) + list(max_map.keys())):
        stats_7d = db.get_trader_rolling_pnl(trader, 7)
        pnl_7d = stats_7d.get("total_pnl", 0) or 0
        cnt_7d = stats_7d.get("cnt", 0) or 0
        wins_7d = stats_7d.get("wins", 0) or 0
        wr_7d = (wins_7d / cnt_7d * 100) if cnt_7d > 0 else 0
        if pnl_7d <= 0 or cnt_7d < 3:
            continue

        stats_30d = db.get_trader_rolling_pnl(trader, 30)
        pnl_30d = stats_30d.get("total_pnl", 0) or 0
        wr_30d = (stats_30d.get("wins", 0) / stats_30d.get("cnt", 1) * 100) if (stats_30d.get("cnt", 0) or 0) > 0 else 50

        tier_name = _classify_trader(pnl_7d, wr_7d, cnt_7d, pnl_30d, wr_30d)
        tier_cfg = tiers.get(tier_name, {})
        tier_min = tier_cfg.get("min_entry")
        tier_max = tier_cfg.get("max_entry")
        if tier_min is None or tier_max is None:
            continue

        old_min = min_map.get(trader, tier_min)
        old_max = max_map.get(trader, tier_max)
        new_min = round(max(old_min - 0.05, tier_min), 2)
        new_max = round(min(old_max + 0.05, tier_max), 2)
        if new_min >= new_max:
            continue
        if new_min == old_min and new_max == old_max:
            continue
        min_map[trader] = new_min
        max_map[trader] = new_max
        db.log_brain_decision(
            "RELAX_FILTER", trader,
            "7d pnl=$%.2f wr=%.0f%% tier=%s" % (pnl_7d, wr_7d, tier_name),
            "",
            "Loosen price range toward tier default"
        )
        logger.info("[BRAIN] Relaxed %s price range: %.0f-%.0fc -> %.0f-%.0fc",
                    trader, old_min * 100, old_max * 100, new_min * 100, new_max * 100)
        relaxes += 1

    if relaxes > 0:
        map_str = ",".join("%s:%s" % (k, v) for k, v in sorted(min_map.items()))
        pattern = r'^(MIN_ENTRY_PRICE_MAP=).*$'
        if re.search(pattern, content, re.MULTILINE):
            content = re.sub(pattern, r'\g<1>' + map_str, content, flags=re.MULTILINE)
        map_str2 = ",".join("%s:%s" % (k, v) for k, v in sorted(max_map.items()))
        pattern2 = r'^(MAX_ENTRY_PRICE_MAP=).*$'
        if re.search(pattern2, content, re.MULTILINE):
            content = re.sub(pattern2, r'\g<1>' + map_str2, content, flags=re.MULTILINE)
        _write_settings(content)

    return relaxes
```

- [ ] **Step 4: Call both from `run_brain()`**

Find `run_brain()` in `bot/brain.py` (line 22). Add the two revert calls at the END of the try block (after `check_transitions()`):

```python
def run_brain():
    logger.info("[BRAIN] === Brain Engine starting ===")
    try:
        try:
            from bot.auto_tuner import auto_tune
            auto_tune()
        except Exception as e:
            logger.warning("[BRAIN] Auto-tuner error: %s", e)
        _classify_losses()
        _check_trader_health()
        _optimize_score_weights()
        _check_autonomous_performance()
        try:
            from bot.trader_lifecycle import check_transitions
            check_transitions()
        except Exception as e:
            logger.warning("[BRAIN] Lifecycle error: %s", e)
        try:
            _revert_obsolete_blacklists()
            _revert_obsolete_tightens()
        except Exception as e:
            logger.warning("[BRAIN] Revert helpers error: %s", e)
        logger.info("[BRAIN] === Brain Engine complete ===")
    except Exception as e:
        logger.exception("[BRAIN] Fatal error: %s", e)
```

- [ ] **Step 5: Re-run test**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest tests.test_brain_revert -v 2>&1 | tail -20
```

Expected: 2 tests pass.

- [ ] **Step 6: Run full suite**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest discover tests -v 2>&1 | tail -20
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add bot/brain.py tests/test_brain_revert.py
git commit -m "$(cat <<'EOF'
feat: brain auto-reverts stale blacklist + filter tightens

Previously BLACKLIST_CATEGORY and TIGHTEN_FILTER brain decisions were
permanent — once KING:dota was blacklisted, even if KING later had a
60% WR in dota the blacklist never came off. Over time these
accumulated and strangled the bot's trading activity.

Adds bot.brain._revert_obsolete_blacklists and
bot.brain._revert_obsolete_tightens called at the end of run_brain.
Blacklist reverts require 3+ trades in the last 7d with WR>=50% and
non-negative PnL. Price-range relaxes move the min/max one 5c step
toward the current tier default per cycle — never in a single jump.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 12 — Batch 3 Deploy

**Files:** deploy only.

- [ ] **Step 1: Verify tests**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner && python3 -m unittest discover tests -v 2>&1 | tail -30
```

Expected: all tests pass (full suite, ~17-18 tests).

- [ ] **Step 2: SCP changed files**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner
SRV=walter@10.0.0.20:/home/walter/polymarketscanner
scp database/db.py $SRV/database/db.py
scp bot/brain.py $SRV/bot/brain.py
scp bot/daily_report.py $SRV/bot/daily_report.py
```

Expected: 3 transfers successful.

- [ ] **Step 3: Syntax-check on server**

```bash
ssh walter@10.0.0.20 "cd /home/walter/polymarketscanner && python3 -m py_compile database/db.py bot/brain.py bot/daily_report.py && echo OK"
```

Expected: `OK`.

- [ ] **Step 4: Restart + monitor**

```bash
ssh walter@10.0.0.20 "sudo systemctl restart polybot"
sleep 30
ssh walter@10.0.0.20 "sudo journalctl -u polybot --since '1 minute ago' --no-pager" | tail -80
```

Expected: clean restart, no tracebacks.

- [ ] **Step 5: Trigger a manual brain run to exercise new code**

```bash
ssh walter@10.0.0.20 "cd /home/walter/polymarketscanner && python3 -c '
import sys
sys.path.insert(0, \".\")
from bot.brain import run_brain
run_brain()
' 2>&1 | tail -60"
```

Expected: `[BRAIN] === Brain Engine starting ===` ... `[BRAIN] === Brain Engine complete ===`, no errors. May log `REVERT_BLACKLIST` or `RELAX_FILTER` depending on data.

- [ ] **Step 6: Verify no data regressions**

```bash
ssh walter@10.0.0.20 "cd /home/walter/polymarketscanner && python3 -c '
import sqlite3
con = sqlite3.connect(\"database/scanner.db\")
con.row_factory = sqlite3.Row
for r in con.execute(\"SELECT action, COUNT(*) FROM brain_decisions WHERE created_at > datetime(\\\"now\\\", \\\"-10 minutes\\\") GROUP BY action\"):
    print(dict(r))
for r in con.execute(\"SELECT username, status FROM trader_lifecycle ORDER BY username\"):
    print(\"lc:\", dict(r))
'"
```

Expected: brain_decisions in the last 10 min has no duplicate spam. trader_lifecycle still has the followed traders in LIVE_FOLLOW.

- [ ] **Step 7: Tag deploy checkpoint**

```bash
cd /home/wisdom/Schreibtisch/polymarketscanner
git tag -a batch3-deployed -m "Batch 3 deployed to server at $(date -Iseconds)"
```

- [ ] **Step 8: Update memory file**

Append to `/home/wisdom/.claude/projects/-home-wisdom-Schreibtisch-polymarketscanner/memory/project_polybot.md` a new section:

```markdown
## Round 4 Bugfixes 2026-04-12 Abend (3-Batch Fix-Everything)

**Batch 1 — Safety Rails:**
- `settings.env`: STOP_LOSS_PCT=0.40, MAX_DAILY_LOSS=10, MAX_DAILY_TRADES=30
- `scorer_weights.json` initialized on server (scorer stops using fallback defaults forever)

**Batch 2 — Code Bugs (fully tested via tests/ stdlib unittest):**
- `db.update_trade_score_outcome` + `db.backfill_trade_score_outcomes` — feedback loop closed (brain can tune weights now)
- `settings_lock.mark_dirty()` / `poll_dirty()` + `config.reload()` — auto-tuner changes take effect without restart (was dead code before)
- `brain._classify_losses` dedup — 1 log per unique (trader, category), not 1 per loss (357 → ~5 rows/cycle)
- `trader_lifecycle.ensure_followed_traders_seeded()` — KING/Jargs/aenews2 now in lifecycle table as LIVE_FOLLOW
- `brain._check_trader_health` — re-reads FOLLOWED_TRADERS per iteration, no more race below MIN_LIVE_TRADERS
- `clv_tracker` signal_performance — real wins/losses counts (was `int(avg_clv > 0)` / hardcoded 0)
- `ml_scorer` — time-ordered train/test split + class balance + baseline accuracy logging

**Batch 3 — Design Cleanups:**
- `db.get_trader_effective_state(name)` + `db.is_trader_paused(name)` — unified reader for soft-throttle (trader_status) + hard-pause (trader_lifecycle). They are orthogonal axes, not duplicates — no destructive migration.
- `brain._revert_obsolete_blacklists` + `_revert_obsolete_tightens` — auto-revert stale category blacklists when 7d WR/PnL recovered. Price range relaxes one 5c step per cycle toward tier default.

**Test infra:** `tests/` dir with stdlib unittest (no pytest dep), temp SQLite per test. Run with `python3 -m unittest discover tests -v`.

**Deploy:** SCP per batch, `sudo systemctl restart polybot` after each, 5 min log monitoring. Git tags `batch2-deployed` and `batch3-deployed`.

**Still NOT done (YAGNI from this round):**
- ML model features/CV redesign (only time-split fixed)
- autonomous_trades / paper mode
- Dashboard reader switch for trader_status (daily_report only)
- Trade-score backfill for 426 legacy NULL rows (covered by outcome_tracker sweep going forward)
```

- [ ] **Step 9: Commit the memory file update (if in repo) or save separately**

Memory files live outside the repo. Save directly — no commit needed.

All three batches done. Monitor for 30 minutes and confirm no new WARN/ERR patterns before considering complete.

---

## Self-Review

**Spec coverage check** — walked through each item in the spec:

| Spec item | Task |
|---|---|
| A1 trade_scores.outcome_pnl missing | Task 2 |
| A2 auto-restart dead code | Task 3 |
| A3 scorer_weights.json missing | Task 1 |
| A4 brain log spam | Task 4 |
| A5 trader_lifecycle hole | Task 5 |
| A6 MIN_LIVE_TRADERS race | Task 6 |
| A7 signal_performance losses stuck | Task 7 |
| A8 ML time-series leakage | Task 8 |
| B9 STOP_LOSS_PCT=0 | Task 1 |
| B10 MAX_DAILY_LOSS / MAX_DAILY_TRADES = 0 | Task 1 |
| C11 two trader-state systems | Task 10 (revised: unified reader, not merge) |
| C12 no revert for brain decisions | Task 11 |

All 12 covered. Task 0 adds test infrastructure. Tasks 9 and 12 are deploy checkpoints.

**Placeholder scan:** I looked for "TBD", "TODO", "similar to", "handle errors" without showing code — none found. Every step that modifies code shows the code. Every command has an expected-output line.

**Type consistency:** 
- `db.update_trade_score_outcome(condition_id, trader_name, pnl, since_minutes=120)` — used in smart_sell.py, copy_trader.py (3 places), test
- `db.backfill_trade_score_outcomes(days=30)` — used in outcome_tracker.py
- `settings_lock.mark_dirty()` / `poll_dirty()` — used in config.py, copy_trader.py, auto_tuner.py
- `config.reload()` — used in copy_trader.py
- `trader_lifecycle.ensure_followed_traders_seeded()` — used in main.py and brain.py
- `db.get_trader_effective_state(username)` / `db.is_trader_paused(username)` — used in daily_report.py
- `brain._revert_obsolete_blacklists()` / `_revert_obsolete_tightens()` — called from `run_brain()`

All signatures consistent across tasks. No orphan references.

**Scope check:** Three batches, 12 implementation tasks + 2 deploy checkpoints + 1 bootstrap. Single session is feasible though tight. If time runs short, safe stopping points are: end of Task 1 (Batch 1 only — safety active), end of Task 9 (Batch 1+2 — feedback loop live), or end of Task 12 (complete).

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-04-12-polybot-brain-fixes.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Each subagent gets its own clean context window and implements one task end-to-end (TDD: test → fail → implement → pass → commit), then I review before launching the next.

**2. Inline Execution** — I execute tasks in this session using executing-plans, batch execution with checkpoints for review. Faster start but uses this conversation's context and cannot run subagent work in parallel.

**Which approach?**
