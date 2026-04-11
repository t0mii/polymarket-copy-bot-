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
    """Check dashboard secret from X-Dashboard-Key header, ?key= param, or JSON body.
    Query param accepted for backwards compat but header/body preferred (no log leaks).
    """
    expected = config.DASHBOARD_SECRET
    if expected == "changeme":
        logger.warning("DASHBOARD_SECRET is still 'changeme' — change it in settings.env!")
    key = (request.headers.get("X-Dashboard-Key", "")
           # query param removed for security (leaks to server logs)
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
            deadline = time.time() + 120  # max 120s then close (client reconnects in 1s)
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
    _sport_map = {
                  # Tennis tournaments (MUST be before soccer to catch "Barcelona Open" etc)
                  "barcelona open": "\U0001F3BE TENNIS", "bmw open": "\U0001F3BE TENNIS",
                  "rouen": "\U0001F3BE TENNIS", "capfinances": "\U0001F3BE TENNIS",
                  "qualification:": "\U0001F3BE TENNIS", "qualif": "\U0001F3BE TENNIS",
                  "challenger": "\U0001F3BE TENNIS", "sarasota": "\U0001F3BE TENNIS",
                  "roland garros": "\U0001F3BE TENNIS", "wimbledon": "\U0001F3BE TENNIS",
                  "indian wells": "\U0001F3BE TENNIS", "miami open": "\U0001F3BE TENNIS",
                  "rome open": "\U0001F3BE TENNIS",
                  # Main sports
                  "mlb": "\u26BE MLB", "nba": "\U0001F3C0 NBA", "nhl": "\U0001F3D2 NHL",
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
                  "southampton": "\u26BD EPL", "fc barcelona": "\u26BD LAL", "real madrid": "\u26BD LAL",
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
                "SELECT id, condition_id, wallet_username, created_at, closed_at, "
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
            # Prefer actual fill data over planned entry/size
            _actual_entry = _open_match.get("actual_entry_price") if _open_match else None
            _actual_size = _open_match.get("actual_size") if _open_match else None
            _show_size = round(_actual_size, 2) if _actual_size else (round(_db_size, 2) if _db_size else round(iv, 2))
            _show_entry = _actual_entry or _db_entry or ap
            # PnL: shares × (current_price - entry_price)
            _shares = _show_size / _show_entry if _show_entry > 0 else 0
            _show_pnl = round(_shares * (cp - _show_entry), 2) if _shares > 0 else round(cv - iv, 2)

            open_positions.append({
                "id": _open_match.get("id", hash(_cid) % 10000) if _open_match else hash(_cid) % 10000,
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
    # Also include DB slugs (may differ from API slugs)
    for _dbt in _db_open_trades:
        _dbs = _dbt.get("event_slug", "")
        if _dbs:
            _unique_slugs.add(_dbs)
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
    import re as _re_ev
    for _pos in open_positions:
        _es = _pos.get("event_slug", "")
        # Also check DB event_slug if API slug differs
        _cid_lookup = _pos.get("condition_id", "")
        _db_match = _open_by_cid.get(_cid_lookup)
        _db_slug = _db_match.get("event_slug", "") if _db_match else ""
        if not _es and _db_slug:
            _es = _db_slug
            _pos["event_slug"] = _es
        _cached = _event_start_cache.get(_es)
        if not _cached and _db_slug and _db_slug != _es:
            _cached = _event_start_cache.get(_db_slug)
        if _cached:
            _pos["event_start"] = _cached.get("start", "")
            _pos["event_end"] = _cached.get("end", "")
        # Fix: for sports matches, slug date (match day) beats tournament end date
        # e.g. slug "atp-bueno-midon-2026-04-11" → match is today, but API endDate = tournament end
        _slug_date_match = _re_ev.search(r"(\d{4}-\d{2}-\d{2})", _es) if _es else None
        if _slug_date_match:
            _slug_end = _slug_date_match.group(1) + "T23:59:59Z"
            if not _pos.get("event_start"):
                _pos["event_start"] = _slug_date_match.group(1) + "T00:00:00Z"
            # If slug date is BEFORE the API event_end, use slug date (it's the actual match day)
            if _pos.get("event_end") and _slug_end < _pos["event_end"]:
                _pos["event_end"] = _slug_end
            elif not _pos.get("event_end"):
                _pos["event_end"] = _slug_end
        elif not _pos.get("event_start") and _es:
            _date_match = _re_ev.search(r"(\d{4}-\d{2}-\d{2})", _es)
            if _date_match:
                _pos["event_start"] = _date_match.group(1) + "T00:00:00Z"
        # Fallback 2: extract deadline from title ("by April 30", "by end of April", "by June 30")
        if not _pos.get("event_end"):
            _mq = _pos.get("market_question", "")
            _months_map = {"january":"01","february":"02","march":"03","april":"04","may":"05","june":"06","july":"07","august":"08","september":"09","october":"10","november":"11","december":"12"}
            _month_names = "|".join(_months_map.keys())
            _deadline = None
            _yr = "2026"
            _mn = "01"
            _dy = "28"
            if _mq:
                # "by April 30" or "by April 30, 2026"
                _deadline = _re_ev.search(r"by\s+(%s)\s+(\d{1,2})(?:,?\s*(\d{4}))?" % _month_names, _mq, _re_ev.IGNORECASE)
                if _deadline:
                    _yr = _deadline.group(3) or "2026"
                    _mn = _months_map.get(_deadline.group(1).lower(), "01")
                    _dy = _deadline.group(2).zfill(2)
                else:
                    # "by end of April"
                    _end_of = _re_ev.search(r"by\s+end\s+of\s+(%s)(?:\s*(\d{4}))?" % _month_names, _mq, _re_ev.IGNORECASE)
                    if _end_of:
                        _mn = _months_map.get(_end_of.group(1).lower(), "01")
                        _yr = _end_of.group(2) or "2026"
                        # Last day of month
                        import calendar
                        _dy = str(calendar.monthrange(int(_yr), int(_mn))[1])
                        _deadline = _end_of
            if _deadline:
                _pos["event_end"] = "%s-%s-%sT23:59:59Z" % (_yr, _mn, _dy)

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
    _pending_won = [rp for rp in all_raw if float(rp.get("curPrice", 0) or 0) >= 0.99 and float(rp.get("currentValue", 0) or 0) > 0.05]
    redeemable_value = sum(float(rp.get("currentValue", 0) or 0) for rp in _pending_won)
    _resolved_count = sum(1 for rp in _pending_won if rp.get("redeemable"))
    _awaiting_count = sum(1 for rp in _pending_won if not rp.get("redeemable"))
    _resolved_value = sum(float(rp.get("currentValue", 0) or 0) for rp in _pending_won if rp.get("redeemable"))
    _awaiting_value = sum(float(rp.get("currentValue", 0) or 0) for rp in _pending_won if not rp.get("redeemable"))
    total_value = wallet + open_value
    total_pnl = total_value - DEPOSIT
    wr = round(wins / max(total_closed, 1) * 100, 1)

    followed = db.get_followed_wallets()

    # Filter out unattributed old positions from display (keep values in summary)
    display_open = sorted(
        [p for p in open_positions if p["wallet_username"] != "—"],
        key=lambda p: p.get("created_at", "") or "", reverse=True)
    display_closed = closed_positions  # already from DB, no "—" entries

    # Orphan active positions: in wallet but not tracked by bot — merge into open display
    _bot_cids = set(_open_by_cid.keys())
    for rp in all_raw:
        cv = float(rp.get("currentValue", 0) or 0)
        cp = float(rp.get("curPrice", 0) or 0)
        ap = float(rp.get("avgPrice", 0) or 0)
        iv = float(rp.get("initialValue", 0) or 0)
        _cid = rp.get("conditionId", "")
        if cv < 0.10 or _cid in _bot_cids:
            continue
        if cp >= 0.98 or cp <= 0.01:
            continue  # skip resolved/dead — those show in redeemable summary
        outcome = rp.get("outcome", "")
        if outcome.lower() in ("yes", "y"): _side = "YES"
        elif outcome.lower() in ("no", "n"): _side = "NO"
        else: _side = outcome or "YES"
        _shares = iv / ap if ap > 0 else 0
        _pnl = round(_shares * (cp - ap), 2) if _shares > 0 else round(cv - iv, 2)
        display_open.append({
            "id": 0,
            "wallet_username": "orphan",
            "wallet_address": funder,
            "market_question": rp.get("title") or rp.get("question", ""),
            "market_slug": rp.get("slug", ""),
            "sport": _detect_sport(rp.get("slug", ""), rp.get("title", "")),
            "event_slug": rp.get("eventSlug", ""),
            "side": _side,
            "outcome_label": outcome if _side not in ("YES", "NO") else "",
            "entry_price": ap,
            "current_price": cp,
            "size": round(iv, 2),
            "pnl_unrealized": _pnl,
            "condition_id": _cid,
            "created_at": "",
            "is_orphan": True,
        })

    # Apply event time fallback to orphan positions
    for _orph in display_open:
        if not _orph.get("is_orphan"):
            continue
        _oes = _orph.get("event_slug", "")
        # Check gamma cache
        _oc = _event_start_cache.get(_oes)
        if _oc:
            _orph["event_start"] = _oc.get("start", "")
            _orph["event_end"] = _oc.get("end", "")
        # Fix: slug date beats tournament end for orphans too
        _orph_slug_dm = _re_ev.search(r"(\d{4}-\d{2}-\d{2})", _oes) if _oes else None
        if _orph_slug_dm:
            _orph_slug_end = _orph_slug_dm.group(1) + "T23:59:59Z"
            if not _orph.get("event_start"):
                _orph["event_start"] = _orph_slug_dm.group(1) + "T00:00:00Z"
            if _orph.get("event_end") and _orph_slug_end < _orph["event_end"]:
                _orph["event_end"] = _orph_slug_end
            elif not _orph.get("event_end"):
                _orph["event_end"] = _orph_slug_end
        elif not _orph.get("event_start") and _oes:
            _odm = _re_ev.search(r"(\d{4}-\d{2}-\d{2})", _oes)
            if _odm:
                _orph["event_start"] = _odm.group(1) + "T00:00:00Z"
        # Title date fallback
        if not _orph.get("event_end"):
            _omq = _orph.get("market_question", "")
            _months_map = {"january":"01","february":"02","march":"03","april":"04","may":"05","june":"06","july":"07","august":"08","september":"09","october":"10","november":"11","december":"12"}
            _mn_pat = "|".join(_months_map.keys())
            _odl = _re_ev.search(r"by\s+(%s)\s+(\d{1,2})(?:,?\s*(\d{4}))?" % _mn_pat, _omq, _re_ev.IGNORECASE) if _omq else None
            if _odl:
                _orph["event_end"] = "%s-%s-%sT23:59:59Z" % (_odl.group(3) or "2026", _months_map.get(_odl.group(1).lower(),"01"), _odl.group(2).zfill(2))
            elif _omq:
                _oeo = _re_ev.search(r"by\s+end\s+of\s+(%s)(?:\s*(\d{4}))?" % _mn_pat, _omq, _re_ev.IGNORECASE)
                if _oeo:
                    import calendar
                    _omn = _months_map.get(_oeo.group(1).lower(),"01")
                    _oyr = _oeo.group(2) or "2026"
                    _orph["event_end"] = "%s-%s-%sT23:59:59Z" % (_oyr, _omn, str(calendar.monthrange(int(_oyr), int(_omn))[1]))

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
        "resolved_count": _resolved_count,
        "awaiting_count": _awaiting_count,
        "resolved_value": round(_resolved_value, 2),
        "awaiting_value": round(_awaiting_value, 2),
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
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
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
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
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


@app.route("/logs")
def logs_page():
    return render_template("logs.html")


@app.route("/api/logs")
def api_logs():
    """Return last N lines of the bot log, optionally filtered."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    lines = min(int(request.args.get("lines", 200)), 5000)
    filt = request.args.get("filter", "").lower()
    try:
        with open(config.LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = all_lines[-min(lines * 3, len(all_lines)):]  # read extra to compensate for filtering
        if filt:
            filters = filt.split(",")
            tail = [l for l in tail if any(f in l.lower() for f in filters)]
        return jsonify({"lines": [l.rstrip() for l in tail[-lines:]]})
    except Exception as e:
        return jsonify({"lines": [f"Error reading log: {e}"]})


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
        {"key": "AVG_TRADER_SIZE_MAP", "value": config.AVG_TRADER_SIZE_MAP or "default $%.0f" % config.DEFAULT_AVG_TRADER_SIZE, "desc": "Per-trader avg bet size (conviction baseline)"},
        # --- Trade Filters ---
        {"key": "MIN_TRADER_USD", "value": _dlr(config.MIN_TRADER_USD), "desc": "Default min trade size to copy"},
        {"key": "MIN_TRADER_USD_MAP", "value": config.MIN_TRADER_USD_MAP or "default", "desc": "Per-trader min trade size override"},
        {"key": "MIN_CONVICTION_RATIO", "value": _x(config.MIN_CONVICTION_RATIO) if config.MIN_CONVICTION_RATIO > 0 else "OFF", "desc": "Min trader bet / avg ratio to copy (arb noise filter)"},
        {"key": "MIN_CONVICTION_RATIO_MAP", "value": config.MIN_CONVICTION_RATIO_MAP or "default", "desc": "Per-trader conviction filter (e.g. sovereign:1.5x)"},
        {"key": "MIN_ENTRY_PRICE", "value": str(int(config.MIN_ENTRY_PRICE * 100)) + "c", "desc": "Skip bets below this price"},
        {"key": "MIN_ENTRY_PRICE_MAP", "value": config.MIN_ENTRY_PRICE_MAP or "default", "desc": "Per-trader min entry price override"},
        {"key": "MAX_ENTRY_PRICE", "value": str(int(config.MAX_ENTRY_PRICE * 100)) + "c", "desc": "Skip bets above this price"},
        {"key": "MAX_ENTRY_PRICE_MAP", "value": config.MAX_ENTRY_PRICE_MAP or "default", "desc": "Per-trader max entry price override"},
        {"key": "MAX_COPIES_PER_MARKET", "value": str(config.MAX_COPIES_PER_MARKET), "desc": "Max copies per market"},
        {"key": "MAX_PER_EVENT", "value": _dlr(config.MAX_PER_EVENT), "desc": "Max $ per event/game (0=off)"},
        {"key": "MAX_PER_MATCH", "value": _dlr(config.MAX_PER_MATCH), "desc": "Max $ per match (Map1+Map2+BO3 grouped)"},
        {"key": "MAX_SPREAD", "value": _pct(config.MAX_SPREAD), "desc": "Max bid/ask spread"},
        {"key": "ENTRY_TRADE_SEC", "value": _sec(config.ENTRY_TRADE_SEC), "desc": "Max trade age to copy"},
        {"key": "NO_REBUY_MINUTES", "value": str(config.NO_REBUY_MINUTES) + " min", "desc": "Block re-entry after close (0=off). Also sets MAX_COPIES lookback window"},
        {"key": "CATEGORY_BLACKLIST_MAP", "value": config.CATEGORY_BLACKLIST_MAP or "none", "desc": "Per-trader blocked categories (e.g. sovereign2013:tennis|mlb)"},
        {"key": "MAX_HOURS_BEFORE_EVENT", "value": str(config.MAX_HOURS_BEFORE_EVENT) + "h", "desc": "Queue if event > Xh away (0=off)"},
        {"key": "EVENT_WAIT_MIN_CASH", "value": _dlr(config.EVENT_WAIT_MIN_CASH) if config.EVENT_WAIT_MIN_CASH > 0 else "always queue", "desc": "Only queue when cash < $X (0=always)"},
        {"key": "QUEUE_DRIFT", "value": "<20c:%d%% 20-40c:%d%% 40-60c:%d%% 60c+:%d%%" % (config.QUEUE_DRIFT_LOTTERY*100, config.QUEUE_DRIFT_UNDERDOG*100, config.QUEUE_DRIFT_COINFLIP*100, config.QUEUE_DRIFT_FAVORITE*100), "desc": "Max price drift for queued trades (per range)"},
        # --- Entry Mechanics ---
        {"key": "ENTRY_SLIPPAGE", "value": str(config.ENTRY_SLIPPAGE), "desc": "Added to entry price"},
        {"key": "MAX_ENTRY_PRICE_CAP", "value": str(int(config.MAX_ENTRY_PRICE_CAP * 100)) + "c", "desc": "Hard ceiling after slippage"},
        {"key": "TRADE_SEC_FROM_RESOLVE", "value": _sec(config.TRADE_SEC_FROM_RESOLVE), "desc": "Stop buying before market close"},
        # --- Hedge Detection ---
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
        {"key": "TAKE_PROFIT_MAP", "value": config.TAKE_PROFIT_MAP or "default", "desc": "Per-trader TP override (0=disabled for that trader)"},
        # --- Auto-Sell / Auto-Close ---
        {"key": "AUTO_SELL_PRICE", "value": str(int(config.AUTO_SELL_PRICE * 100)) + "c", "desc": "Sell winning positions above this price"},
        {"key": "AUTO_CLOSE_WON_PRICE", "value": str(int(config.AUTO_CLOSE_WON_PRICE * 100)) + "c", "desc": "Mark as won above this price"},
        {"key": "AUTO_CLOSE_LOST_PRICE", "value": str(int(config.AUTO_CLOSE_LOST_PRICE * 100)) + "c", "desc": "Mark as lost below this price"},
        # --- Feature Toggles ---
        {"key": "COPY_SELLS", "value": _onoff(config.COPY_SELLS), "desc": "Copy sell signals from traders"},
        {"key": "POSITION_DIFF_ENABLED", "value": _onoff(config.POSITION_DIFF_ENABLED), "desc": "Position-diff fallback scan"},
        # --- Circuit Breaker ---
        {"key": "CB_THRESHOLD", "value": str(config.CB_THRESHOLD) + " failures", "desc": "API failures before pause"},
        {"key": "CB_PAUSE_SECS", "value": _sec(config.CB_PAUSE_SECS), "desc": "Pause duration after breaker trips"},
        # --- Order Execution ---
        {"key": "BUY_SLIPPAGE_LEVELS", "value": config.BUY_SLIPPAGE_LEVELS, "desc": "Buy retry slippage steps"},
        {"key": "SELL_SLIPPAGE_LEVELS", "value": config.SELL_SLIPPAGE_LEVELS, "desc": "Sell retry slippage steps"},
        {"key": "DELAYED_BUY_VERIFY_SECS", "value": _sec(config.DELAYED_BUY_VERIFY_SECS), "desc": "Verify delayed buy orders"},
        {"key": "DELAYED_SELL_VERIFY_SECS", "value": _sec(config.DELAYED_SELL_VERIFY_SECS), "desc": "Verify delayed sell orders"},
        {"key": "SELL_VERIFY_THRESHOLD", "value": str(config.SELL_VERIFY_THRESHOLD), "desc": "Max remaining shares fraction (0.05 = 95%+ must be sold)"},
        # --- Fill Verification ---
        {"key": "FILL_VERIFY_DELAY_SECS", "value": _sec(config.FILL_VERIFY_DELAY_SECS), "desc": "Delay before checking fill amount"},
        {"key": "MIN_FILL_AMOUNT", "value": _dlr(config.MIN_FILL_AMOUNT), "desc": "Min USDC change for valid fill"},
        # --- Trailing Stop ---
        {"key": "TRAILING_STOP_ENABLED", "value": _onoff(config.TRAILING_STOP_ENABLED), "desc": "Sell when position drops from peak"},
        {"key": "TRAILING_STOP_MARGIN", "value": str(int(config.TRAILING_STOP_MARGIN * 100)) + "c", "desc": "Sell when price drops Xc below peak"},
        {"key": "TRAILING_STOP_ACTIVATE", "value": _pct(config.TRAILING_STOP_ACTIVATE), "desc": "Only activate after X% gain over entry"},
        # --- Position Tracking ---
        {"key": "MISS_COUNT_TO_CLOSE", "value": str(config.MISS_COUNT_TO_CLOSE) if config.MISS_COUNT_TO_CLOSE > 0 else "OFF", "desc": "Close stale positions after N misses"},
        {"key": "EVENT_WAIT_MAX_SECS", "value": str(config.EVENT_WAIT_MAX_SECS // 3600) + "h", "desc": "Max queued trade age"},
        # --- AI Analysis ---
        {"key": "AI_ANALYZER", "value": "ON" if config.ANTHROPIC_API_KEY else "OFF (no key)", "desc": "Claude AI parameter optimization (every 6h)"},
        {"key": "BLOCKED_TRADE_LOGGING", "value": "ON", "desc": "Log all filtered trades for outcome analysis"},
        {"key": "OUTCOME_TRACKER", "value": "ON (every 30min)", "desc": "Check what blocked trades would have earned"},
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
    """Per-Trader P&L breakdown — shows all-time + last 24h stats.

    ?hours=24 (default) filters closed trades to last N hours.
    ?hours=0 returns all-time stats.
    """
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    hours = request.args.get("hours", "24")
    try:
        hours = int(hours)
    except ValueError:
        hours = 24

    all_trades = [dict(t) for t in db.get_all_copy_trades(limit=5000)]
    trader_map = {}

    # Pre-populate with ALL followed wallets (including auto-promoted with 0 trades)
    try:
        followed = db.get_followed_wallets()
        for w in followed:
            addr = w["address"]
            trader_map[addr] = {
                "username": w["username"] or addr[:12],
                "address": addr,
                "open": 0, "closed": 0, "wins": 0, "losses": 0,
                "pnl_realized": 0.0, "pnl_unrealized": 0.0,
                "total_invested": 0.0,
                "all_closed": 0, "all_pnl": 0.0, "all_wins": 0, "all_losses": 0,
            }
    except Exception:
        pass

    for t in all_trades:
        addr = t["wallet_address"]
        if addr not in trader_map:
            trader_map[addr] = {
                "username": t["wallet_username"] or addr[:12],
                "address": addr,
                "open": 0, "closed": 0, "wins": 0, "losses": 0,
                "pnl_realized": 0.0, "pnl_unrealized": 0.0,
                "total_invested": 0.0,
                # All-time stats (always shown)
                "all_closed": 0, "all_pnl": 0.0, "all_wins": 0, "all_losses": 0,
            }
        s = trader_map[addr]
        if t["status"] == "open":
            s["open"] += 1
            s["pnl_unrealized"] += (t["pnl_unrealized"] or 0)
            s["total_invested"] += t["size"]
        elif t["status"] == "closed":
            pnl = t["pnl_realized"] or 0
            # All-time always counted
            s["all_closed"] += 1
            s["all_pnl"] += pnl
            if pnl > 0:
                s["all_wins"] += 1
            elif pnl < 0:
                s["all_losses"] += 1
            # Period-filtered (24h default)
            in_period = True
            if hours > 0 and t.get("closed_at"):
                try:
                    from datetime import datetime as _dt, timedelta as _td
                    closed_dt = _dt.strptime(t["closed_at"][:19], "%Y-%m-%d %H:%M:%S")
                    in_period = (_dt.now() - closed_dt) < _td(hours=hours)
                except Exception:
                    in_period = True
            if in_period:
                s["closed"] += 1
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
        s["all_pnl"] = round(s["all_pnl"], 2)
        all_total = s["all_wins"] + s["all_losses"]
        s["all_win_rate"] = round(s["all_wins"] / all_total * 100, 1) if all_total > 0 else 0
        s["period_hours"] = hours
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
    sell_resp = None
    if config.LIVE_MODE and cid:
        sell_resp = sell_shares(cid, side, current_price)
        sell_ok = sell_resp is not None
        if not sell_ok:
            return jsonify({"error": "Sell order failed", "trade_id": trade_id}), 500
    else:
        sell_ok = True  # paper mode

    # Calculate PnL using best available entry price
    _ep = trade.get("actual_entry_price") or entry_price
    _sz = trade.get("actual_size") or trade["size"]
    shares = _sz / _ep if _ep > 0 else 0
    pnl = round((current_price - _ep) * shares, 2)

    db.close_copy_trade(trade_id, pnl, close_price=current_price)

    # Correct P&L with actual USDC received from sell
    if sell_resp and sell_resp.get("usdc_received", 0) > 0:
        real_pnl = round(sell_resp["usdc_received"] - _sz, 2)
        db.update_closed_trade_pnl(trade_id, real_pnl, sell_resp["usdc_received"])
        pnl = real_pnl
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
            "size": trade.get("size", 0),
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


@app.route("/api/orphan/sell", methods=["POST"])
def api_sell_orphan():
    """Sell an orphan position (not tracked by bot)."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403

    data = request.get_json() or {}
    cid = data.get("condition_id", "")
    side = data.get("side", "")
    price = float(data.get("price", 0))

    if not cid or not side or price <= 0:
        return jsonify({"error": "Missing condition_id, side, or price"}), 400

    from bot.order_executor import sell_shares
    result = sell_shares(cid, side, price)
    if result:
        logger.info("[ORPHAN-SELL] %s / %s @ %.0fc | received $%.2f",
                     cid[:20], side, price * 100, result.get("usdc_received", 0))
        return jsonify({"status": "sold", "usdc_received": result.get("usdc_received", 0)})
    else:
        return jsonify({"error": "Sell failed — no liquidity or orderbook missing"}), 500


@app.route("/api/copy/scan", methods=["POST"])
def api_copy_scan():
    """Manually trigger copy-trade scan of followed wallets."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
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
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
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
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
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


@app.route("/api/equity-curve")
def api_equity_curve():
    """Eigener Equity-Curve Endpoint — berechnet aus copy_trades DB."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    curve = db.get_equity_curve()
    return jsonify({
        "labels": [p["date"] for p in curve],
        "values": [p["value"] for p in curve],
    })




@app.route("/api/brain/decisions")
def api_brain_decisions():
    """Recent brain engine decisions."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    limit = request.args.get("limit", 50, type=int)
    decisions = db.get_brain_decisions(limit)
    return jsonify(decisions)

@app.route("/api/brain/scores")
def api_brain_scores():
    """Trade score distribution and performance."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    perf = db.get_score_range_performance()
    return jsonify(perf)

@app.route("/api/brain/lifecycle")
def api_brain_lifecycle():
    """All traders in the lifecycle pipeline."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    result = {}
    for status in ["DISCOVERED", "OBSERVING", "PAPER_FOLLOW", "LIVE_FOLLOW", "PAUSED", "KICKED"]:
        result[status] = db.get_lifecycle_traders_by_status(status)
    return jsonify(result)

# --- PandaScore Stream Cache (avoid hammering API) ---
_stream_cache = {}  # {cache_key: {"url": str, "ts": float}}
_STREAM_CACHE_TTL = 120  # 2 minutes

_PANDASCORE_GAMES = {
    "cs": "csgo", "counter-strike": "csgo", "cs2": "csgo",
    "lol": "lol", "league of legends": "lol",
    "dota": "dota2", "dota 2": "dota2",
    "valorant": "valorant",
}


def _find_stream(market_question: str) -> dict:
    """Find livestream URL for an esports market via PandaScore API."""
    import re
    if not config.PANDASCORE_API_KEY:
        return {"url": "", "source": "no_key"}

    q = market_question.lower()

    # Detect game
    game_slug = ""
    for kw, slug in _PANDASCORE_GAMES.items():
        if kw in q:
            game_slug = slug
            break
    if not game_slug:
        # Fallback: Twitch search
        return {"url": "https://www.twitch.tv/search?term=" + market_question.replace(" ", "+"),
                "source": "twitch_search"}

    # Extract team names from market question
    # Patterns: "CS: TeamA vs TeamB - Map 1", "LoL: TeamA vs TeamB (BO3)"
    m = re.search(r':\s*(.+?)\s+vs\s+(.+?)(?:\s*[-–(]|$)', q)
    if not m:
        m = re.search(r'(.+?)\s+vs\s+(.+?)(?:\s*[-–(]|$)', q)
    if not m:
        return {"url": "https://www.twitch.tv/search?term=" + market_question.replace(" ", "+"),
                "source": "twitch_search"}

    team1 = m.group(1).strip().lower()
    team2 = m.group(2).strip().lower()

    # Check cache
    cache_key = f"{game_slug}:{team1}:{team2}"
    cached = _stream_cache.get(cache_key)
    if cached and (time.time() - cached["ts"]) < _STREAM_CACHE_TTL:
        return cached["data"]

    # Fetch running + upcoming matches from PandaScore
    try:
        import requests as _rq
        matches = []
        for status in ["running", "upcoming"]:
            r = _rq.get(f"https://api.pandascore.co/{game_slug}/matches/{status}",
                        params={"token": config.PANDASCORE_API_KEY, "per_page": 50},
                        timeout=5)
            if r.ok:
                matches.extend(r.json())
            if status == "running" and matches:
                break  # running matches found, skip upcoming

        # Match teams
        best_match = None
        best_score = 0
        for match in matches:
            opponents = match.get("opponents", [])
            if len(opponents) < 2:
                continue
            names = []
            for opp in opponents:
                o = opp.get("opponent", {})
                names.extend([(o.get("name") or "").lower(), (o.get("acronym") or "").lower(), (o.get("slug") or "").lower()])

            # Score: how many of our team name words match
            score = 0
            for team in [team1, team2]:
                team_words = team.split()
                for name in names:
                    if team in name or name in team:
                        score += 10  # exact/substring match
                    elif any(w in name for w in team_words if len(w) > 2):
                        score += 3   # word match

            if score > best_score:
                best_score = score
                best_match = match

        if best_match and best_score >= 10:
            streams = best_match.get("streams_list", [])
            # Prefer: English official stream > any official > any stream
            stream_url = ""
            for pref in [
                lambda s: s.get("official") and s.get("language") == "en",
                lambda s: s.get("main"),
                lambda s: s.get("official"),
                lambda s: s.get("raw_url"),
            ]:
                for s in streams:
                    if pref(s) and s.get("raw_url"):
                        stream_url = s["raw_url"]
                        break
                if stream_url:
                    break

            if not stream_url and streams:
                stream_url = streams[0].get("raw_url", "")

            teams_found = " vs ".join(o["opponent"]["name"] for o in best_match.get("opponents", []))
            result = {
                "url": stream_url,
                "source": "pandascore",
                "match": teams_found,
                "status": best_match.get("status", ""),
                "begin_at": best_match.get("begin_at", ""),
            }
            if not stream_url:
                # Match found but no stream → Twitch search
                result["url"] = "https://www.twitch.tv/search?term=" + teams_found.replace(" ", "+")
                result["source"] = "twitch_search"

            _stream_cache[cache_key] = {"data": result, "ts": time.time()}
            return result

    except Exception as e:
        logger.debug("PandaScore error: %s", e)

    # Fallback: Twitch search
    fallback = {"url": "https://www.twitch.tv/search?term=" + market_question.replace(" ", "+"),
                "source": "twitch_search"}
    _stream_cache[cache_key] = {"data": fallback, "ts": time.time()}
    return fallback


@app.route("/api/stream/find")
def api_stream_find():
    """Find livestream URL for a market question."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    question = request.args.get("q", "")
    if not question:
        return jsonify({"error": "missing q parameter"}), 400
    result = _find_stream(question)
    return jsonify(result)


@app.route("/api/copy/history")
def api_copy_history():
    """Return positions, chart data, and stats filtered by period."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
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


# --- AI Analysis Endpoints ---

@app.route("/api/ai/blocked-stats")
def api_blocked_stats():
    """Get blocked trade statistics."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    hours = int(request.args.get("hours", 48))
    stats = db.get_blocked_trade_stats(hours=hours)
    return jsonify(stats)


@app.route("/api/ai/blocked-trades")
def api_blocked_trades():
    """Get recent blocked trades."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    hours = int(request.args.get("hours", 48))
    limit = int(request.args.get("limit", 200))
    trades = db.get_blocked_trades_since(hours=hours, limit=limit)
    return jsonify(trades)


@app.route("/api/ai/recommendations")
def api_ai_recommendations():
    """Get AI recommendations."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    limit = int(request.args.get("limit", 5))
    recs = db.get_recommendations(limit=limit)
    return jsonify(recs)


@app.route("/api/ai/latest")
def api_ai_latest():
    """Get latest AI recommendation."""
    rec = db.get_latest_recommendation()
    if rec:
        try:
            rec["recommendations"] = json.loads(rec.get("recommendations_json", "[]"))
        except Exception:
            rec["recommendations"] = []
    return jsonify(rec or {})


@app.route("/api/ai/analyze", methods=["POST"])
def api_ai_analyze():
    """Trigger AI analysis manually."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401
    if not config.ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY nicht gesetzt — trage ihn in secrets.env ein"}), 400
    try:
        from bot.ai_analyzer import analyze_and_recommend
        result = analyze_and_recommend(hours=48)
        return jsonify(result)
    except Exception as e:
        logger.error("AI analyze error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/ai/recommendation/<int:rec_id>/apply", methods=["POST"])
def api_ai_apply(rec_id):
    """Mark a recommendation as applied."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401
    db.update_recommendation_status(rec_id, "applied")
    return jsonify({"ok": True})


@app.route("/api/ai/recommendation/<int:rec_id>/dismiss", methods=["POST"])
def api_ai_dismiss(rec_id):
    """Mark a recommendation as dismissed."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401
    db.update_recommendation_status(rec_id, "dismissed")
    return jsonify({"ok": True})


# =====================================================================
# UPGRADE: Performance, ML, Discovery, Router, Autonomous Endpoints
# =====================================================================

@app.route("/api/upgrade/trader-performance")
def api_trader_performance():
    """Performance aller Trader mit Status."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    with db.get_connection() as conn:
        perf = conn.execute(
            "SELECT tp.*, ts.status as trader_status, ts.bet_multiplier, ts.reason "
            "FROM trader_performance tp "
            "LEFT JOIN trader_status ts ON tp.trader_name = ts.trader_name "
            "WHERE tp.period = '7d' AND tp.trader_name != 'imported' AND tp.trader_name != 'test' AND tp.trades_count > 0 ORDER BY tp.total_pnl DESC"
        ).fetchall()
    return jsonify({"traders": [dict(r) for r in perf]})


@app.route("/api/upgrade/category-heatmap")
def api_category_heatmap():
    """Kategorie-Performance als Heatmap-Daten."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    with db.get_connection() as conn:
        cats = conn.execute(
            "SELECT * FROM category_performance WHERE period = '30d' "
            "ORDER BY total_pnl DESC"
        ).fetchall()
    try:
        from bot.smart_router import _load_allocations
        allocs = _load_allocations()
    except Exception:
        allocs = {}
    result = []
    for c in cats:
        d = dict(c)
        d["allocation"] = allocs.get(c["category"], 0.10)
        result.append(d)
    return jsonify({"categories": result})


@app.route("/api/upgrade/ml-info")
def api_ml_info():
    """ML-Modell Info und Training-History."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    with db.get_connection() as conn:
        training = conn.execute(
            "SELECT * FROM ml_training_log ORDER BY trained_at DESC LIMIT 5"
        ).fetchall()
    return jsonify({"training_history": [dict(r) for r in training]})


@app.route("/api/upgrade/candidates")
def api_candidates():
    """Trader-Kandidaten mit Paper-Stats."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    candidates = db.get_all_candidates()
    for c in candidates:
        stats = db.get_candidate_stats(c["address"])
        c.update(stats)
    return jsonify({"candidates": candidates})


@app.route("/api/upgrade/autonomous-trades")
def api_autonomous_trades():
    """Autonome Trades (Paper + Live)."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    with db.get_connection() as conn:
        trades = conn.execute(
            "SELECT * FROM autonomous_trades ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    return jsonify({"trades": [dict(r) for r in trades]})


@app.route("/api/upgrade/status")
def api_upgrade_status():
    """Overall upgrade status — alles auf einen Blick."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    result = {}

    # Trader status
    with db.get_connection() as conn:
        traders = conn.execute("SELECT * FROM trader_status").fetchall()
        result["trader_status"] = [dict(r) for r in traders]

    # ML model
    with db.get_connection() as conn:
        ml = conn.execute(
            "SELECT * FROM ml_training_log ORDER BY trained_at DESC LIMIT 1"
        ).fetchone()
        result["ml_model"] = dict(ml) if ml else None

    # Candidates count
    with db.get_connection() as conn:
        cand = conn.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN status='observing' THEN 1 ELSE 0 END) as observing, "
            "SUM(CASE WHEN status='promoted' THEN 1 ELSE 0 END) as promoted "
            "FROM trader_candidates"
        ).fetchone()
        result["candidates"] = dict(cand) if cand else {"total": 0}

    # Autonomous trades
    with db.get_connection() as conn:
        auto = conn.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) as open_count "
            "FROM autonomous_trades"
        ).fetchone()
        result["autonomous"] = dict(auto) if auto else {"total": 0}

    # Category allocations
    try:
        from bot.smart_router import _load_allocations
        result["allocations"] = _load_allocations()
    except Exception:
        result["allocations"] = {}

    return jsonify(result)


@app.route("/brain")
def brain_dashboard():
    """Intelligence Dashboard — ML, Performance, Discovery, Router."""
    return render_template("brain.html")


# =====================================================================
# FUN FEATURES: Trash Talk, Trader Cards, Ticker, Konfetti
# =====================================================================

import random as _random

_TRASH_TALK_WINS = [
    "hat mal wieder geliefert! Druckmaschine geht BRRRR",
    "zeigt wie man's macht. Easy money.",
    "casht ein wie ein Boss. Respekt!",
    "hat den Markt gelesen wie ein offenes Buch.",
    "macht Polymarket zu seinem persoenlichen Geldautomaten.",
    "auf Feuer! Alles was er anfasst wird Gold.",
    "kennt offensichtlich die Zukunft. Zeitreisender?",
    "der absolute GOAT. Kein Wunder dass wir den kopieren.",
]

_TRASH_TALK_LOSSES = [
    "hat wieder daneben gegriffen. Classic.",
    "versenkt unser Geld. Danke fuer nichts!",
    "sollte vielleicht eine Muenze werfen stattdessen.",
    "Blindfold-Trading waere profitabler.",
    "das Geld haette man auch verbrennen koennen.",
    "macht einen auf Experte, liefert wie ein Anfaenger.",
    "hat offensichtlich den Wetterbericht mit Sportergebnissen verwechselt.",
    "RIP unsere USDC. Gone but not forgotten.",
]

_TRASH_TALK_PAUSED = [
    "wurde auf die Bank gesetzt. Ab in die Ecke und schaemen!",
    "darf erstmal zugucken. Lern was, Bruder.",
    "ist gesperrt. Der Bot hat genug von deinen Verlusten.",
    "sitzt auf der Strafbank. Rote Karte!",
]

_TRADER_TITLES = {
    "sovereign2013": "The Sovereign",
    "KING7777777": "The King",
    "xsaghav": "The Wildcard",
    "RN1": "Random Number One",
    "Jargs": "The Ghost",
    "fsavhlc": "The Rookie",
}

_TRADER_SPECIALS = {
    "sovereign2013": "NBA Oracle — sieht die Zukunft",
    "KING7777777": "CS2 Sniper — headshots only",
    "xsaghav": "Volume King — tradet wie verueckt",
    "RN1": "Spray & Pray — quantity over quality",
    "Jargs": "Silent Assassin — selten aber praezise",
    "fsavhlc": "Fresh Blood — noch in der Ausbildung",
}


@app.route("/api/fun/trash-talk")
def api_trash_talk():
    """Generate AI trash talk for recent trades."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    talks = []
    with db.get_connection() as conn:
        # Last 10 closed trades
        rows = conn.execute(
            "SELECT wallet_username, market_question, pnl_realized, closed_at "
            "FROM copy_trades WHERE status = 'closed' AND pnl_realized IS NOT NULL "
            "ORDER BY closed_at DESC LIMIT 10"
        ).fetchall()

    for r in rows:
        trader = r["wallet_username"] or "Unknown"
        pnl = r["pnl_realized"] or 0
        market = (r["market_question"] or "")[:50]
        if pnl > 0:
            talk = _random.choice(_TRASH_TALK_WINS)
        else:
            talk = _random.choice(_TRASH_TALK_LOSSES)
        talks.append({
            "trader": trader,
            "market": market,
            "pnl": round(pnl, 2),
            "talk": "%s %s" % (trader, talk),
            "time": r["closed_at"] or "",
        })

    # Add paused trader trash talk
    with db.get_connection() as conn:
        paused = conn.execute(
            "SELECT trader_name FROM trader_status WHERE status = 'paused'"
        ).fetchall()
    for p in paused:
        talks.insert(0, {
            "trader": p["trader_name"],
            "market": "",
            "pnl": 0,
            "talk": "%s %s" % (p["trader_name"], _random.choice(_TRASH_TALK_PAUSED)),
            "time": "",
        })

    return jsonify({"talks": talks})


@app.route("/api/fun/trader-cards")
def api_trader_cards():
    """Trader trading card data with stats, titles, specials."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    cards = []
    with db.get_connection() as conn:
        traders = conn.execute(
            "SELECT wallet_username, COUNT(*) as total, "
            "SUM(CASE WHEN pnl_realized > 0 THEN 1 ELSE 0 END) as wins, "
            "SUM(CASE WHEN pnl_realized < 0 THEN 1 ELSE 0 END) as losses, "
            "ROUND(SUM(pnl_realized), 2) as total_pnl, "
            "ROUND(MAX(pnl_realized), 2) as best_trade, "
            "ROUND(MIN(pnl_realized), 2) as worst_trade "
            "FROM copy_trades WHERE status = 'closed' AND wallet_username != '' "
            "GROUP BY wallet_username ORDER BY SUM(pnl_realized) DESC"
        ).fetchall()
        # Pre-fetch all trader statuses while conn is open
        _status_map = {}
        for _sr in conn.execute("SELECT trader_name, status FROM trader_status").fetchall():
            _status_map[_sr['trader_name']] = _sr['status']

    for t in traders:
        name = t["wallet_username"]
        total = t["total"] or 0
        wins = t["wins"] or 0
        pnl = t["total_pnl"] or 0
        wr = round(wins / total * 100, 1) if total > 0 else 0

        # Rarity based on P&L
        if pnl >= 20:
            rarity = "legendary"
        elif pnl >= 5:
            rarity = "epic"
        elif pnl >= 0:
            rarity = "rare"
        else:
            rarity = "common"

        # Power level
        power = max(0, round((wr * 2) + (pnl * 5) + (total * 0.5)))

        # Status
        status = _status_map.get(name, 'active')

        cards.append({
            "name": name,
            "title": _TRADER_TITLES.get(name, "The Trader"),
            "special": _TRADER_SPECIALS.get(name, "Copy-Trading Pro"),
            "rarity": rarity,
            "power": power,
            "total_trades": total,
            "wins": wins,
            "losses": t["losses"] or 0,
            "winrate": wr,
            "total_pnl": pnl,
            "best_trade": t["best_trade"] or 0,
            "worst_trade": t["worst_trade"] or 0,
            "status": status,
        })

    return jsonify({"cards": cards})


@app.route("/api/fun/ticker")
def api_ticker():
    """Live ticker tape data — last 20 events."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    events = []
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT event_type, title, detail, pnl, created_at "
            "FROM activity_log ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    for r in rows:
        events.append({
            "type": r["event_type"],
            "title": r["title"],
            "detail": r["detail"],
            "pnl": r["pnl"] or 0,
            "time": r["created_at"] or "",
        })
    return jsonify({"events": events})


@app.route("/api/fun/daily-pnl")
def api_daily_pnl():
    """Daily P&L for konfetti check + calendar heatmap."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT DATE(closed_at) as day, ROUND(SUM(pnl_realized), 2) as pnl, COUNT(*) as trades "
            "FROM copy_trades WHERE status = 'closed' AND closed_at IS NOT NULL "
            "GROUP BY DATE(closed_at) ORDER BY day DESC LIMIT 30"
        ).fetchall()
    days = [{"day": r["day"], "pnl": r["pnl"] or 0, "trades": r["trades"] or 0} for r in rows]
    today_pnl = days[0]["pnl"] if days and days[0]["day"] else 0
    return jsonify({"days": days, "today_pnl": today_pnl, "konfetti": today_pnl > 0})


@app.route("/api/upgrade/clv")
def api_clv():
    """CLV tracking stats."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    from bot.clv_tracker import get_clv_by_trader, update_clv_for_closed_trades
    try:
        overall = update_clv_for_closed_trades()
        by_trader = get_clv_by_trader()
        return jsonify({"overall": overall, "by_trader": by_trader})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/gazette")
def reports_gazette():
    """SSC Trading Gazette — daily reports, password protected."""
    return render_template("reports.html")


@app.route("/api/fun/daily-reports")
def api_daily_reports():
    """Latest daily reports for gazette."""
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 403
    import json as _json
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT id, report_text, data_snapshot, created_at FROM ai_reports "
            "WHERE LENGTH(report_text) < 5000 "
            "ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
    reports = []
    for r in rows:
        data = {}
        try:
            data = _json.loads(r["data_snapshot"] or "{}")
        except Exception:
            pass
        if data.get("type") == "daily_auto":
            reports.append({
                "id": r["id"],
                "text": r["report_text"],
                "date": r["created_at"],
                "pnl": data.get("pnl", 0),
                "trades": data.get("trades", 0),
            })
    return jsonify({"reports": reports})
