"""AI Analyzer — uses Claude to analyze blocked vs executed trades and suggest parameter changes.

Runs periodically (every 6h). Gathers:
- Blocked trades with outcomes (would_have_won)
- Executed trades with P&L
- Current settings

Sends structured prompt to Claude API, parses recommendations.
"""
import json
import logging
import os

import config
from database import db

logger = logging.getLogger(__name__)


def _get_api_key() -> str:
    """Get Anthropic API key from config."""
    key = config.ANTHROPIC_API_KEY
    if not key:
        key = os.getenv("ANTHROPIC_API_KEY", "")
    return key


def _format_settings() -> str:
    """Format current bot settings for the prompt."""
    return """BET_SIZE_PCT={bet_size}
BET_SIZE_MAP={bet_map}
TRADER_EXPOSURE_MAP={exp_map}
CATEGORY_BLACKLIST_MAP={cat_map}
MIN_TRADER_USD={min_usd} | MIN_TRADER_USD_MAP={min_usd_map}
MIN_ENTRY_PRICE={min_price} | MIN_ENTRY_PRICE_MAP={min_price_map}
MAX_ENTRY_PRICE={max_price} | MAX_ENTRY_PRICE_MAP={max_price_map}
MIN_CONVICTION_RATIO={min_conv} | MIN_CONVICTION_RATIO_MAP={min_conv_map}
MAX_PER_EVENT={max_evt} | MAX_PER_MATCH={max_match}
MAX_COPIES_PER_MARKET={max_copies}
STOP_LOSS_PCT={sl} | TAKE_PROFIT_PCT={tp}
NO_REBUY_MINUTES={no_rebuy}
MAX_SPREAD={spread}
MAX_FEE_BPS={fee}
TRAILING_STOP_ENABLED={ts_on} | TRAILING_STOP_MARGIN={ts_margin} | TRAILING_STOP_ACTIVATE={ts_act}""".format(
        bet_size=config.BET_SIZE_PCT,
        bet_map=config.BET_SIZE_MAP,
        exp_map=config.TRADER_EXPOSURE_MAP,
        cat_map=config.CATEGORY_BLACKLIST_MAP,
        min_usd=config.MIN_TRADER_USD, min_usd_map=config.MIN_TRADER_USD_MAP,
        min_price=config.MIN_ENTRY_PRICE, min_price_map=config.MIN_ENTRY_PRICE_MAP,
        max_price=config.MAX_ENTRY_PRICE, max_price_map=config.MAX_ENTRY_PRICE_MAP,
        min_conv=config.MIN_CONVICTION_RATIO, min_conv_map=config.MIN_CONVICTION_RATIO_MAP,
        max_evt=config.MAX_PER_EVENT, max_match=config.MAX_PER_MATCH,
        max_copies=config.MAX_COPIES_PER_MARKET,
        sl=config.STOP_LOSS_PCT, tp=config.TAKE_PROFIT_PCT,
        no_rebuy=config.NO_REBUY_MINUTES,
        spread=config.MAX_SPREAD,
        fee=config.MAX_FEE_BPS,
        ts_on=config.TRAILING_STOP_ENABLED, ts_margin=config.TRAILING_STOP_MARGIN,
        ts_act=config.TRAILING_STOP_ACTIVATE,
    )


def _format_blocked_summary(blocked: list) -> str:
    """Format blocked trades into a summary table."""
    if not blocked:
        return "Keine geblockten Trades in diesem Zeitraum."

    # Group by reason
    by_reason = {}
    for bt in blocked:
        r = bt["block_reason"]
        if r not in by_reason:
            by_reason[r] = {"total": 0, "won": 0, "lost": 0, "unknown": 0, "trades": []}
        by_reason[r]["total"] += 1
        if bt.get("would_have_won") == 1:
            by_reason[r]["won"] += 1
        elif bt.get("would_have_won") == 0:
            by_reason[r]["lost"] += 1
        else:
            by_reason[r]["unknown"] += 1
        if len(by_reason[r]["trades"]) < 5:
            by_reason[r]["trades"].append(bt)

    lines = ["| Filter | Blocked | Would-Win | Would-Lose | Unknown | Win% |"]
    lines.append("|--------|---------|-----------|------------|---------|------|")
    for reason, data in sorted(by_reason.items(), key=lambda x: x[1]["total"], reverse=True):
        checked = data["won"] + data["lost"]
        win_pct = "%.0f%%" % (data["won"] / checked * 100) if checked > 0 else "n/a"
        lines.append("| %s | %d | %d | %d | %d | %s |" % (
            reason, data["total"], data["won"], data["lost"], data["unknown"], win_pct))

    lines.append("")
    lines.append("Top geblockte Trades (mit Outcome):")
    for bt in sorted(blocked, key=lambda x: x["would_have_won"] if x.get("would_have_won") is not None else -1, reverse=True)[:15]:
        outcome = "WIN" if bt.get("would_have_won") == 1 else "LOSS" if bt.get("would_have_won") == 0 else "?"
        lines.append("  [%s] %s | %s | %.0fc | %s | %s" % (
            outcome, bt["trader"], bt["market_question"][:40], bt["trader_price"] * 100,
            bt["block_reason"], bt.get("block_detail", "")))

    return "\n".join(lines)


def _format_blocked_by_trader(blocked: list) -> str:
    """Format blocked trades grouped by trader."""
    by_trader = {}
    for bt in blocked:
        t = bt["trader"]
        if t not in by_trader:
            by_trader[t] = {"total": 0, "won": 0, "lost": 0, "reasons": {}}
        by_trader[t]["total"] += 1
        if bt.get("would_have_won") == 1:
            by_trader[t]["won"] += 1
        elif bt.get("would_have_won") == 0:
            by_trader[t]["lost"] += 1
        r = bt["block_reason"]
        by_trader[t]["reasons"][r] = by_trader[t]["reasons"].get(r, 0) + 1

    lines = []
    for trader, data in sorted(by_trader.items(), key=lambda x: x[1]["total"], reverse=True):
        checked = data["won"] + data["lost"]
        win_pct = "%.0f%%" % (data["won"] / checked * 100) if checked > 0 else "n/a"
        top_reasons = sorted(data["reasons"].items(), key=lambda x: x[1], reverse=True)[:3]
        reasons_str = ", ".join("%s(%d)" % (r, c) for r, c in top_reasons)
        lines.append("%s: %d blocked (win%%=%s) — %s" % (trader, data["total"], win_pct, reasons_str))
    return "\n".join(lines)


def _format_executed_summary(hours: int = 48) -> str:
    """Format executed trades summary."""
    try:
        stats = db.get_copy_trade_stats()
        closed = db.get_closed_copy_trades(limit=200)

        by_trader = {}
        for t in closed:
            tn = t["wallet_username"] or "unknown"
            if tn not in by_trader:
                by_trader[tn] = {"wins": 0, "losses": 0, "pnl": 0}
            pnl = t["pnl_realized"] or 0
            if pnl > 0:
                by_trader[tn]["wins"] += 1
            elif pnl < 0:
                by_trader[tn]["losses"] += 1
            by_trader[tn]["pnl"] += pnl

        lines = ["Gesamt: %d Trades, %d offen, %d geschlossen, WR=%.0f%%, P&L=$%+.2f" % (
            stats["total_trades"], stats["open_trades"], stats["closed_trades"],
            stats["win_rate"], stats["total_pnl"])]
        lines.append("")
        for tn, data in sorted(by_trader.items(), key=lambda x: x[1]["pnl"], reverse=True):
            total = data["wins"] + data["losses"]
            wr = data["wins"] / total * 100 if total > 0 else 0
            lines.append("%s: %dW/%dL (%.0f%%) P&L=$%+.2f" % (
                tn, data["wins"], data["losses"], wr, data["pnl"]))
        return "\n".join(lines)
    except Exception as e:
        return "Fehler beim Laden: %s" % e


def analyze_and_recommend(hours: int = 48) -> dict:
    """Run Claude analysis on blocked vs executed trades.

    Returns dict with 'analysis', 'recommendations', 'error'.
    """
    api_key = _get_api_key()
    if not api_key:
        logger.warning("[AI-ANALYZE] No ANTHROPIC_API_KEY set — skipping analysis")
        return {"analysis": "", "recommendations": [], "error": "No API key"}

    # Gather data
    blocked = db.get_blocked_trades_since(hours=hours)
    stats = db.get_blocked_trade_stats(hours=hours)

    if not blocked:
        logger.info("[AI-ANALYZE] No blocked trades in last %dh — skipping", hours)
        return {"analysis": "Keine geblockten Trades.", "recommendations": [], "error": None}

    blocked_summary = _format_blocked_summary(blocked)
    blocked_by_trader = _format_blocked_by_trader(blocked)
    executed_summary = _format_executed_summary(hours)
    settings = _format_settings()

    # Calculate key metrics
    total_blocked = stats["total"]
    checked = stats["checked"]
    would_have_won = stats["would_have_won"]
    won_pct = (would_have_won / checked * 100) if checked > 0 else 0

    prompt = """Du bist der Analyst fuer einen Polymarket Copy-Trading Bot.
Der Bot kopiert Trades von 5 Tradern mit verschiedenen Filtern. Deine Aufgabe:
Analysiere die geblockten Trades und empfehle konkrete Parameter-Aenderungen.

## Aktuelle Settings
{settings}

## Ausgefuehrte Trades (letzte {hours}h)
{executed}

## Geblockte Trades (letzte {hours}h): {total} total, {checked} geprueft, {won} davon waeren Gewinner ({won_pct:.0f}%)
{blocked_summary}

## Pro Trader
{blocked_by_trader}

## Aufgabe
Analysiere diese Daten und antworte in diesem EXAKTEN JSON-Format:

```json
{{
  "analysis": "2-3 Saetze Zusammenfassung der wichtigsten Erkenntnisse",
  "recommendations": [
    {{
      "setting": "CATEGORY_BLACKLIST_MAP",
      "current": "aktueller Wert",
      "suggested": "vorgeschlagener Wert",
      "reason": "Warum diese Aenderung",
      "confidence": 75,
      "expected_impact": "+$X pro Woche geschaetzt"
    }}
  ],
  "filter_scores": {{
    "category_blacklist": {{"verdict": "zu_aggressiv|ok|zu_lasch", "detail": "..."}},
    "exposure_limit": {{"verdict": "zu_aggressiv|ok|zu_lasch", "detail": "..."}},
    "price_range": {{"verdict": "zu_aggressiv|ok|zu_lasch", "detail": "..."}},
    "conviction_ratio": {{"verdict": "zu_aggressiv|ok|zu_lasch", "detail": "..."}},
    "max_copies": {{"verdict": "ok", "detail": "..."}},
    "event_full": {{"verdict": "ok|zu_aggressiv", "detail": "..."}},
    "spread": {{"verdict": "ok", "detail": "..."}}
  }}
}}
```

Wichtige Regeln:
- Nur Aenderungen empfehlen wenn die Daten es klar stuetzen (>10 geblockte Trades pro Kategorie)
- Confidence 0-100: wie sicher bist du?
- Esports-Maerkte haben 10% Fee (1000bps) — das ist normal, nicht wegfiltern
- Wenn ein Filter viele potenzielle Gewinner blockt → "zu_aggressiv"
- Wenn ein Filter hauptsaechlich Verlierer durchlaesst → "zu_lasch"
- Antworte NUR mit dem JSON-Block, kein Text drumherum""".format(
        settings=settings,
        hours=hours,
        executed=executed_summary,
        total=total_blocked,
        checked=checked,
        won=would_have_won,
        won_pct=won_pct,
        blocked_summary=blocked_summary,
        blocked_by_trader=blocked_by_trader,
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()
        # Extract JSON from response (may be wrapped in ```json ... ```)
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()

        result = json.loads(raw)
        analysis = result.get("analysis", "")
        recommendations = result.get("recommendations", [])
        filter_scores = result.get("filter_scores", {})

        # Save to DB
        db.save_ai_recommendation(
            analysis_text=analysis + "\n\nFilter-Scores:\n" + json.dumps(filter_scores, indent=2),
            recommendations_json=json.dumps(recommendations),
            blocked_count=total_blocked,
            executed_count=stats.get("total", 0),
            would_have_won_pct=won_pct,
        )

        logger.info("[AI-ANALYZE] Analysis complete: %d recommendations, %.0f%% blocked would-have-won",
                     len(recommendations), won_pct)

        return {
            "analysis": analysis,
            "recommendations": recommendations,
            "filter_scores": filter_scores,
            "blocked_total": total_blocked,
            "would_have_won_pct": won_pct,
            "error": None,
        }

    except ImportError:
        logger.error("[AI-ANALYZE] anthropic package not installed — run: pip install anthropic")
        return {"analysis": "", "recommendations": [], "error": "anthropic not installed"}
    except json.JSONDecodeError as e:
        logger.warning("[AI-ANALYZE] Failed to parse Claude response as JSON: %s", e)
        # Save raw response anyway
        db.save_ai_recommendation(
            analysis_text="Parse error: %s\n\nRaw:\n%s" % (e, raw[:1000]),
            recommendations_json="[]",
            blocked_count=total_blocked,
            executed_count=0,
            would_have_won_pct=won_pct,
        )
        return {"analysis": raw[:500], "recommendations": [], "error": "JSON parse error"}
    except Exception as e:
        logger.error("[AI-ANALYZE] Claude API error: %s", e)
        return {"analysis": "", "recommendations": [], "error": str(e)}
