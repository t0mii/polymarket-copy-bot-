import logging
import os
import sys
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

import config
from database.db import init_db
from dashboard.app import app

# --- Logging Setup ---

os.makedirs(os.path.dirname(config.LOG_PATH), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("poly-copybot")






def copy_scan():
    """Scan followed wallets for new copy trades (smart detection, no AI tokens)."""
    from bot.copy_trader import copy_followed_wallets
    try:
        n = copy_followed_wallets()
        if n > 0:
            logger.info("[COPY] Copy scan: %d new trades copied.", n)
    except Exception as e:
        logger.exception("Error in copy scan: %s", e)


_update_counter = 0
_recently_closed: dict = {}  # cid → timestamp, prevents duplicate logs



def auto_redeem():
    """Auto-redeem resolved positions via Builder Relayer (every 5 min)."""
    try:
        import requests as _rq
        import time as _t
        from web3 import Web3
        from bot.order_executor import get_wallet_balance
        from database.db import log_activity

        funder = config.POLYMARKET_FUNDER
        if not funder or not config.BUILDER_KEY:
            return

        # Fetch all positions
        all_pos = []
        _off = 0
        while True:
            _r = _rq.get("https://data-api.polymarket.com/positions", params={
                "user": funder, "limit": 500, "offset": _off, "sizeThreshold": 0
            }, timeout=15)
            if not _r.ok: break
            _page = _r.json()
            if not _page: break
            all_pos.extend(_page)
            if len(_page) < 500: break
            _off += 500

        # Find redeemable positions (API says redeemable + on-chain verified)
        _w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
        CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
        CTF_ABI = [{"inputs":[{"name":"","type":"bytes32"}],"name":"payoutDenominator","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]
        _ctf = _w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)

        to_redeem = []
        for p in all_pos:
            cp = float(p.get("curPrice", 0) or 0)
            cv = float(p.get("currentValue", 0) or 0)
            cid = p.get("conditionId", "")
            if cp < 0.99 or cv <= 0.20 or not cid:
                continue
            if not p.get("redeemable", False):
                continue
            try:
                cid_bytes = bytes.fromhex(cid.replace("0x", ""))
                if _ctf.functions.payoutDenominator(cid_bytes).call() == 0:
                    continue
            except Exception:
                continue
            to_redeem.append(cid)

        if not to_redeem:
            return

        total_value = sum(float(p.get("currentValue", 0) or 0) for p in all_pos
                         if p.get("conditionId") in to_redeem)
        logger.info("[AUTO-REDEEM] %d positions to redeem ($%.2f)", len(to_redeem), total_value)

        # Connect to Relayer
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON
        from py_builder_relayer_client.client import RelayClient
        from py_builder_signing_sdk.config import BuilderConfig
        from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
        from poly_web3 import RELAYER_URL, PolyWeb3Service

        client = ClobClient(
            host="https://clob.polymarket.com",
            key=config.POLYMARKET_PRIVATE_KEY,
            chain_id=POLYGON, signature_type=1,
            funder=config.POLYMARKET_FUNDER,
        )
        client.set_api_creds(client.create_or_derive_api_creds())

        relayer_client = RelayClient(
            RELAYER_URL, POLYGON, config.POLYMARKET_PRIVATE_KEY,
            BuilderConfig(local_builder_creds=BuilderApiKeyCreds(
                key=config.BUILDER_KEY,
                secret=config.BUILDER_SECRET,
                passphrase=config.BUILDER_PASSPHRASE,
            )),
        )

        service = PolyWeb3Service(
            clob_client=client, relayer_client=relayer_client,
            rpc_url="https://polygon-bor-rpc.publicnode.com",
        )

        bal_before = get_wallet_balance()

        # Try batch first, then individual
        redeemed = 0
        try:
            result = service.redeem_all(batch_size=10)
            if hasattr(result, 'success_list') and result.success_list:
                redeemed = len(result.success_list)
                logger.info("[AUTO-REDEEM] Batch: %d redeemed", redeemed)
        except Exception:
            pass

        if redeemed < len(to_redeem):
            for cid in to_redeem:
                try:
                    r = service.redeem([cid], batch_size=1)
                    if hasattr(r, 'success_list') and r.success_list:
                        redeemed += 1
                except Exception:
                    pass
                _t.sleep(2)

        bal_after = get_wallet_balance()
        gained = bal_after - bal_before
        if gained > 0.10:
            logger.info("[AUTO-REDEEM] Done: +$%.2f (wallet $%.2f -> $%.2f)", gained, bal_before, bal_after)
            log_activity("redeem", "CASH", "Auto-Redeem",
                         "Won shares redeemed: +$%.2f (wallet $%.2f)" % (gained, bal_after), gained)
        elif redeemed > 0:
            logger.info("[AUTO-REDEEM] %d redeemed but no USDC change yet (settlement pending)", redeemed)

    except Exception as e:
        logger.warning("[AUTO-REDEEM] Error: %s", e)


def auto_generate_report():
    """Auto-generate performance report every 10 minutes."""
    from bot.ai_report import generate_report
    try:
        report = generate_report()
        logger.info("[REPORT] Auto-generated performance report (%d chars)", len(report))
    except Exception as e:
        logger.warning("Auto-report error: %s", e)


def track_blocked_outcomes():
    """Check outcomes of blocked trades (every 30 min)."""
    from bot.outcome_tracker import track_outcomes
    try:
        checked = track_outcomes()
        if checked > 0:
            logger.info("[OUTCOME] Tracked %d blocked trade outcomes", checked)
    except Exception as e:
        logger.warning("Outcome tracker error: %s", e)


def ai_analyze():
    """Run Claude AI analysis on blocked vs executed trades (every 6h)."""
    from bot.ai_analyzer import analyze_and_recommend
    try:
        result = analyze_and_recommend(hours=48)
        if result.get("error"):
            logger.warning("[AI-ANALYZE] %s", result["error"])
        elif result.get("recommendations"):
            logger.info("[AI-ANALYZE] %d recommendations generated", len(result["recommendations"]))
    except Exception as e:
        logger.warning("AI analyze error: %s", e)


def update_prices():
    """Update copy trade prices (every 30s), auto-sell wins, save snapshot every 5 min."""
    global _update_counter
    from bot.copy_trader import update_copy_positions
    from database import db as _db
    try:
        update_copy_positions()
        # Auto-sell won positions every cycle (recycle capital fast)
        try:
            from bot.order_executor import sell_shares, get_wallet_balance
            import requests as _rq
            import time as _t
            _all_positions = []
            _offset = 0
            while True:
                _r = _rq.get("https://data-api.polymarket.com/positions", params={
                    "user": config.POLYMARKET_FUNDER, "limit": 500, "offset": _offset, "sizeThreshold": 0
                }, timeout=config.DATA_API_TIMEOUT)
                if not _r.ok:
                    break
                _page = _r.json()
                if not _page:
                    break
                _all_positions.extend(_page)
                if len(_page) < 500:
                    break
                _offset += 500
            if _all_positions:
                for _p in _all_positions:
                    _cp = float(_p.get("curPrice", 0) or 0)
                    _cv = float(_p.get("currentValue", 0) or 0)
                    _iv = float(_p.get("initialValue", 0) or 0)
                    _pnl_check = _cv - _iv
                    _cid_pos = _p.get("conditionId", "")
                    if _cid_pos in _recently_closed and (_t.time() - _recently_closed[_cid_pos]) < 300:
                        continue
                    # Only auto-sell/close positions that are tracked in copy_trades
                    _our_trade = None
                    _pos_side = _p.get("outcome", "")
                    try:
                        from database.db import get_connection as _gc_check
                        with _gc_check() as _cc:
                            _our_trade = _cc.execute(
                                "SELECT id, size, entry_price, side, actual_entry_price, actual_size FROM copy_trades WHERE condition_id=? AND side=? AND status='open'", (_cid_pos, _pos_side)
                            ).fetchone()
                            if not _our_trade:
                                _our_trade = _cc.execute(
                                    "SELECT id, size, entry_price, side, actual_entry_price, actual_size FROM copy_trades WHERE condition_id=? AND status='open'", (_cid_pos,)
                                ).fetchone()
                        if not _our_trade:
                            continue  # not our bot's position, skip
                    except Exception:
                        continue  # DB error → skip, don't risk selling non-bot positions
                    # Use best available entry price/size (actual > planned)
                    _our_size = _our_trade["actual_size"] or _our_trade["size"] or _iv
                    _our_entry = _our_trade["actual_entry_price"] or _our_trade["entry_price"] or 0
                    # Close lost positions in DB (price went to 0)
                    if _cp <= config.AUTO_CLOSE_LOST_PRICE and _iv > 0.01:
                        _close_pnl = round(-_our_size, 2)
                        _close_title = (_p.get("title") or "")[:50]
                        _did_close = False
                        try:
                            from database.db import get_connection
                            with get_connection() as _conn:
                                _now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                _did_close = _conn.execute(
                                    "UPDATE copy_trades SET status='closed', pnl_realized=?, current_price=0, closed_at=? "
                                    "WHERE condition_id=? AND side=? AND status='open'",
                                    (_close_pnl, _now, _cid_pos, _pos_side)).rowcount > 0
                                if not _did_close:
                                    _did_close = _conn.execute(
                                        "UPDATE copy_trades SET status='closed', pnl_realized=?, current_price=0, closed_at=? "
                                        "WHERE condition_id=? AND status='open'",
                                        (_close_pnl, _now, _cid_pos)).rowcount > 0
                        except Exception:
                            pass
                        if _did_close:
                            logger.info("[AUTO-CLOSE] Lost position marked closed: $%.2f | %s", _iv, _close_title[:40])
                            _recently_closed[_cid_pos] = _t.time()
                            try:
                                _db.log_activity("resolved", "LOSS", "Position lost",
                                                 "%s — P&L $%.2f" % (_close_title[:35], _close_pnl), _close_pnl)
                                from dashboard.app import broadcast_event
                                broadcast_event("trade_closed", {"market": _close_title, "pnl": _close_pnl, "price": 0, "trader": "auto", "size": _our_size})
                            except Exception:
                                pass
                        continue  # Already handled — skip auto-sell
                    # Close won positions in DB (price at 100c, resolved)
                    elif _cp >= config.AUTO_CLOSE_WON_PRICE and _iv > 0.01:
                        _shares = _our_size / _our_entry if _our_entry > 0 else 0
                        _pnl_won = round((1.0 - _our_entry) * _shares, 2)
                        _close_title = (_p.get("title") or "")[:50]
                        _did_close = False
                        try:
                            from database.db import get_connection
                            with get_connection() as _conn:
                                _now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                _did_close = _conn.execute(
                                    "UPDATE copy_trades SET status='closed', pnl_realized=?, current_price=1.0, closed_at=? "
                                    "WHERE condition_id=? AND side=? AND status='open'",
                                    (_pnl_won, _now, _cid_pos, _pos_side)).rowcount > 0
                                if not _did_close:
                                    _did_close = _conn.execute(
                                        "UPDATE copy_trades SET status='closed', pnl_realized=?, current_price=1.0, closed_at=? "
                                        "WHERE condition_id=? AND status='open'",
                                        (_pnl_won, _now, _cid_pos)).rowcount > 0
                        except Exception:
                            pass
                        if _did_close:
                            logger.info("[AUTO-CLOSE] Won position marked closed: +$%.2f | %s", _pnl_won, _close_title[:40])
                            _recently_closed[_cid_pos] = _t.time()
                            try:
                                _db.log_activity("resolved", "WIN", "Position won",
                                                 "#%s — P&L $+%.2f" % (_close_title[:35], _pnl_won), _pnl_won)
                                from dashboard.app import broadcast_event
                                broadcast_event("trade_closed", {"market": _close_title, "pnl": _pnl_won, "price": 100, "trader": "auto", "size": _our_size})
                            except Exception:
                                pass
                        continue  # Already handled — skip auto-sell
                    # Use OUR entry price for profit check — API's initialValue differs due to slippage/fees
                    _our_pnl_check = (_cp - _our_entry) * (_our_size / _our_entry) if _our_entry > 0 else 0
                    if _cp >= config.AUTO_SELL_PRICE and _cv > 0.50 and _our_pnl_check > 0:
                        _out = _p.get("outcome", "")
                        if _out.lower() in ("yes", "y"): _side = "YES"
                        elif _out.lower() in ("no", "n"): _side = "NO"
                        else: _side = _out
                        _cid = _p.get("conditionId", "")
                        _resp = sell_shares(_cid, _side, _cp)
                        if _resp:
                            _sell_shares = _our_size / _our_entry if _our_entry > 0 else 0
                            _pnl = round((_cp - _our_entry) * _sell_shares, 2)
                            # Correct with actual USDC received if available
                            if _resp.get("usdc_received", 0) > 0:
                                _pnl = round(_resp["usdc_received"] - _our_size, 2)
                            logger.info("[AUTO-SELL] Sold: P&L $%+.2f (entry %.0fc, sell %.0fc) | %s", _pnl, _our_entry * 100, _cp * 100, (_p.get("title") or "")[:40])
                            _recently_closed[_cid_pos] = _t.time()
                            _db.log_activity("sell", "WIN" if _pnl >= 0 else "LOSS",
                                             "Position closed — %s" % ("profit" if _pnl >= 0 else "sold"),
                                             "%s — sold $%.2f, P&L $%+.2f" % ((_p.get("title") or "")[:35], _cv, _pnl), _pnl)
                            try:
                                from dashboard.app import broadcast_event
                                broadcast_event("trade_closed", {"market": (_p.get("title") or "")[:50], "pnl": _pnl, "price": round(_cp * 100), "trader": "auto", "size": _our_size})
                            except Exception:
                                pass
                            # Close matching copy_trade in DB + save usdc_received
                            try:
                                from database.db import get_connection
                                _now2 = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                _usdc_recv = _resp.get("usdc_received", 0)
                                with get_connection() as _conn:
                                    _conn.execute(
                                        "UPDATE copy_trades SET status='closed', pnl_realized=?, current_price=?, closed_at=?, usdc_received=? "
                                        "WHERE condition_id=? AND side=? AND status='open'",
                                        (_pnl, _cp, _now2, _usdc_recv, _cid, _pos_side))
                            except Exception as _e:
                                logger.warning("[AUTO-SELL] DB update failed: %s", _e)
        except Exception as _e:
            logger.warning("[AUTO-SELL] Error in auto-sell loop: %s", _e)
        _update_counter += 1
        # Snapshot alle 10 Updates (= 5 Min bei 30s Intervall)
        if _update_counter >= 10:
            _stale = [k for k, v in _recently_closed.items() if _t.time() - v > config.RECENTLY_CLOSED_SECS]
            for _sk in _stale:
                del _recently_closed[_sk]
            _update_counter = 0
            try:
                from bot.order_executor import get_wallet_balance
                from bot.wallet_scanner import fetch_wallet_positions
                import requests
                bal = get_wallet_balance()
                r = requests.get("https://data-api.polymarket.com/positions", params={
                    "user": config.POLYMARKET_FUNDER, "limit": 500, "sizeThreshold": 0
                }, timeout=config.DATA_API_TIMEOUT)
                pos_val = sum(float(p.get("currentValue", 0) or 0) for p in (r.json() if r.ok else []))
                total = bal + pos_val
                pnl = total - config.STARTING_BALANCE
                _db.save_copy_portfolio_snapshot({
                    "total_value": round(total, 2),
                    "cash_balance": round(bal, 2),
                    "open_positions_value": round(pos_val, 2),
                    "pnl_total": round(pnl, 2),
                })
                logger.info("Snapshot saved: $%.2f (PnL $%.2f)", total, pnl)
            except Exception as e:
                logger.warning("Snapshot error: %s", e)
    except Exception as e:
        logger.exception("Error updating prices: %s", e)


def run_startup_baseline():
    """Erzwingt eine frische Baseline für alle gefolgten Wallets beim Start.
    Läuft synchron BEVOR der Scheduler startet — so werden NUR neue Positionen kopiert.
    """
    import time as _time
    from database import db as _db
    from bot.wallet_scanner import fetch_wallet_positions, fetch_wallet_recent_trades

    followed = list(_db.get_followed_wallets())
    if not followed:
        logger.info("BASELINE: Keine gefolgten Wallets — übersprungen.")
        return

    logger.info("=" * 60)
    logger.info("STARTUP BASELINE: %d Wallets werden neu eingelesen...", len(followed))
    logger.info("Bestehende Positionen werden NICHT kopiert.")
    logger.info("=" * 60)

    for wallet in followed:
        address = wallet["address"]
        username = wallet["username"] or address[:12]
        try:
            # Snapshot und Baseline-Flag zurücksetzen
            _db.clear_wallet_snapshot(address)
            _db.set_wallet_unbaselined(address)

            # Aktuelle Positionen holen
            positions = fetch_wallet_positions(address)
            if not positions:
                logger.info("  [SKIP] %s — keine Positionen erreichbar", username)
                _db.set_wallet_baselined(address)
                continue

            # Snapshot speichern (alle aktuellen Positionen als bekannt markieren)
            _db.save_position_snapshot(address, positions)

            # Baseline-Einträge in copy_trades anlegen (verhindert Doppelkopien)
            saved = 0
            for pos in positions:
                cid = pos.get("condition_id", "")
                if not cid or pos.get("size", 0) < 0.50:
                    continue
                try:
                    if not _db.is_trade_duplicate(address, pos["market_question"], cid):
                        _db.create_baseline_trade({
                            "wallet_address": address,
                            "wallet_username": username,
                            "market_question": pos["market_question"],
                            "market_slug": pos.get("market_slug", ""),
                            "event_slug": pos.get("event_slug", ""),
                            "side": pos["side"],
                            "entry_price": pos["current_price"],
                            "end_date": pos.get("end_date", ""),
                            "outcome_label": pos.get("outcome_label", ""),
                            "condition_id": cid,
                        })
                        saved += 1
                except Exception:
                    pass

            _db.set_wallet_baselined(address)
            # Echten letzten Trade-Timestamp aus API holen — Idle-Check braucht realen Wert
            try:
                recent = fetch_wallet_recent_trades(address, limit=5)
                real_ts = max((t["timestamp"] for t in recent), default=int(_time.time()))
            except Exception:
                real_ts = int(_time.time())
            _db.set_last_trade_timestamp(address, real_ts)
            mins_ago = (int(_time.time()) - real_ts) // 60
            logger.info("  [OK] %-20s %d Positionen als Baseline | letzter Trade vor %d min",
                        username, len(positions), mins_ago)

        except Exception as e:
            logger.warning("  [ERR] %s — Baseline fehlgeschlagen: %s", username, e)
            try:
                _db.set_last_trade_timestamp(address, int(_time.time()))
                _db.set_wallet_baselined(address)
            except Exception:
                pass

    logger.info("=" * 60)
    logger.info("BASELINE FERTIG — Bot kopiert ab jetzt NUR neue Positionen!")
    logger.info("=" * 60)


def main():
    logger.info("=" * 60)
    logger.info("Poly CopyBot starting...")
    logger.info("Dashboard port: %d", config.DASHBOARD_PORT)
    logger.info("=" * 60)

    # Initialize database
    init_db()
    logger.info("Database initialized.")

    # Sync followed traders from .env
    if config.FOLLOWED_TRADERS:
        from database.db import upsert_wallet, toggle_follow, unfollow_all
        unfollow_all()
        for entry in config.FOLLOWED_TRADERS.split(","):
            entry = entry.strip()
            if ":" not in entry:
                continue
            parts = entry.split(":", 1)
            name = parts[0].strip()
            addr = parts[1].strip()
            upsert_wallet({
                "address": addr, "username": name,
                "rank": 0, "volume": 0, "pnl": 0, "markets_traded": 0,
                "score": 0, "strategy_type": "",
                "strengths": "", "weaknesses": "",
                "recommendation": "COPY", "reasoning": "From .env",
                "win_rate": 0, "total_trades": 0,
                "profile_url": "https://polymarket.com/profile/" + addr,
            })
            toggle_follow(addr, 1)
            logger.info("Following %s (%s) from .env", name, addr[:16])

    # Startup-Validierung: Keys prüfen BEVOR der Bot losläuft
    from bot.copy_trader import LIVE_MODE, STARTING_BALANCE
    balance = 0
    if LIVE_MODE:
        if not config.POLYMARKET_PRIVATE_KEY:
            logger.error("=" * 60)
            logger.error("FEHLER: POLYMARKET_PRIVATE_KEY nicht gesetzt!")
            logger.error("Trage deinen Private Key in .env ein.")
            logger.error("Ohne Key kann der Bot KEINE echten Orders platzieren.")
            logger.error("=" * 60)
            return
        if not config.POLYMARKET_FUNDER:
            logger.warning("WARNUNG: POLYMARKET_FUNDER nicht gesetzt — wird automatisch ermittelt.")
        # Verbindungstest
        try:
            from bot.order_executor import test_connection, get_wallet_balance
            if test_connection():
                balance = get_wallet_balance()
                logger.info("Wallet verbunden! USDC Balance: $%.2f", balance)
                if balance < 1.0:
                    logger.warning("WARNUNG: Nur $%.2f USDC auf der Wallet — Trading kaum moeglich.", balance)
            else:
                logger.warning("CLOB Verbindungstest fehlgeschlagen — Bot startet trotzdem.")
        except Exception as e:
            logger.warning("Wallet-Check uebersprungen: %s", e)
    logger.info("Mode: %s | Startkapital: $%.0f | Version: %s", "LIVE" if LIVE_MODE else "PAPER", STARTING_BALANCE, config.BOT_VERSION)

    from database.db import log_activity

    # STARTUP CLEANUP: close any zombie positions (resolved while bot was down)
    try:
        import requests as _req_startup
        from database.db import get_connection
        _r_startup = _req_startup.get("https://data-api.polymarket.com/positions", params={
            "user": config.POLYMARKET_FUNDER, "limit": 500, "sizeThreshold": 0
        }, timeout=config.DATA_API_TIMEOUT)
        if _r_startup.ok:
            _api_prices = {p.get("conditionId", ""): float(p.get("curPrice", 0) or 0) for p in _r_startup.json()}
            with get_connection() as _conn:
                _open_trades = _conn.execute(
                    "SELECT id, condition_id, size, market_question FROM copy_trades WHERE status='open'"
                ).fetchall()
                _cleaned = 0
                for _ot in _open_trades:
                    _cp = _api_prices.get(_ot["condition_id"], -1)
                    if _cp <= 0.01 and _cp >= 0:
                        _pnl = round(-(_ot["size"] or 0), 2)
                        _conn.execute("UPDATE copy_trades SET status='closed', pnl_realized=?, closed_at=datetime('now') WHERE id=?",
                                      (_pnl, _ot["id"]))
                        _conn.execute("INSERT INTO activity_log (event_type, icon, title, detail, pnl) VALUES (?,?,?,?,?)",
                                      ("resolved", "LOSS", "Position lost", "%s — P&L $%.2f" % ((_ot["market_question"] or "")[:35], _pnl), _pnl))
                        _cleaned += 1
                    elif _cp >= config.AUTO_CLOSE_WON_PRICE:
                        # Won: shares × $1 - invested
                        _ep = 0
                        try:
                            _ep_row = _conn.execute("SELECT entry_price FROM copy_trades WHERE id=?", (_ot["id"],)).fetchone()
                            _ep = _ep_row["entry_price"] if _ep_row else 0
                        except Exception:
                            pass
                        _shares = (_ot["size"] or 0) / _ep if _ep > 0 else 0
                        _pnl = round(_shares * 1.0 - (_ot["size"] or 0), 2)
                        _conn.execute("UPDATE copy_trades SET status='closed', pnl_realized=?, closed_at=datetime('now') WHERE id=?",
                                      (_pnl, _ot["id"]))
                        _conn.execute("INSERT INTO activity_log (event_type, icon, title, detail, pnl) VALUES (?,?,?,?,?)",
                                      ("resolved", "WIN", "Position won", "%s — P&L $+%.2f" % ((_ot["market_question"] or "")[:35], _pnl), _pnl))
                        _cleaned += 1
                if _cleaned:
                    logger.info("[STARTUP] Cleaned %d zombie positions (resolved while bot was down)", _cleaned)
    except Exception as e:
        logger.debug("Startup cleanup skipped: %s", e)

    # STARTUP BASELINE: Immer beim Start neue Baseline erstellen
    # → verhindert, dass bestehende Positionen kopiert werden
    run_startup_baseline()

    # Start WebSocket price tracker (real-time prices for open positions)
    from bot.ws_price_tracker import price_tracker
    price_tracker.start()
    logger.info("WebSocket price tracker started.")

    # Schedule daily scans
    scheduler = BackgroundScheduler()
    # Copy-Scan (Intervall einstellbar via COPY_SCAN_INTERVAL in .env)
    scheduler.add_job(
        copy_scan,
        "interval",
        seconds=config.COPY_SCAN_INTERVAL,
        id="copy_scan",
        next_run_time=datetime.now(),
        coalesce=True,
        misfire_grace_time=30,
        max_instances=1,
    )
    # Update copy trade prices + close-check alle 30 Sekunden (kostenlos)
    scheduler.add_job(
        update_prices,
        "interval",
        seconds=60,
        id="price_update",
        next_run_time=datetime.now(),
        coalesce=True,
        misfire_grace_time=60,
        max_instances=1,
    )
    # Auto-generate performance report (every 5 min, only if 5+ new activities)
    scheduler.add_job(
        auto_generate_report,
        "interval",
        minutes=10,
        id="auto_report",
        next_run_time=datetime.now() + timedelta(minutes=2),
    )
    # Outcome tracker: check what would have happened with blocked trades (every 30 min)
    scheduler.add_job(
        track_blocked_outcomes,
        "interval",
        minutes=30,
        id="outcome_tracker",
        next_run_time=datetime.now() + timedelta(minutes=5),
    )
    # AI Analysis: Claude analyzes blocked vs executed trades (every 6 hours)
    if config.ANTHROPIC_API_KEY:
        scheduler.add_job(
            ai_analyze,
            "interval",
            hours=6,
            id="ai_analyze",
            next_run_time=datetime.now() + timedelta(minutes=30),
        )
        logger.info("AI Analyzer enabled (every 6h, first run in 30min)")
    else:
        logger.info("AI Analyzer disabled (no ANTHROPIC_API_KEY)")
    # Auto-redeem resolved positions (every 5 min)
    scheduler.add_job(
        auto_redeem,
        "interval",
        minutes=5,
        id="auto_redeem",
        next_run_time=datetime.now() + timedelta(minutes=1),
        coalesce=True,
        misfire_grace_time=120,
        max_instances=1,
    )
    logger.info("Auto-Redeem enabled (every 5 min)")

    # UPGRADE: register new jobs
    # DISABLED (module not implemented): scheduler.add_job(performance_update, 'interval', minutes=30, id='performance_update',
    #                       next_run_time=datetime.now() + timedelta(seconds=60))
    # DISABLED (module not implemented): scheduler.add_job(ml_train, 'cron', hour=4, minute=0, id='ml_train',
    #                       next_run_time=datetime.now() + timedelta(seconds=120))
    scheduler.add_job(discovery_scan, 'interval', hours=3, id='discovery_scan',
                      next_run_time=datetime.now() + timedelta(seconds=180))
    scheduler.add_job(autonomous_scan, 'interval', seconds=60, id='autonomous_scan', max_instances=1,
                      next_run_time=datetime.now() + timedelta(seconds=90))
    scheduler.add_job(daily_report, 'cron', hour=0, minute=5, id='daily_report')
    scheduler.add_job(auto_backup, 'interval', hours=6, id='auto_backup',
                      next_run_time=datetime.now() + timedelta(seconds=60))
    scheduler.add_job(auto_tune_settings, 'interval', hours=2, id='auto_tuner',
                      next_run_time=datetime.now() + timedelta(seconds=100))
    scheduler.add_job(clv_update, 'interval', hours=2, id='clv_update',
                      next_run_time=datetime.now() + timedelta(seconds=200))
    # DISABLED (module not implemented): scheduler.add_job(arbitrage_scan, 'interval', minutes=30, id='arbitrage_scan',
    #                       next_run_time=datetime.now() + timedelta(seconds=240))
    # DISABLED (module not implemented): scheduler.add_job(ai_news_scan, 'interval', hours=2, id='ai_news_scan',
    #                       next_run_time=datetime.now() + timedelta(seconds=300))
    scheduler.add_job(smart_sell_check, 'interval', seconds=60, id='smart_sell',
                      next_run_time=datetime.now() + timedelta(seconds=45))
    # DISABLED (module not implemented): scheduler.add_job(smart_rebalance, 'cron', day_of_week='mon', hour=4, minute=30,
    #                       id='smart_rebalance',
    #                       next_run_time=datetime.now() + timedelta(seconds=150))
    scheduler.add_job(brain_engine, 'interval', hours=2, id='brain_engine',
                      next_run_time=datetime.now() + timedelta(minutes=5))
    scheduler.start()
    logger.info("Scheduler started (copy scan every %ds, prices every 30s, outcome every 30min).",
                config.COPY_SCAN_INTERVAL)

    # Start Flask dashboard
    logger.info("Starting dashboard on http://%s:%d", config.DASHBOARD_HOST, config.DASHBOARD_PORT)
    try:
        app.run(
            host=config.DASHBOARD_HOST,
            port=config.DASHBOARD_PORT,
            debug=False,
            use_reloader=False,
            threaded=True,
        )
    except KeyboardInterrupt:
        logger.info("Scanner stopped by user.")
    finally:
        scheduler.shutdown()
        price_tracker.stop()
        logger.info("Scheduler + WebSocket stopped. Goodbye!")




# =====================================================================
# UPGRADE: Performance, ML, Discovery, Autonomous, Router
# =====================================================================

def performance_update():
    """Update trader + category performance stats (every 30 min)."""
    from bot.trader_performance import update_all_trader_stats, update_category_stats, update_adaptive_stop_loss
    try:
        update_all_trader_stats()
        update_category_stats()
        update_adaptive_stop_loss()
    except Exception as e:
        logger.exception('Error in performance update: %s', e)

def ml_train():
    """Re-train ML model daily at 4 AM."""
    from bot.ml_scorer import train_model
    try:
        train_model()
    except Exception as e:
        logger.exception('Error in ML training: %s', e)

def discovery_scan():
    """Scan leaderboard + paper-follow + check promotions (every 6h)."""
    from bot.auto_discovery import check_inactivity, check_reactivation, scan_leaderboard, scan_all_sources, paper_follow_candidates, check_promotions
    try:
        scan_all_sources()
        paper_follow_candidates()
        check_promotions()
        check_reactivation()
        check_inactivity()
    except Exception as e:
        logger.exception('Error in discovery scan: %s', e)

def autonomous_scan():
    """Scan for autonomous trading signals (every 60 sec)."""
    from bot.autonomous_signals import scan_momentum_signals, update_autonomous_positions
    try:
        scan_momentum_signals()
        try:
            from bot.autonomous_signals import scan_ai_divergence_signals
            scan_ai_divergence_signals()
        except Exception:
            pass
        update_autonomous_positions()
    except Exception as e:
        logger.exception('Error in autonomous scan: %s', e)




def daily_report():
    """Generate daily AI report at midnight."""
    from bot.daily_report import generate_daily_report
    try:
        generate_daily_report()
    except Exception as e:
        logger.exception("Error in daily report: %s", e)

def auto_backup():
    """Auto-backup to GitHub every 6h."""
    from bot.auto_backup import run_backup
    try:
        run_backup()
    except Exception as e:
        logger.exception("Error in auto backup: %s", e)


def auto_tune_settings():
    """Auto-tune all per-trader settings based on performance (every 2h)."""
    from bot.auto_tuner import auto_tune
    try:
        auto_tune()
    except Exception as e:
        logger.exception('Error in auto-tuner: %s', e)

def clv_update():
    """Update CLV tracking stats (every 2h)."""
    from bot.clv_tracker import update_clv_for_closed_trades
    try:
        update_clv_for_closed_trades()
    except Exception as e:
        logger.exception("Error in CLV update: %s", e)

def arbitrage_scan():
    """Scan for complete-set and logic arbitrage (every 30 min)."""
    from bot.arbitrage import scan_complete_set_arb, scan_logic_arb
    try:
        scan_complete_set_arb()
        scan_logic_arb()
    except Exception as e:
        logger.exception("Error in arbitrage scan: %s", e)

def ai_news_scan():
    """AI news trading scan (every 2h)."""
    from bot.ai_news_trader import scan_ai_opportunities
    try:
        scan_ai_opportunities()
    except Exception as e:
        logger.exception("Error in AI news scan: %s", e)

def smart_sell_check():
    """Check if traders exited positions we are copying (every 60s)."""
    from bot.smart_sell import check_trader_exits
    try:
        check_trader_exits()
    except Exception as e:
        logger.exception('Error in smart sell check: %s', e)

def smart_rebalance():
    """Weekly smart router rebalancing (Monday 4 AM)."""
    from bot.smart_router import rebalance
    try:
        rebalance()
    except Exception as e:
        logger.exception('Error in smart rebalance: %s', e)


def brain_engine():
    """Brain Engine — self-diagnosis, auto-fix, lifecycle management (every 2h)."""
    from bot.brain import run_brain
    try:
        run_brain()
    except Exception as e:
        logger.exception("Error in brain engine: %s", e)

if __name__ == "__main__":
    main()
