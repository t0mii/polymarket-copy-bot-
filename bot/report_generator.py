import html as _html
import logging
import os
from datetime import datetime

import config

logger = logging.getLogger(__name__)


def generate_report(analyzed_wallets: list[dict], source: str = "leaderboard", top_n: int = 10) -> str:
    """Generate an HTML report with the top N wallets."""
    os.makedirs(config.REPORTS_DIR, exist_ok=True)

    top_wallets = analyzed_wallets[:top_n]
    now = datetime.now()
    filename = f"report_{source}_{now.strftime('%Y-%m-%d_%H-%M')}.html"
    filepath = os.path.join(config.REPORTS_DIR, filename)

    # Count stats
    total_scanned = len(analyzed_wallets)
    copy_count = sum(1 for w in analyzed_wallets if w["recommendation"] == "COPY")
    watch_count = sum(1 for w in analyzed_wallets if w["recommendation"] == "WATCH")

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Wallet Report - {now.strftime('%d.%m.%Y %H:%M')}</title>
    <style>
        :root {{
            --bg: #0d1117; --bg-card: #161b22; --border: #30363d;
            --text: #e6edf3; --text-dim: #8b949e;
            --green: #00C853; --red: #FF1744; --blue: #58a6ff; --yellow: #d29922; --purple: #bc8cff;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); padding: 20px; }}
        .container {{ max-width: 1100px; margin: 0 auto; }}
        header {{ text-align: center; margin-bottom: 30px; padding-bottom: 20px; border-bottom: 1px solid var(--border); }}
        header h1 {{ font-size: 1.8rem; margin-bottom: 8px; }}
        header .meta {{ color: var(--text-dim); font-size: 0.9rem; }}
        .stats {{ display: flex; gap: 16px; justify-content: center; margin: 16px 0; }}
        .stat {{ background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; padding: 12px 20px; text-align: center; }}
        .stat-value {{ font-size: 1.4rem; font-weight: 700; }}
        .stat-label {{ font-size: 0.75rem; color: var(--text-dim); text-transform: uppercase; }}
        .wallet-card {{ background: var(--bg-card); border: 1px solid var(--border); border-radius: 10px; padding: 20px; margin-bottom: 16px; }}
        .wallet-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }}
        .wallet-rank {{ font-size: 2rem; font-weight: 800; color: var(--purple); margin-right: 16px; }}
        .wallet-name {{ font-size: 1.2rem; font-weight: 600; }}
        .wallet-address {{ font-size: 0.8rem; color: var(--text-dim); font-family: monospace; }}
        .wallet-address a {{ color: var(--blue); text-decoration: none; }}
        .wallet-address a:hover {{ text-decoration: underline; }}
        .score {{ font-size: 1.5rem; font-weight: 800; padding: 4px 16px; border-radius: 8px; }}
        .score-high {{ background: rgba(0,200,83,0.2); color: var(--green); }}
        .score-mid {{ background: rgba(210,153,34,0.2); color: var(--yellow); }}
        .score-low {{ background: rgba(255,23,68,0.2); color: var(--red); }}
        .wallet-stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; margin: 12px 0; }}
        .ws {{ text-align: center; padding: 8px; background: var(--bg); border-radius: 6px; }}
        .ws-value {{ font-weight: 700; font-size: 1rem; }}
        .ws-label {{ font-size: 0.7rem; color: var(--text-dim); text-transform: uppercase; }}
        .rec {{ display: inline-block; padding: 3px 10px; border-radius: 4px; font-size: 0.75rem; font-weight: 700; text-transform: uppercase; }}
        .rec-copy {{ background: rgba(0,200,83,0.2); color: var(--green); }}
        .rec-watch {{ background: rgba(210,153,34,0.2); color: var(--yellow); }}
        .rec-skip {{ background: rgba(255,23,68,0.2); color: var(--red); }}
        .tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; background: rgba(88,166,255,0.15); color: var(--blue); margin-right: 4px; }}
        .detail {{ margin-top: 10px; font-size: 0.85rem; color: var(--text-dim); line-height: 1.5; }}
        .detail strong {{ color: var(--text); }}
        .positions {{ margin-top: 10px; }}
        .positions h4 {{ font-size: 0.8rem; color: var(--text-dim); text-transform: uppercase; margin-bottom: 6px; }}
        .pos {{ font-size: 0.8rem; padding: 4px 0; border-bottom: 1px solid var(--border); }}
        .positive {{ color: var(--green); }}
        .negative {{ color: var(--red); }}
        footer {{ text-align: center; color: var(--text-dim); font-size: 0.8rem; padding: 20px 0; }}
    </style>
</head>
<body>
<div class="container">
    <header>
        <h1>Polymarket Wallet Report</h1>
        <div class="meta">Quelle: {source.upper()} | {now.strftime('%d.%m.%Y %H:%M')} Uhr</div>
        <div class="stats">
            <div class="stat"><div class="stat-value">{total_scanned}</div><div class="stat-label">Gescannt</div></div>
            <div class="stat"><div class="stat-value">{len(top_wallets)}</div><div class="stat-label">Top Wallets</div></div>
            <div class="stat"><div class="stat-value positive">{copy_count}</div><div class="stat-label">Copy</div></div>
            <div class="stat"><div class="stat-value" style="color:var(--yellow)">{watch_count}</div><div class="stat-label">Watch</div></div>
        </div>
    </header>
"""

    for i, w in enumerate(top_wallets, 1):
        score = w["score"]
        score_class = "score-high" if score >= 7 else "score-mid" if score >= 5 else "score-low"
        rec_class = f"rec-{w['recommendation'].lower()}"
        pnl_class = "positive" if w["pnl"] >= 0 else "negative"
        pnl_sign = "+" if w["pnl"] >= 0 else ""
        display_name = _html.escape(w["username"]) if w["username"] else w["address"][:12] + "..."

        positions_html = ""
        if w.get("positions"):
            positions_html = '<div class="positions"><h4>Offene Positionen</h4>'
            for p in w["positions"]:
                positions_html += f'<div class="pos">{_html.escape(p["side"])} - {_html.escape(p["market_question"][:70])} | ${p["size"]:.2f}</div>'
            positions_html += "</div>"

        html += f"""
    <div class="wallet-card">
        <div class="wallet-header">
            <div style="display:flex;align-items:center;">
                <span class="wallet-rank">#{i}</span>
                <div>
                    <div class="wallet-name">{display_name}</div>
                    <div class="wallet-address"><a href="{w['profile_url']}" target="_blank">{w['address']}</a></div>
                </div>
            </div>
            <div style="text-align:right">
                <div class="score {score_class}">{score}/10</div>
                <div style="margin-top:6px"><span class="rec {rec_class}">{w['recommendation']}</span></div>
            </div>
        </div>
        <div class="wallet-stats">
            <div class="ws"><div class="ws-value {pnl_class}">{pnl_sign}${w['pnl']:,.2f}</div><div class="ws-label">P&L</div></div>
            <div class="ws"><div class="ws-value">${w['volume']:,.0f}</div><div class="ws-label">Volume</div></div>
            <div class="ws"><div class="ws-value">{w['win_rate']}%</div><div class="ws-label">Win Rate</div></div>
            <div class="ws"><div class="ws-value">{w['total_trades']}</div><div class="ws-label">Trades</div></div>
            <div class="ws"><div class="ws-value">{w['markets_traded']}</div><div class="ws-label">Märkte</div></div>
        </div>
        <div style="margin:8px 0"><span class="tag">{_html.escape(str(w['strategy_type']))}</span><span class="tag">Rang #{w['rank']}</span><span class="tag">{_html.escape(str(w['source']))}</span></div>
        <div class="detail">
            <strong>Stärken:</strong> {_html.escape(str(w['strengths']))}<br>
            <strong>Schwächen:</strong> {_html.escape(str(w['weaknesses']))}<br>
            <strong>Analyse:</strong> {_html.escape(str(w['reasoning']))}
        </div>
        {positions_html}
    </div>
"""

    html += """
    <footer>Polymarket AI Wallet Scanner | Automatisch generiert</footer>
</div>
</body>
</html>"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info("Report saved: %s", filepath)
    return filepath
