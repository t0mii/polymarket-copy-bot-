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

    return {
        "wallet": wallet,
        "positions": positions,
        "closed": closed,
        "traders": trader_stats,
        "deposit": config.STARTING_BALANCE,
    }


def generate_report() -> str:
    """Generate smart performance report."""
    d = _gather_data()
    lines = []

    # === PORTFOLIO STATUS ===
    active = [p for p in d["positions"] if 0.01 < p["current"] < 0.99]
    pending = [p for p in d["positions"] if p["current"] >= 0.99]
    lost = [p for p in d["positions"] if p["current"] <= 0.01]

    total_value = d["wallet"] + sum(p["value"] for p in d["positions"])
    profit = total_value - d["deposit"]
    profit_pct = (profit / d["deposit"] * 100) if d["deposit"] > 0 else 0

    if profit >= 0:
        lines.append("Portfolio is profitable at $%.2f (+$%.2f / +%.1f%%)." % (total_value, profit, profit_pct))
    else:
        lines.append("Portfolio is at $%.2f (down $%.2f / %.1f%%)." % (total_value, abs(profit), profit_pct))

    lines.append("Wallet: $%.2f available. %d active positions ($%.2f), %d pending payout ($%.2f)." % (
        d["wallet"], len(active), sum(p["value"] for p in active),
        len(pending), sum(p["value"] for p in pending)))

    if lost:
        lost_cost = sum(p["cost"] for p in lost)
        lines.append("%d positions lost ($%.2f written off)." % (len(lost), lost_cost))

    # === CLOSED PERFORMANCE ===
    if d["closed"]:
        wins = [c for c in d["closed"] if c["pnl"] >= 0]
        losses = [c for c in d["closed"] if c["pnl"] < 0]
        total_pnl = sum(c["pnl"] for c in d["closed"])
        wr = len(wins) / len(d["closed"]) * 100

        lines.append("")
        lines.append("Closed: %d positions (%d wins, %d losses, %.0f%% WR). Net P&L: $%+.2f." % (
            len(d["closed"]), len(wins), len(losses), wr, total_pnl))

        if wins:
            best = max(wins, key=lambda c: c["pnl"])
            lines.append("Best win: %s (+$%.2f)." % (best["market"][:35], best["pnl"]))
        if losses:
            worst = min(losses, key=lambda c: c["pnl"])
            lines.append("Worst loss: %s ($%.2f)." % (worst["market"][:35], worst["pnl"]))

        # Win pattern analysis
        avg_win = sum(c["pnl"] for c in wins) / len(wins) if wins else 0
        avg_loss = sum(c["pnl"] for c in losses) / len(losses) if losses else 0
        if avg_win > 0 and avg_loss < 0:
            ratio = abs(avg_win / avg_loss)
            if ratio > 2:
                lines.append("Avg win ($%.2f) is %.1fx avg loss ($%.2f) — good risk/reward." % (avg_win, ratio, abs(avg_loss)))
            elif ratio < 0.5:
                lines.append("Warning: Avg win ($%.2f) is smaller than avg loss ($%.2f) — wins need to be bigger." % (avg_win, abs(avg_loss)))

    # === TRADER PERFORMANCE ===
    if d["traders"]:
        lines.append("")
        for t in d["traders"]:
            day_str = "+$%.0f" % t["day"] if t["day"] >= 0 else "-$%.0f" % abs(t["day"])
            week_str = "+$%.0f" % t["week"] if t["week"] >= 0 else "-$%.0f" % abs(t["week"])

            if t["week"] > 10000:
                grade = "exceptional"
            elif t["week"] > 1000:
                grade = "strong"
            elif t["week"] > 0:
                grade = "positive"
            elif t["week"] > -1000:
                grade = "underperforming"
            else:
                grade = "poor — consider removing"

            lines.append("%s: %s today, %s this week — %s." % (t["name"], day_str, week_str, grade))

    # === OPEN POSITION ANALYSIS ===
    if active:
        in_profit = [p for p in active if p["pnl"] > 0.50]
        in_loss = [p for p in active if p["pnl"] < -0.50]

        if in_profit:
            best_open = max(in_profit, key=lambda p: p["pnl"])
            lines.append("")
            lines.append("Best open: %s (%dc -> %dc, +$%.2f)." % (
                best_open["market"][:35], best_open["entry"]*100, best_open["current"]*100, best_open["pnl"]))

        if in_loss:
            worst_open = min(in_loss, key=lambda p: p["pnl"])
            lines.append("Worst open: %s (%dc -> %dc, $%.2f)." % (
                worst_open["market"][:35], worst_open["entry"]*100, worst_open["current"]*100, worst_open["pnl"]))

        # Concentration risk
        if active:
            biggest = max(active, key=lambda p: p["value"])
            pct = biggest["value"] / sum(p["value"] for p in active) * 100
            if pct > 40:
                lines.append("Warning: %.0f%% of active value in one position (%s)." % (pct, biggest["market"][:30]))

    # === RECOMMENDATION ===
    lines.append("")

    if profit_pct > 10:
        lines.append("Recommendation: Strong performance. Consider taking some profit.")
    elif profit_pct > 0:
        lines.append("Recommendation: Profitable. Keep running, monitor traders.")
    elif profit_pct > -10:
        lines.append("Recommendation: Slight loss. Normal variance — stay patient.")
    elif profit_pct > -25:
        lines.append("Recommendation: Significant loss. Review trader selection and position sizes.")
    else:
        lines.append("Recommendation: Heavy loss. Consider pausing and reviewing strategy.")

    # Cash warning
    if d["wallet"] < 5:
        lines.append("Warning: Low cash ($%.2f). Bot cannot copy new positions until funds are available." % d["wallet"])

    report = "\n".join(lines)

    # Save
    db.save_report(report, json.dumps({
        "wallet": d["wallet"], "total_value": total_value, "profit": profit,
        "active": len(active), "pending": len(pending), "closed": len(d["closed"]),
    }))

    logger.info("Report generated (%d chars)", len(report))
    return report
