# Changelog

Session-level notes. For full commit history see `git log`.

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
