"""Smart Report Generator — analyzes bot performance without external AI."""
import json
import logging
import time
from datetime import datetime, timedelta

import requests

import config
from database import db

logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"


def _gather_data() -> dict:
    """Collect all data for the report."""
    funder = config.POLYMARKET_FUNDER

    wallet = 0
    try:
        from bot.order_executor import get_wallet_balance
        wallet = round(get_wallet_balance(), 2)
    except Exception:
        pass

    # Positions
    positions = []
    try:
        r = requests.get(f"{DATA_API}/positions", params={
            "user": funder, "limit": 500, "sizeThreshold": 0
        }, timeout=15)
        if r.ok:
            for p in r.json():
                cv = float(p.get("currentValue", 0) or 0)
                iv = float(p.get("initialValue", 0) or 0)
                cp = float(p.get("curPrice", 0) or 0)
                if cv < 0.05 and cp < 0.01:
                    continue
                positions.append({
                    "market": (p.get("title") or "")[:50],
                    "side": p.get("outcome", ""),
                    "entry": float(p.get("avgPrice", 0) or 0),
                    "current": cp,
                    "value": round(cv, 2),
                    "cost": round(iv, 2),
                    "pnl": round(cv - iv, 2),
                    "slug": p.get("slug", ""),
                })
    except Exception:
        pass

    # Recent sells
    closed = []
    try:
        buys_r = requests.get(f"{DATA_API}/activity", params={
            "user": funder, "type": "TRADE", "side": "BUY", "limit": 300
        }, timeout=15)
        sells_r = requests.get(f"{DATA_API}/activity", params={
            "user": funder, "type": "TRADE", "side": "SELL", "limit": 200
        }, timeout=15)

        buy_cost = {}
        buy_info = {}
        for b in (buys_r.json() if buys_r.ok else []):
            cid = b.get("conditionId", "")
            buy_cost[cid] = buy_cost.get(cid, 0) + float(b.get("usdcSize", 0))
            if cid not in buy_info:
                buy_info[cid] = {"title": b.get("title", ""), "price": float(b.get("price", 0) or 0)}

        sell_by_cid = {}
        for s in (sells_r.json() if sells_r.ok else []):
            cid = s.get("conditionId", "")
            sell_by_cid[cid] = sell_by_cid.get(cid, 0) + float(s.get("usdcSize", 0))

        for cid, rev in sell_by_cid.items():
            cost = buy_cost.get(cid, 0)
            if cost <= 0:
                continue
            closed.append({
                "market": (buy_info.get(cid, {}).get("title", "") or "")[:50],
                "cost": round(cost, 2),
                "revenue": round(rev, 2),
                "pnl": round(rev - cost, 2),
            })
    except Exception:
        pass

    # Trader leaderboard
    trader_stats = []
    followed = db.get_followed_wallets()
    for w in followed:
        ts = {"name": w["username"], "day": 0, "week": 0}
        for period, key in [("DAY", "day"), ("WEEK", "week")]:
            try:
                lr = requests.get(f"{DATA_API}/v1/leaderboard", params={
                    "user": w["address"], "timePeriod": period
                }, timeout=5)
                if lr.ok and lr.json():
                    ts[key] = round(float(lr.json()[0].get("pnl", 0)), 2)
            except Exception:
                pass
        trader_stats.append(ts)

    # Per-trader copy performance + open positions from DB
    # Exclude imported/wallet positions — they are not bot-copied trades
    copy_perf = []
    trader_positions = {}  # address -> list of open positions
    try:
        from database.db import get_connection
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT wallet_username, wallet_address, "
                "COUNT(*) as total, "
                "SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) as open_ct, "
                "SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) as closed_ct, "
                "SUM(CASE WHEN status='closed' AND pnl_realized > 0 THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN status='closed' AND pnl_realized < 0 THEN 1 ELSE 0 END) as losses, "
                "ROUND(SUM(CASE WHEN status='closed' THEN COALESCE(pnl_realized,0) ELSE 0 END), 2) as realized, "
                "ROUND(SUM(CASE WHEN status='open' THEN COALESCE(pnl_unrealized,0) ELSE 0 END), 2) as unrealized "
                "FROM copy_trades WHERE wallet_username NOT IN ('(manual)', '(wallet)') "
                "GROUP BY wallet_address"
            ).fetchall()
            for r in rows:
                copy_perf.append({
                    "name": r["wallet_username"],
                    "address": r["wallet_address"],
                    "open": r["open_ct"] or 0,
                    "closed": r["closed_ct"] or 0,
                    "wins": r["wins"] or 0,
                    "losses": r["losses"] or 0,
                    "realized": r["realized"] or 0,
                    "unrealized": r["unrealized"] or 0,
                })
            # Last 10 trades per trader (recent copies, not baselines or imports)
            pos_rows = conn.execute(
                "SELECT wallet_username, wallet_address, market_question, side, "
                "entry_price, current_price, size, pnl_unrealized, pnl_realized, status "
                "FROM copy_trades WHERE status != 'baseline' "
                "AND wallet_username NOT IN ('(manual)', '(wallet)') "
                "ORDER BY created_at DESC"
            ).fetchall()
            for p in pos_rows:
                addr = p["wallet_address"]
                if addr not in trader_positions:
                    trader_positions[addr] = []
                if len(trader_positions[addr]) >= 50:
                    continue
                pnl = p["pnl_realized"] if p["status"] == "closed" else (p["pnl_unrealized"] or 0)
                trader_positions[addr].append({
                    "market": (p["market_question"] or "")[:50],
                    "side": p["side"] or "",
                    "entry": p["entry_price"] or 0,
                    "current": p["current_price"] or 0,
                    "size": p["size"] or 0,
                    "pnl": round(pnl, 2),
                    "status": p["status"],
                })
    except Exception:
        pass

    return {
        "wallet": wallet,
        "positions": positions,
        "closed": closed,
        "traders": trader_stats,
        "copy_perf": copy_perf,
        "trader_positions": trader_positions,
        "deposit": config.STARTING_BALANCE,
    }


def _short(name, maxlen=25):
    """Shorten market name."""
    name = (name.replace("Counter-Strike: ", "CS: ").replace("League of Legends: ", "LoL: ")
            .replace("Dota 2: ", "Dota: ").replace("Valorant: ", "VAL: "))
    for cut in [" - ", " (BO", ": Both", ": Spread"]:
        if cut in name:
            name = name[:name.index(cut)]
            break
    return name[:maxlen] if len(name) > maxlen else name


def _verdict(wins, losses):
    if wins + losses == 0:
        return "no results yet"
    if losses == 0:
        return "all profitable"
    if wins == 0:
        return "all losing"
    if wins >= losses * 2:
        return "mostly winning"
    if losses >= wins * 2:
        return "mostly losing"
    return "mixed"


def _trade_line(t, maxname=30):
    tag = " W" if t["current"] >= 0.99 else " L" if t["current"] <= 0.01 else ""
    st = "[closed]" if t.get("status") == "closed" else "[open]"
    return "$%+.2f %s %dc>%dc %s %s%s" % (
        t["pnl"], _short(t["market"], maxname), round(t["entry"] * 100),
        round(t["current"] * 100), t["trader"], st, tag)


def generate_report() -> str:
    """Generate performance report: last positions per trader + recommendation."""
    d = _gather_data()

    followed_addrs_set = {w["address"] for w in db.get_followed_wallets()}
    current_copy = sorted(
        [cp for cp in d["copy_perf"] if cp["address"] in followed_addrs_set],
        key=lambda x: x["realized"] + x["unrealized"], reverse=True)

    # Collect all trades with trader name
    all_trades = []
    for cp in current_copy:
        for t in d.get("trader_positions", {}).get(cp["address"], []):
            t["trader"] = cp["name"]
            all_trades.append(t)

    # ═══ PREVIEW (2 lines — last 10 per trader) ═══
    preview = []
    for cp in current_copy:
        trades = d.get("trader_positions", {}).get(cp["address"], [])[:10]
        n = len(trades)
        pnl = sum(t["pnl"] for t in trades)
        wins_n = sum(1 for t in trades if t["pnl"] > 0.10)
        loss_n = sum(1 for t in trades if t["pnl"] < -0.10)
        open_n = sum(1 for t in trades if t.get("status") == "open" and -0.10 <= t["pnl"] <= 0.10)
        closed_n = sum(1 for t in trades if t.get("status") == "closed")
        best = max(trades, key=lambda t: t["pnl"]) if trades else None
        worst = min(trades, key=lambda t: t["pnl"]) if trades else None

        total_pnl = cp["realized"] + cp["unrealized"]
        total_trades = cp["open"] + cp["closed"]
        # Total first (the real number), then last 10 for trend
        parts = ["%s total: $%+.2f" % (cp["name"], total_pnl)]
        rec = []
        if wins_n: rec.append("%dW" % wins_n)
        if loss_n: rec.append("%dL" % loss_n)
        if open_n: rec.append("%d open" % open_n)
        if rec:
            parts.append("/".join(rec))
        parts.append(_verdict(wins_n, loss_n))
        if n < total_trades:
            parts.append("last %d: $%+.2f" % (n, pnl))
        if best and best["pnl"] > 0.20:
            tag = " W" if best["current"] >= 0.99 else ""
            parts.append("best: %s +$%.2f%s" % (_short(best["market"], 18), best["pnl"], tag))
        if worst and worst["pnl"] < -0.20:
            tag = " L" if worst["current"] <= 0.01 else ""
            parts.append("worst: %s $%.2f%s" % (_short(worst["market"], 18), worst["pnl"], tag))
        preview.append(" | ".join(parts))

    # ═══ FULL REPORT (per trader, most recent first) ═══
    full = []

    for cp in current_copy:
        trades = d.get("trader_positions", {}).get(cp["address"], [])
        n = len(trades)
        pnl = sum(t["pnl"] for t in trades)
        w = sum(1 for t in trades if t["pnl"] > 0.10)
        l = sum(1 for t in trades if t["pnl"] < -0.10)
        wr = round(w / max(w + l, 1) * 100)
        avg_w = sum(t["pnl"] for t in trades if t["pnl"] > 0.10) / max(w, 1)
        avg_l = sum(t["pnl"] for t in trades if t["pnl"] < -0.10) / max(l, 1)

        total_pnl_all = cp["realized"] + cp["unrealized"]
        total_trades = cp["open"] + cp["closed"]
        full.append("%s — last %d: $%+.2f | total (%d): $%+.2f | %dW/%dL %d%% | avg win $%.2f avg loss $%.2f" % (
            cp["name"], n, pnl, total_trades, total_pnl_all, w, l, wr, avg_w, abs(avg_l)))
        for t in trades:
            t["trader"] = cp["name"]
            full.append("  " + _trade_line(t))
        full.append("")

    # Recommendation
    current_total = sum(t["pnl"] for t in all_trades)
    total_w = sum(1 for t in all_trades if t["pnl"] > 0.10)
    total_l = sum(1 for t in all_trades if t["pnl"] < -0.10)
    full.append("")
    if current_total > 20:
        full.append("Recommendation: Performing well ($%+.2f). Keep running." % current_total)
    elif current_total > 5:
        full.append("Recommendation: Solid start ($%+.2f). Let positions develop." % current_total)
    elif current_total > -5:
        full.append("Recommendation: Early phase ($%+.2f). Let positions play out." % current_total)
    elif current_total > -20:
        full.append("Recommendation: Slightly down ($%+.2f). Normal variance." % current_total)
    else:
        full.append("Recommendation: Under pressure ($%+.2f). Monitor closely." % current_total)

    report = "\n".join(preview) + "\n---\n" + "\n".join(full)

    db.save_report(report, json.dumps({
        "total_pnl": round(current_total, 2),
        "winners": total_w, "losers": total_l,
        "total_positions": len(all_trades),
    }))
    logger.info("Report generated (%d chars)", len(report))
    return report
