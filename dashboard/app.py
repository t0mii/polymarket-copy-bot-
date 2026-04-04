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


@app.route("/api/live-data")
def api_live_data():
    """Dashboard data — reads directly from Polymarket API for accuracy."""
    import requests as _req

    DEPOSIT = float(config.STARTING_BALANCE)
    funder = config.POLYMARKET_FUNDER
    DATA_API = "https://data-api.polymarket.com"
    _sport_map = {"mlb": "MLB", "nba": "NBA", "nhl": "NHL", "nfl": "NFL",
                  "ufc": "UFC", "mma": "MMA", "atp": "ATP", "wta": "WTA",
                  "soccer": "SOC", "liga": "SOC", "epl": "SOC", "ucl": "SOC",
                  "lol": "LOL", "csgo": "CS", "ncaa": "NCAA"}

    # Real wallet balance
    wallet = 0
    try:
        from bot.order_executor import get_wallet_balance
        wallet = round(get_wallet_balance(), 2)
    except Exception:
        pass

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

            if outcome.lower() in ("yes", "y"): side = "YES"
            elif outcome.lower() in ("no", "n"): side = "NO"
            else: side = outcome or "YES"

            # Detect sport from slug
            _slug = rp.get("slug", "") or ""
            _sport = ""
            for _k, _v in _sport_map.items():
                if _k in _slug.lower():
                    _sport = _v
                    break

            open_positions.append({
                "id": hash(rp.get("conditionId", "")) % 10000,
                "wallet_username": "RN1",
                "wallet_address": funder,
                "market_question": rp.get("title") or rp.get("question", ""),
                "market_slug": rp.get("slug", ""),
                "sport": _sport,
                "event_slug": rp.get("eventSlug", ""),
                "side": side,
                "outcome_label": outcome if side not in ("YES", "NO") else "",
                "entry_price": ap,
                "current_price": cp,
                "size": round(cv, 2),
                "pnl_unrealized": round(cv - float(rp.get("initialValue", 0) or 0), 2),
                "condition_id": rp.get("conditionId", ""),
                "created_at": "",
            })
    except Exception:
        pass

    # Count W/L from positions: 0c = lost, 100c = won, sold via activity = closed
    wins = 0
    losses = 0
    resolved_list = []
    for rp in all_raw:
        cp = float(rp.get("curPrice", 0) or 0)
        cv = float(rp.get("currentValue", 0) or 0)
        iv = float(rp.get("initialValue", 0) or 0)
        cashpnl = float(rp.get("cashPnl", 0) or 0)
        if cp >= 0.99 and iv > 0.01:  # won
            wins += 1
            resolved_list.append({"q": rp.get("title", ""), "pnl": cashpnl, "status": "won"})
        elif cp < 0.01 and iv > 0.01:  # lost
            losses += 1
            resolved_list.append({"q": rp.get("title", ""), "pnl": cashpnl, "status": "lost"})

    # Sells W/L from buy vs sell comparison + build closed_positions list
    sell_wins = 0
    sell_losses = 0
    closed_positions = []
    try:
        buys_r = _req.get(f"{DATA_API}/activity", params={
            "user": funder, "type": "TRADE", "side": "BUY", "limit": 500}, timeout=15)
        sells_r = _req.get(f"{DATA_API}/activity", params={
            "user": funder, "type": "TRADE", "side": "SELL", "limit": 200}, timeout=10)
        buy_data = {}
        for b in (buys_r.json() if buys_r.ok else []):
            cid = b.get("conditionId", "")
            if cid not in buy_data:
                buy_data[cid] = {"cost": 0, "title": b.get("title", ""), "outcome": b.get("outcome", ""),
                                 "slug": b.get("slug", ""), "eventSlug": b.get("eventSlug", ""),
                                 "avg_price": float(b.get("price", 0) or 0)}
            buy_data[cid]["cost"] += float(b.get("usdcSize", 0))
        sell_data = {}
        for s in (sells_r.json() if sells_r.ok else []):
            cid = s.get("conditionId", "")
            if cid not in sell_data:
                sell_data[cid] = {"rev": 0, "price": float(s.get("price", 0) or 0),
                                  "timestamp": s.get("timestamp", 0)}
            sell_data[cid]["rev"] += float(s.get("usdcSize", 0))
        for cid, sv in sell_data.items():
            bv = buy_data.get(cid)
            if not bv or bv["cost"] <= 0:
                continue
            pnl = round(sv["rev"] - bv["cost"], 2)
            if pnl >= 0:
                sell_wins += 1
            else:
                sell_losses += 1
            outcome = bv["outcome"]
            side = outcome if outcome.lower() not in ("yes","no","y","n","") else outcome.upper()[:3] or "?"
            _cs = bv.get("slug", "") or ""
            _csport = ""
            for _ck, _cv2 in _sport_map.items():
                if _ck in _cs.lower():
                    _csport = _cv2
                    break
            closed_positions.append({
                "id": hash(cid) % 10000,
                "wallet_username": "RN1",
                "wallet_address": funder,
                "market_question": bv["title"],
                "side": side,
                "outcome_label": outcome if side not in ("YES","NO") else "",
                "entry_price": bv["avg_price"],
                "current_price": sv["price"],
                "size": round(bv["cost"], 2),
                "pnl_realized": pnl,
                "status": "closed",
                "market_slug": bv.get("slug", ""),
                "event_slug": bv.get("eventSlug", ""),
                "sport": _csport,
                "closed_at": "",
                "created_at": "",
            })
    except Exception:
        pass

    # Add resolved positions (won/lost at 0c/100c)
    for rl in resolved_list:
        closed_positions.append({
            "id": 0, "wallet_username": "RN1",
            "wallet_address": funder,
            "market_question": rl["q"], "side": "", "outcome_label": "",
            "entry_price": 0, "current_price": 1.0 if rl["status"] == "won" else 0,
            "size": abs(rl["pnl"]), "pnl_realized": round(rl["pnl"], 2),
            "status": "closed", "market_slug": "", "event_slug": "",
            "closed_at": "", "created_at": "",
        })

    closed_positions.sort(key=lambda x: abs(x.get("pnl_realized", 0)), reverse=True)
    total_closed = wins + losses + sell_wins + sell_losses
    wins += sell_wins
    losses += sell_losses

    # Polymarket values
    open_value = sum(p["size"] for p in open_positions)
    active_value = sum(p["size"] for p in open_positions if 0.01 < p.get("current_price", 0) < 0.99)
    redeemable_value = sum(p["size"] for p in open_positions if p.get("current_price", 0) >= 0.99)
    total_value = wallet + open_value
    total_pnl = total_value - DEPOSIT
    wr = round(wins / max(wins + losses, 1) * 100, 1)

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
        "open_trades": len(open_positions),
        "closed_trades": total_closed,
        "wins": wins,
        "win_rate": wr,
        "starting_balance": DEPOSIT,
    }

    followed = db.get_followed_wallets()

    return jsonify({
        "summary": summary,
        "starting_balance": DEPOSIT,
        "open_trades": open_positions,
        "closed_trades": closed_positions[:50],
        "followed": [dict(w) for w in followed],
        "trader_stats": [{"username": "RN1",
                          "address": funder,
                          "pnl_realized": 0,
                          "pnl_unrealized": round(total_value - DEPOSIT, 2),
                          "wins": wins, "losses": losses,
                          "open": len(open_positions), "closed": total_closed}],
        "activity": [dict(a) for a in db.get_activity_log(limit=50)],
        "timestamp": int(time.time()),
    })


@app.route("/")
def index():
    """Root redirects to copy trading dashboard."""
    from flask import redirect
    return redirect("/copy")


@app.route("/wallets")
def wallets_page():
    top_wallets = db.get_top_wallets(limit=20)
    followed = db.get_followed_wallets()
    recent_scans = db.get_recent_scans(limit=5)
    rec_stats = db.get_recommendation_stats()
    total_wallets = db.get_wallet_count()

    return render_template(
        "index.html",
        top_wallets=top_wallets,
        followed=followed,
        recent_scans=recent_scans,
        rec_stats=rec_stats,
        total_wallets=total_wallets,
    )


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
    secret = request.args.get("key", "") or ((request.json or {}).get("key", "") if request.is_json else "")
    if secret != os.getenv("DASHBOARD_SECRET", "changeme"):
        return jsonify({"error": "unauthorized"}), 403
    db.toggle_follow(address, 1)
    return jsonify({"status": "ok", "followed": True})


@app.route("/api/wallet/<address>/unfollow", methods=["POST"])
def api_unfollow(address):
    secret = request.args.get("key", "") or ((request.json or {}).get("key", "") if request.is_json else "")
    if secret != os.getenv("DASHBOARD_SECRET", "changeme"):
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


# --- Copy Trading ---

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
        "copy_trading.html",
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
    """Update prices for open copy trades."""
    import threading
    from bot.copy_trader import update_copy_positions

    thread = threading.Thread(target=update_copy_positions, daemon=True)
    thread.start()
    return jsonify({"status": "update_started"})


@app.route("/api/copy/reset", methods=["POST"])
def api_copy_reset():
    """Reset copy trading: delete all trades, baselines, snapshots. Keep followed wallets."""
    confirm = request.args.get("confirm", "")
    if not confirm and request.is_json:
        confirm = (request.json or {}).get("confirm", "")
    if confirm != "RESET":
        return jsonify({"error": "Pass ?confirm=RESET to confirm"}), 400
    db.reset_copy_trading()
    return jsonify({"status": "reset_complete"})


@app.route("/api/copy/chart")
def api_copy_chart():
    """Portfolio chart data for copy trading."""
    snapshots = db.get_copy_portfolio_snapshots(limit=168)
    snapshots = list(reversed(snapshots))
    return jsonify({
        "labels": [s["created_at"] for s in snapshots],
        "values": [round(s["pnl_total"], 2) for s in snapshots],
    })


@app.route("/copy/history")
def copy_history():
    return render_template("copy_history.html")


@app.route("/api/copy/history")
def api_copy_history():
    """Return copy trades, chart data, and stats filtered by period."""
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
