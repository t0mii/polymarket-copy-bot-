import json
import logging
import os
import glob as globlib
import time
import threading
from datetime import datetime, timedelta

from flask import Flask, render_template, jsonify, send_from_directory, request, Response

from database import db
import config

logger = logging.getLogger(__name__)

app = Flask(__name__)


def _check_auth() -> bool:
    """Check dashboard secret from X-Dashboard-Key header, ?key= param, or JSON body."""
    expected = os.getenv("DASHBOARD_SECRET", "changeme")
    key = (request.headers.get("X-Dashboard-Key", "")
           or request.args.get("key", "")
           or ((request.json or {}).get("key", "") if request.is_json else ""))
    return key == expected


@app.route("/api/auth/check", methods=["POST"])
def api_auth_check():
    """Verify dashboard secret — used by frontend unlock button."""
    if _check_auth():
        return jsonify({"status": "ok", "authenticated": True})
    return jsonify({"error": "invalid key", "authenticated": False}), 403


# --- Server-Sent Events for Live Dashboard ---
_sse_clients: list = []
_sse_lock = threading.Lock()


def broadcast_event(event_type: str, data: dict):
    """Send event to all connected SSE clients."""
    msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.append(msg)
            except Exception:
                dead.append(q)
        for d in dead:
            _sse_clients.remove(d)


@app.route("/api/stream")
def sse_stream():
    """SSE endpoint — non-blocking long-poll style (30s timeout to not block Flask)."""
    def generate():
        q = []
        with _sse_lock:
            _sse_clients.append(q)
        try:
            yield "event: connected\ndata: {}\n\n"
            deadline = time.time() + 25  # max 25s then close (client reconnects)
            while time.time() < deadline:
                if q:
                    msg = q.pop(0)
                    yield msg
                else:
                    time.sleep(1)
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with _sse_lock:
                if q in _sse_clients:
                    _sse_clients.remove(q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


_event_start_cache = {}  # slug -> {"start": iso, "end": iso}

@app.route("/api/live-data")
def api_live_data():
    """Dashboard data — reads directly from Polymarket API for accuracy."""
    import requests as _req

    DEPOSIT = float(config.STARTING_BALANCE)
    funder = config.POLYMARKET_FUNDER
    DATA_API = "https://data-api.polymarket.com"
    _sport_map = {"mlb": "\u26BE MLB", "nba": "\U0001F3C0 NBA", "nhl": "\U0001F3D2 NHL",
                  "nfl": "\U0001F3C8 NFL", "ufc": "\U0001F94A UFC", "mma": "\U0001F94A MMA",
                  "atp": "\U0001F3BE ATP", "wta": "\U0001F3BE WTA",
                  "soccer": "\u26BD", "epl": "\u26BD EPL", "ucl": "\u26BD UCL",
                  "efa-": "\u26BD EPL", "lal-": "\u26BD LAL",
                  "copa": "\U0001F3BE", "charleston": "\U0001F3BE", "monte carlo": "\U0001F3BE",
                  "lol": "\U0001F3AE LOL", "csgo": "\U0001F3AE CS", "cs2": "\U0001F3AE CS",
                  "counter-strike": "\U0001F3AE CS", "dota": "\U0001F9D9 DOTA", "valorant": "\U0001F52B VAL",
                  "ncaa": "\U0001F3C0 NCAA",
                  "bundesliga": "\u26BD BL", "freiburg": "\u26BD BL", "bayern": "\u26BD BL",
                  "mex-": "\u26BD MX", "liga mx": "\u26BD MX",
                  "puebla": "\u26BD MX", "juarez": "\u26BD MX", "cruz": "\u26BD MX",
                  "necaxa": "\u26BD MX", "tigre": "\u26BD MX", "tijuana": "\u26BD MX", "mazatl": "\u26BD MX",
                  "southampton": "\u26BD EPL", "barcelona": "\u26BD LAL", "madrid": "\u26BD LAL",
                  "serie a": "\u26BD SA", "premier": "\u26BD EPL",
                  # NHL teams
                  "avalanche": "\U0001F3D2 NHL", "blackhawks": "\U0001F3D2 NHL", "bruins": "\U0001F3D2 NHL",
                  "canadiens": "\U0001F3D2 NHL", "canucks": "\U0001F3D2 NHL", "capitals": "\U0001F3D2 NHL",
                  "coyotes": "\U0001F3D2 NHL", "devils": "\U0001F3D2 NHL", "ducks": "\U0001F3D2 NHL",
                  "flames": "\U0001F3D2 NHL", "flyers": "\U0001F3D2 NHL", "hurricanes": "\U0001F3D2 NHL",
                  "islanders": "\U0001F3D2 NHL", "jets": "\U0001F3D2 NHL", "kings": "\U0001F3D2 NHL",
                  "kraken": "\U0001F3D2 NHL", "lightning": "\U0001F3D2 NHL", "maple leafs": "\U0001F3D2 NHL",
                  "oilers": "\U0001F3D2 NHL", "panthers": "\U0001F3D2 NHL", "penguins": "\U0001F3D2 NHL",
                  "predators": "\U0001F3D2 NHL", "rangers": "\U0001F3D2 NHL", "red wings": "\U0001F3D2 NHL",
                  "sabres": "\U0001F3D2 NHL", "senators": "\U0001F3D2 NHL", "sharks": "\U0001F3D2 NHL",
                  "blues": "\U0001F3D2 NHL", "stars": "\U0001F3D2 NHL", "wild": "\U0001F3D2 NHL",
                  # MLB teams
                  "astros": "\u26BE MLB", "athletics": "\u26BE MLB", "blue jays": "\u26BE MLB",
                  "braves": "\u26BE MLB", "brewers": "\u26BE MLB", "cardinals": "\u26BE MLB",
                  "cubs": "\u26BE MLB", "diamondbacks": "\u26BE MLB", "dodgers": "\u26BE MLB",
                  "guardians": "\u26BE MLB", "mariners": "\u26BE MLB", "marlins": "\u26BE MLB",
                  "mets": "\u26BE MLB", "nationals": "\u26BE MLB", "orioles": "\u26BE MLB",
                  "padres": "\u26BE MLB", "phillies": "\u26BE MLB", "pirates": "\u26BE MLB",
                  "rays": "\u26BE MLB", "red sox": "\u26BE MLB", "reds": "\u26BE MLB",
                  "rockies": "\u26BE MLB", "royals": "\u26BE MLB", "tigers": "\u26BE MLB",
                  "twins": "\u26BE MLB", "white sox": "\u26BE MLB", "yankees": "\u26BE MLB"}

    def _detect_sport(slug, title):
        s = (slug or "").lower() + " " + (title or "").lower()
        for k, v in _sport_map.items():
            if k in s:
                return v
        return ""

    # Real wallet balance
    wallet = 0
    try:
        from bot.order_executor import get_wallet_balance
        wallet = round(get_wallet_balance(), 2)
    except Exception:
        pass

    # Build trader + timestamp lookup from copy_trades DB
    _trader_by_cid = {}
    _time_by_cid = {}
    _closed_at_by_cid = {}
    _db_open_trades = []
    try:
        from database.db import get_connection
        with get_connection() as _conn:
            for _row in _conn.execute(
                "SELECT condition_id, wallet_username, created_at, closed_at, "
                "size, entry_price, status FROM copy_trades "
                "WHERE condition_id != '' AND status != 'baseline' "
                "ORDER BY created_at DESC"
            ).fetchall():
                _trader_by_cid[_row["condition_id"]] = _row["wallet_username"]
                _time_by_cid[_row["condition_id"]] = _row["created_at"] or ""
                if _row["closed_at"]:
                    _closed_at_by_cid[_row["condition_id"]] = _row["closed_at"]
                if _row["status"] == "open":
                    _db_open_trades.append(dict(_row))
    except Exception:
        pass

    # Build open trades lookup by condition_id
    _open_by_cid = {t.get("condition_id", ""): t for t in _db_open_trades if t.get("condition_id")}

    # Real open positions — fetch directly with currentValue/initialValue
    open_positions = []
    try:
        all_raw = []
        _offset = 0
        while True:
            _r = _req.get(f"{DATA_API}/positions", params={
                "user": funder, "limit": 500, "offset": _offset,
                "sizeThreshold": 0, "sortBy": "CURRENT", "sortDirection": "DESC"
            }, timeout=15)
            if not _r.ok: break
            _page = _r.json()
            if not _page: break
            all_raw.extend(_page)
            if len(_page) < 500: break
            _offset += 500

        for rp in all_raw:
            cv = float(rp.get("currentValue", 0) or 0)
            iv = float(rp.get("initialValue", 0) or 0)
            cp = float(rp.get("curPrice", 0) or 0)
            ap = float(rp.get("avgPrice", 0) or 0)
            pnl = float(rp.get("cashPnl", 0) or 0)
            outcome = rp.get("outcome", "")

            if cv < 0.001 and cp < 0.001:
                continue
            # Skip resolved positions (won at 98c+ or lost at 0c) — waiting for redeem
            if cp >= 0.98 or (cp <= 0.01 and iv > 0.01):
                continue

            if outcome.lower() in ("yes", "y"): side = "YES"
            elif outcome.lower() in ("no", "n"): side = "NO"
            else: side = outcome or "YES"

            _sport = _detect_sport(rp.get("slug", ""), rp.get("title", ""))

            _cid = rp.get("conditionId", "")
            _db_size = None
            _db_entry = None
            _db_side = None
            _open_match = _open_by_cid.get(_cid)
            if _cid in _trader_by_cid:
                if not _open_match:
                    continue  # known cid but closed → skip
                _db_size = _open_match.get("size")
                _db_entry = _open_match.get("entry_price")
                _db_side = _open_match.get("side", "")
                # Skip the opposite outcome (we only hold one side)
                if _db_side and _db_side.lower() != (outcome or "").lower() and _db_side.lower() != side.lower():
                    continue
                side = _db_side or side
            _show_size = round(_db_size, 2) if _db_size else round(iv, 2)
            _show_entry = _db_entry if _db_entry else ap
            # PnL: shares × (current_price - entry_price)
            _shares = _show_size / _show_entry if _show_entry > 0 else 0
            _show_pnl = round(_shares * (cp - _show_entry), 2) if _shares > 0 else round(cv - iv, 2)

            open_positions.append({
                "id": hash(_cid) % 10000,
                "wallet_username": _trader_by_cid.get(_cid, "—"),
                "wallet_address": funder,
                "market_question": rp.get("title") or rp.get("question", ""),
                "market_slug": rp.get("slug", ""),
                "sport": _sport,
                "event_slug": rp.get("eventSlug", ""),
                "side": side,
                "outcome_label": outcome if side not in ("YES", "NO") else "",
                "entry_price": _show_entry,
                "current_price": cp,
                "size": _show_size,
                "pnl_unrealized": _show_pnl,
                "condition_id": _cid,
                "created_at": _time_by_cid.get(_cid, ""),
            })
    except Exception:
        pass

    # Fetch event start times from Gamma API (cached per slug)
    _unique_slugs = set(p.get("event_slug", "") for p in open_positions if p.get("event_slug"))
    _uncached = [s for s in _unique_slugs if s not in _event_start_cache]
    for _slug in _uncached:
        try:
            _ev_r = _req.get("https://gamma-api.polymarket.com/events",
                             params={"slug": _slug.split("/")[-1]}, timeout=3)
            _ev_data = _ev_r.json() if _ev_r.ok else None
            if _ev_data:
                _ev = _ev_data[0] if isinstance(_ev_data, list) else _ev_data
                _st = _ev.get("startTime", "") or _ev.get("startDate", "")
                _et = _ev.get("endTime", "") or _ev.get("endDate", "")
                if _st:
                    _event_start_cache[_slug] = {"start": _st, "end": _et}
        except Exception:
            _event_start_cache[_slug] = {}  # mark as tried, don't retry
    for _pos in open_positions:
        _es = _pos.get("event_slug", "")
        _cached = _event_start_cache.get(_es)
        if _cached:
            _pos["event_start"] = _cached.get("start", "")
            _pos["event_end"] = _cached.get("end", "")

    # Closed positions from DB (accurate bot-level data)
    closed_positions = []
    try:
        from database.db import get_connection
        with get_connection() as _conn:
            _closed_rows = _conn.execute(
                "SELECT id, wallet_username, market_question, side, outcome_label, "
                "entry_price, current_price, size, pnl_realized, closed_at, created_at, "
                "market_slug, event_slug FROM copy_trades "
                "WHERE status='closed' AND wallet_username != '' "
                "ORDER BY closed_at DESC LIMIT 100"
            ).fetchall()
            for _cr in _closed_rows:
                _q = _cr["market_question"] or ""
                _side = _cr["side"] or ""
                closed_positions.append({
                    "id": _cr["id"],
                    "wallet_username": _cr["wallet_username"],
                    "wallet_address": funder,
                    "market_question": _q,
                    "side": _side,
                    "outcome_label": _cr["outcome_label"] or "",
                    "entry_price": _cr["entry_price"] or 0,
                    "current_price": _cr["current_price"] or 0,
                    "size": round(_cr["size"] or 0, 2),
                    "pnl_realized": round(_cr["pnl_realized"] or 0, 2),
                    "status": "closed",
                    "market_slug": _cr["market_slug"] or "",
                    "event_slug": _cr["event_slug"] or "",
                    "sport": _detect_sport(_cr["market_slug"] or "", _q),
                    "closed_at": _cr["closed_at"] or "",
                    "created_at": _cr["created_at"] or "",
                })
    except Exception:
        pass

    wins = sum(1 for p in closed_positions if p["pnl_realized"] > 0)
    losses = sum(1 for p in closed_positions if p["pnl_realized"] < 0)
    total_closed = len(closed_positions)

    # Polymarket values (use ALL raw positions for accurate totals, not filtered open_positions)
    open_value = sum(float(rp.get("currentValue", 0) or 0) for rp in all_raw)
    active_value = sum(float(rp.get("currentValue", 0) or 0) for rp in all_raw if 0.01 < float(rp.get("curPrice", 0) or 0) < 0.99)
    redeemable_value = sum(float(rp.get("currentValue", 0) or 0) for rp in all_raw if float(rp.get("curPrice", 0) or 0) >= 0.99)
    total_value = wallet + open_value
    total_pnl = total_value - DEPOSIT
    wr = round(wins / max(total_closed, 1) * 100, 1)

    followed = db.get_followed_wallets()

    # Filter out unattributed old positions from display (keep values in summary)
    display_open = sorted(
        [p for p in open_positions if p["wallet_username"] != "—"],
        key=lambda p: p.get("created_at", "") or "", reverse=True)
    display_closed = closed_positions  # already from DB, no "—" entries

    # Counts from bot-copies only
    bot_wins = wins
    bot_losses = losses
    bot_wr = round(bot_wins / max(total_closed, 1) * 100, 1)

    summary = {
        "total_value": round(total_value, 2),
        "wallet_usdc": wallet,
        "cash_balance": wallet,
        "total_invested": round(open_value, 2),
        "active_value": round(active_value, 2),
        "redeemable_value": round(redeemable_value, 2),
        "total_pnl": round(total_pnl, 2),
        "realized_pnl": 0,
        "unrealized_pnl": 0,
        "daily_pnl": 0,
        "open_trades": len(display_open),
        "closed_trades": len(display_closed),
        "wins": bot_wins,
        "losses": bot_losses,
        "win_rate": bot_wr,
        "starting_balance": DEPOSIT,
    }

    return jsonify({
        "summary": summary,
        "starting_balance": DEPOSIT,
        "open_trades": display_open,
        "closed_trades": display_closed[:50],
        "followed": [dict(w) for w in followed],
        "trader_stats": [{"username": "RN1",
                          "address": funder,
                          "pnl_realized": 0,
                          "pnl_unrealized": round(total_value - DEPOSIT, 2),
                          "wins": wins, "losses": losses,
                          "open": len(open_positions), "closed": total_closed}],
        "activity": [dict(a) for a in db.get_activity_log(limit=200)],
        "timestamp": int(time.time()),
    })


@app.route("/api/report/generate", methods=["POST"])
def api_generate_report():
    """Generate AI performance report."""
    import threading
    from bot.ai_report import generate_report
    result = {"status": "generating"}

    def do_generate():
        report = generate_report()
        result["report"] = report
        result["status"] = "done"

    thread = threading.Thread(target=do_generate, daemon=True)
    thread.start()
    thread.join(timeout=30)

    if result["status"] == "done":
        return jsonify({"report": result.get("report", ""), "status": "ok"})
    return jsonify({"report": "Generating... refresh in a few seconds", "status": "pending"})


@app.route("/api/report/latest")
def api_latest_report():
    """Get most recent AI report."""
    report = db.get_latest_report()
    if report:
        return jsonify({"report": dict(report)["report_text"], "created_at": dict(report)["created_at"]})
    return jsonify({"report": "No reports yet. Click Generate.", "created_at": ""})


@app.route("/")
def index():
    """Root redirects to copybot dashboard."""
    from flask import redirect
    return redirect("/copy")


@app.route("/wallets")
def wallets_page():
    return render_template("index.html")


@app.route("/api/settings")
def api_settings():
    """Current bot settings (read-only)."""
    followed = db.get_followed_wallets()
    def _pct(v): return str(int(v * 100)) + "%"
    def _dlr(v): return "$" + str(v)
    def _sec(v): return str(v) + "s"
    def _x(v): return str(v) + "x"
    def _onoff(v): return "ON" if v else "OFF"
    settings = [
        # --- Core ---
        {"key": "LIVE_MODE", "value": _onoff(config.LIVE_MODE), "desc": "Real money trading"},
        {"key": "STARTING_BALANCE", "value": _dlr(config.STARTING_BALANCE), "desc": "Deposit for P&L calculation"},
        {"key": "COPY_SCAN_INTERVAL", "value": _sec(config.COPY_SCAN_INTERVAL), "desc": "Seconds between scans"},
        # --- Position Sizing ---
        {"key": "BET_SIZE_PCT", "value": _pct(config.BET_SIZE_PCT), "desc": "Base bet as % of portfolio"},
        {"key": "MAX_POSITION_SIZE", "value": _dlr(config.MAX_POSITION_SIZE), "desc": "Max $ per position"},
        {"key": "MIN_TRADE_SIZE", "value": _dlr(config.MIN_TRADE_SIZE), "desc": "Min $ per trade"},
        {"key": "RATIO_MIN", "value": _x(config.RATIO_MIN), "desc": "Min conviction multiplier"},
        {"key": "RATIO_MAX", "value": _x(config.RATIO_MAX), "desc": "Max conviction multiplier"},
        {"key": "BET_SIZE_BASIS", "value": config.BET_SIZE_BASIS, "desc": "Sizing basis (cash or portfolio)"},
        {"key": "BET_SIZE_MAP", "value": config.BET_SIZE_MAP or "default", "desc": "Per-trader base bet % (best traders get bigger bets)"},
        # --- Price Signal ---
        {"key": "PRICE_MULT_HIGH", "value": _x(config.PRICE_MULT_HIGH), "desc": "Multiplier for strong signals (near 0c/100c)"},
        {"key": "PRICE_MULT_MED", "value": _x(config.PRICE_MULT_MED), "desc": "Multiplier for normal signals"},
        {"key": "PRICE_MULT_LOW", "value": _x(config.PRICE_MULT_LOW), "desc": "Multiplier for weak signals (near 50c)"},
        # --- Trade Filters ---
        {"key": "MIN_TRADER_USD", "value": _dlr(config.MIN_TRADER_USD), "desc": "Default min trade size to copy"},
        {"key": "MIN_TRADER_USD_MAP", "value": config.MIN_TRADER_USD_MAP or "default", "desc": "Per-trader min trade size override"},
        {"key": "MIN_ENTRY_PRICE", "value": str(int(config.MIN_ENTRY_PRICE * 100)) + "c", "desc": "Skip bets below this price"},
        {"key": "MAX_ENTRY_PRICE", "value": str(int(config.MAX_ENTRY_PRICE * 100)) + "c", "desc": "Skip bets above this price"},
        {"key": "MAX_COPIES_PER_MARKET", "value": str(config.MAX_COPIES_PER_MARKET), "desc": "Max copies per market"},
        {"key": "MAX_PER_EVENT", "value": _dlr(config.MAX_PER_EVENT), "desc": "Max $ per event/game (0=off)"},
        {"key": "MAX_SPREAD", "value": _pct(config.MAX_SPREAD), "desc": "Max bid/ask spread"},
        {"key": "ENTRY_TRADE_SEC", "value": _sec(config.ENTRY_TRADE_SEC), "desc": "Max trade age to copy"},
        {"key": "NO_REBUY_MINUTES", "value": str(config.NO_REBUY_MINUTES) + " min", "desc": "Block re-entry after close (0=off)"},
        {"key": "MAX_HOURS_BEFORE_EVENT", "value": str(config.MAX_HOURS_BEFORE_EVENT) + "h", "desc": "Queue if event > Xh away (0=off)"},
        {"key": "EVENT_WAIT_MIN_CASH", "value": _dlr(config.EVENT_WAIT_MIN_CASH) if config.EVENT_WAIT_MIN_CASH > 0 else "always queue", "desc": "Only queue when cash < $X (0=always)"},
        # --- Entry Mechanics ---
        {"key": "ENTRY_SLIPPAGE", "value": str(config.ENTRY_SLIPPAGE), "desc": "Added to entry price"},
        {"key": "MAX_ENTRY_PRICE_CAP", "value": str(int(config.MAX_ENTRY_PRICE_CAP * 100)) + "c", "desc": "Hard ceiling after slippage"},
        {"key": "TRADE_SEC_FROM_RESOLVE", "value": _sec(config.TRADE_SEC_FROM_RESOLVE), "desc": "Stop buying before market close"},
        # --- Hedge Detection ---
        {"key": "HEDGE_WAIT_SECS", "value": _sec(config.HEDGE_WAIT_SECS), "desc": "Default hedge wait time"},
        {"key": "HEDGE_WAIT_TRADERS", "value": config.HEDGE_WAIT_TRADERS or "none", "desc": "Per-trader hedge config"},
        # --- Cash Management ---
        {"key": "CASH_FLOOR", "value": _dlr(config.CASH_FLOOR), "desc": "Stop buying below this"},
        {"key": "CASH_RECOVERY", "value": _dlr(config.CASH_RECOVERY), "desc": "Recovery threshold above floor"},
        {"key": "SAVE_POINT_STEP", "value": _dlr(config.SAVE_POINT_STEP), "desc": "Floor increment per recovery"},
        {"key": "CASH_RESERVE", "value": _dlr(config.CASH_RESERVE), "desc": "Permanently reserved cash"},
        {"key": "MAX_OPEN_POSITIONS", "value": str(config.MAX_OPEN_POSITIONS), "desc": "Max simultaneous positions"},
        {"key": "MAX_EXPOSURE_PER_TRADER", "value": _pct(config.MAX_EXPOSURE_PER_TRADER), "desc": "Default max % per trader"},
        {"key": "TRADER_EXPOSURE_MAP", "value": config.TRADER_EXPOSURE_MAP or "default", "desc": "Per-trader exposure overrides"},
        # --- Risk Management ---
        {"key": "MAX_DAILY_LOSS", "value": _dlr(config.MAX_DAILY_LOSS) if config.MAX_DAILY_LOSS > 0 else "OFF", "desc": "Stop after daily loss exceeds $X"},
        {"key": "MAX_DAILY_TRADES", "value": str(config.MAX_DAILY_TRADES) if config.MAX_DAILY_TRADES > 0 else "OFF", "desc": "Max trades per day"},
        {"key": "STOP_LOSS_PCT", "value": _pct(config.STOP_LOSS_PCT) if config.STOP_LOSS_PCT > 0 else "OFF", "desc": "Auto-sell at X% loss"},
        {"key": "TAKE_PROFIT_PCT", "value": _pct(config.TAKE_PROFIT_PCT) if config.TAKE_PROFIT_PCT > 0 else "OFF", "desc": "Auto-sell at X% gain"},
        # --- Feature Toggles ---
        {"key": "COPY_SELLS", "value": _onoff(config.COPY_SELLS), "desc": "Copy sell signals from traders"},
        {"key": "POSITION_DIFF_ENABLED", "value": _onoff(config.POSITION_DIFF_ENABLED), "desc": "Position-diff fallback scan"},
        {"key": "IDLE_REPLACE_ENABLED", "value": _onoff(config.IDLE_REPLACE_ENABLED), "desc": "Auto-replace inactive traders"},
        # --- Circuit Breaker ---
        {"key": "CB_THRESHOLD", "value": str(config.CB_THRESHOLD) + " failures", "desc": "API failures before pause"},
        {"key": "CB_PAUSE_SECS", "value": _sec(config.CB_PAUSE_SECS), "desc": "Pause duration after breaker trips"},
        # --- Fill Verification ---
        {"key": "FILL_VERIFY_DELAY_SECS", "value": _sec(config.FILL_VERIFY_DELAY_SECS), "desc": "Delay before checking fill amount"},
    ]
    traders = []
    for w in followed:
        t = {"username": w["username"], "address": w["address"], "pnl": w["pnl"],
             "win_rate": w["win_rate"], "domain": w["strategy_type"] or "Sports"}
        # Enrich with live leaderboard data if DB has no stats
        if not t["pnl"]:
            try:
                import requests as _rq2
                lr = _rq2.get("https://data-api.polymarket.com/v1/leaderboard",
                              params={"user": w["address"], "timePeriod": "ALL"}, timeout=5)
                if lr.ok and lr.json():
                    ld = lr.json()[0]
                    t["pnl"] = round(float(ld.get("pnl", 0)), 2)
            except Exception:
                pass
        if not t["win_rate"]:
            try:
                from bot.wallet_scanner import fetch_wallet_trades
                st = fetch_wallet_trades(w["address"])
                t["win_rate"] = st["win_rate"]
            except Exception:
                pass
        traders.append(t)
    status = [
        {"name": "Bot Service", "ok": True, "label": "Active"},
        {"name": "Redeem Timer", "ok": True, "label": "Every 15 min"},
        {"name": "WebSocket", "ok": True, "label": "Connected"},
    ]
    return jsonify({"settings": settings, "traders": traders, "status": status})


@app.route("/api/wallets")
def api_wallets():
    wallets = db.get_top_wallets(limit=50)
    return jsonify([dict(w) for w in wallets])


@app.route("/api/wallets/followed")
def api_followed():
    wallets = db.get_followed_wallets()
    return jsonify([dict(w) for w in wallets])


@app.route("/api/wallet/<address>/follow", methods=["POST"])
def api_follow(address):
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    db.toggle_follow(address, 1)
    return jsonify({"status": "ok", "followed": True})


@app.route("/api/wallet/<address>/unfollow", methods=["POST"])
def api_unfollow(address):
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    db.toggle_follow(address, 0)
    return jsonify({"status": "ok", "followed": False})


@app.route("/wallet/<address>")
def wallet_detail(address):
    wallet = db.get_wallet(address)
    history = db.get_wallet_history(address, limit=30)
    return render_template("wallet_detail.html", wallet=wallet, history=history)


@app.route("/reports")
def reports_list():
    reports = []
    if os.path.exists(config.REPORTS_DIR):
        files = sorted(globlib.glob(os.path.join(config.REPORTS_DIR, "report_*.html")), reverse=True)
        for f in files:
            name = os.path.basename(f)
            reports.append({"filename": name, "path": f"/reports/{name}"})
    return render_template("reports.html", reports=reports)


@app.route("/reports/<filename>")
def serve_report(filename):
    return send_from_directory(config.REPORTS_DIR, filename)


@app.route("/api/scan/trigger", methods=["POST"])
def api_trigger_scan():
    """Trigger a scan from the dashboard (runs in background thread)."""
    import threading
    from scan_wallets import run_scan

    def do_scan():
        run_scan(
            limit=config.SCAN_WALLET_LIMIT,
            max_analyze=config.MAX_AI_ANALYSES,
            top_n=config.TOP_N_REPORT,
            open_report=False,
        )

    thread = threading.Thread(target=do_scan, daemon=True)
    thread.start()
    return jsonify({"status": "scan_started"})


# --- Position Copying ---

@app.route("/copy")
def copy_trading():
    import time as _time
    from bot.copy_trader import get_copy_portfolio_summary, STARTING_BALANCE
    summary = get_copy_portfolio_summary()
    open_trades = db.get_open_copy_trades()
    closed_trades = db.get_closed_copy_trades(limit=500)
    all_trades = db.get_all_copy_trades(limit=2000)
    followed = db.get_followed_wallets()
    return render_template(
        "dashboard.html",
        summary=summary,
        open_trades=open_trades,
        closed_trades=closed_trades,
        all_trades=all_trades,
        followed=followed,
        starting_balance=STARTING_BALANCE,
        now_ts=int(_time.time()),
    )


@app.route("/api/copy/trader-stats")
def api_trader_stats():
    """Per-Trader P&L breakdown — zeigt welche Trader profitabel sind."""
    all_trades = db.get_all_copy_trades(limit=5000)
    trader_map = {}
    for t in all_trades:
        addr = t["wallet_address"]
        if addr not in trader_map:
            trader_map[addr] = {
                "username": t["wallet_username"] or addr[:12],
                "address": addr,
                "open": 0, "closed": 0, "wins": 0, "losses": 0,
                "pnl_realized": 0.0, "pnl_unrealized": 0.0,
                "total_invested": 0.0,
            }
        s = trader_map[addr]
        if t["status"] == "open":
            s["open"] += 1
            s["pnl_unrealized"] += (t["pnl_unrealized"] or 0)
            s["total_invested"] += t["size"]
        elif t["status"] == "closed":
            s["closed"] += 1
            pnl = t["pnl_realized"] or 0
            s["pnl_realized"] += pnl
            if pnl > 0:
                s["wins"] += 1
            elif pnl < 0:
                s["losses"] += 1
    stats = sorted(trader_map.values(), key=lambda x: x["pnl_realized"], reverse=True)
    for s in stats:
        total = s["wins"] + s["losses"]
        s["win_rate"] = round(s["wins"] / total * 100, 1) if total > 0 else 0
        s["pnl_total"] = round(s["pnl_realized"] + s["pnl_unrealized"], 2)
        s["pnl_realized"] = round(s["pnl_realized"], 2)
        s["pnl_unrealized"] = round(s["pnl_unrealized"], 2)
    return jsonify(stats)


@app.route("/api/copy/close/<int:trade_id>", methods=["POST"])
def api_close_trade(trade_id):
    """Manually close an open position — sells shares and marks as closed."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403

    from database.db import get_connection
    from bot.order_executor import sell_shares, get_wallet_balance
    from bot.ws_price_tracker import price_tracker

    # Get the trade from DB
    with get_connection() as conn:
        trade = conn.execute(
            "SELECT id, condition_id, side, entry_price, size, market_question, wallet_username "
            "FROM copy_trades WHERE id=? AND status='open'", (trade_id,)
        ).fetchone()

    if not trade:
        return jsonify({"error": "Trade not found or already closed"}), 404

    cid = trade["condition_id"]
    side = trade["side"]
    entry_price = trade["entry_price"] or 0

    # Get current price (WebSocket → API fallback)
    current_price = None
    if cid and price_tracker.is_connected:
        current_price = price_tracker.get_price(cid, side)
    if current_price is None:
        # Fallback: fetch from API
        try:
            import requests as _req
            r = _req.get("https://data-api.polymarket.com/positions", params={
                "user": config.POLYMARKET_FUNDER, "limit": 500, "sizeThreshold": 0
            }, timeout=10)
            if r.ok:
                for p in r.json():
                    if p.get("conditionId") == cid:
                        current_price = float(p.get("curPrice", 0) or 0)
                        break
        except Exception:
            pass
    if current_price is None:
        current_price = entry_price  # last resort

    # LIVE: sell shares on Polymarket
    sell_ok = False
    if config.LIVE_MODE and cid:
        resp = sell_shares(cid, side, current_price)
        sell_ok = resp is not None
        if not sell_ok:
            return jsonify({"error": "Sell order failed", "trade_id": trade_id}), 500
    else:
        sell_ok = True  # paper mode

    # Calculate PnL and close in DB
    shares = trade["size"] / entry_price if entry_price > 0 else 0
    pnl = round((current_price - entry_price) * shares, 2)

    db.close_copy_trade(trade_id, pnl, close_price=current_price)
    db.log_activity("sell", "WIN" if pnl >= 0 else "LOSS",
                     "Manual close — %s" % trade["wallet_username"],
                     "#%d %s — P&L $%+.2f" % (trade_id, (trade["market_question"] or "")[:35], pnl), pnl)

    logger.info("[MANUAL-CLOSE] #%d sold @ %.0fc | PnL $%+.2f | %s",
                trade_id, current_price * 100, pnl, (trade["market_question"] or "")[:40])

    try:
        broadcast_event("trade_closed", {
            "id": trade_id, "trader": trade["wallet_username"],
            "market": (trade["market_question"] or "")[:60],
            "pnl": pnl, "price": round(current_price * 100),
        })
    except Exception:
        pass

    return jsonify({
        "status": "closed",
        "trade_id": trade_id,
        "pnl": pnl,
        "sell_price": current_price,
        "market": trade["market_question"],
    })


@app.route("/api/copy/scan", methods=["POST"])
def api_copy_scan():
    """Manually trigger copy-trade scan of followed wallets."""
    import threading
    from bot.copy_trader import copy_followed_wallets, update_copy_positions

    def do_copy():
        copy_followed_wallets()
        update_copy_positions()

    thread = threading.Thread(target=do_copy, daemon=True)
    thread.start()
    return jsonify({"status": "copy_scan_started"})


@app.route("/api/copy/update", methods=["POST"])
def api_copy_update():
    """Update prices for open positions."""
    import threading
    from bot.copy_trader import update_copy_positions

    thread = threading.Thread(target=update_copy_positions, daemon=True)
    thread.start()
    return jsonify({"status": "update_started"})


@app.route("/api/copy/reset", methods=["POST"])
def api_copy_reset():
    """Reset copy trading: delete all trades, baselines, snapshots. Keep followed wallets."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    confirm = request.args.get("confirm", "")
    if not confirm and request.is_json:
        confirm = (request.json or {}).get("confirm", "")
    if confirm != "RESET":
        return jsonify({"error": "Pass ?confirm=RESET to confirm"}), 400
    db.reset_copy_trading()
    return jsonify({"status": "reset_complete"})


@app.route("/api/copy/chart")
def api_copy_chart():
    """Portfolio chart data for copy trading. Supports ?period=4h|1d|1w|1m|all"""
    period = request.args.get("period", "1d")
    now = datetime.now()
    if period == "4h":
        start = (now - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S")
    elif period == "1d":
        start = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    elif period == "1w":
        start = (now - timedelta(weeks=1)).strftime("%Y-%m-%d %H:%M:%S")
    elif period == "1m":
        start = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    else:
        start = "2020-01-01"
    end = now.strftime("%Y-%m-%d %H:%M:%S")
    snapshots = db.get_copy_snapshots_in_range(start, end)
    return jsonify({
        "labels": [s["created_at"] for s in snapshots],
        "values": [round(s["pnl_total"], 2) for s in snapshots],
    })




@app.route("/api/copy/history")
def api_copy_history():
    """Return positions, chart data, and stats filtered by period."""
    period = request.args.get("period", "week")
    date_from = request.args.get("from")
    date_to = request.args.get("to")

    now = datetime.now()
    if date_from and date_to:
        start = date_from
        end = date_to + " 23:59:59"
    elif period == "day":
        start = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        end = now.strftime("%Y-%m-%d %H:%M:%S")
    elif period == "week":
        start = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        end = now.strftime("%Y-%m-%d %H:%M:%S")
    elif period == "month":
        start = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        end = now.strftime("%Y-%m-%d %H:%M:%S")
    elif period == "year":
        start = (now - timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")
        end = now.strftime("%Y-%m-%d %H:%M:%S")
    else:
        start = "2020-01-01"
        end = now.strftime("%Y-%m-%d %H:%M:%S")

    trades = db.get_copy_trades_in_range(start, end)
    snapshots = db.get_copy_snapshots_in_range(start, end)

    trades_list = [dict(t) for t in trades]
    closed = [t for t in trades_list if t["status"] == "closed"]
    wins = sum(1 for t in closed if (t.get("pnl_realized") or 0) > 0)
    losses = sum(1 for t in closed if (t.get("pnl_realized") or 0) < 0)
    total_pnl = sum(t.get("pnl_realized") or 0 for t in closed)

    return jsonify({
        "trades": trades_list,
        "chart": {
            "labels": [s["created_at"] for s in snapshots],
            "values": [s["total_value"] for s in snapshots],
        },
        "stats": {
            "total": len(trades_list),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(closed) * 100, 1) if closed else 0,
            "pnl": round(total_pnl, 2),
        }
    })
