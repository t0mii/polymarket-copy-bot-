"""
Smart Position Closing — erkennt wenn kopierte Trader Positionen reduzieren.
Checkt alle 60s ob Trader ihre Positionen verkleinert haben.
v2: Fix doppelte Sells + resolved markets.
"""
import logging
import threading
import time

from database import db
from bot.wallet_scanner import fetch_wallet_positions
import config

logger = logging.getLogger(__name__)

# Cooldown: cid -> timestamp, verhindert doppelte Sells
_sell_cooldown = {}
_sell_cooldown_lock = threading.Lock()
_COOLDOWN_SECS = 120


def check_trader_exits():
    """Check if any followed trader has reduced/closed positions we're copying."""
    with db.get_connection() as conn:
        our_trades = conn.execute(
            "SELECT * FROM copy_trades WHERE status = 'open' AND condition_id != ''"
        ).fetchall()

    if not our_trades:
        return

    # Group by wallet address
    by_wallet = {}
    for t in our_trades:
        addr = t["wallet_address"]
        if addr not in by_wallet:
            by_wallet[addr] = []
        by_wallet[addr].append(dict(t))

    now = time.time()

    for address, trades in by_wallet.items():
        try:
            positions = fetch_wallet_positions(address)
            trader_cids = {p["condition_id"] for p in positions if p.get("condition_id")}

            for our_trade in trades:
                cid = our_trade.get("condition_id", "")
                if not cid:
                    continue

                # Cooldown check — nicht nochmal versuchen
                with _sell_cooldown_lock:
                    if cid in _sell_cooldown and now < _sell_cooldown[cid]:
                        continue

                # Trader hat diese Position noch → alles OK
                if cid in trader_cids:
                    continue

                # Trader hat Position verlassen
                entry = our_trade.get("actual_entry_price") or our_trade.get("entry_price") or 0
                # Use live WebSocket price instead of stale DB price
                _live_price = None
                try:
                    from bot.ws_price_tracker import price_tracker
                    _live_price = price_tracker.get_price(cid, our_trade.get("side", "YES"))
                except Exception:
                    pass
                current = _live_price or our_trade.get("current_price") or entry
                if entry <= 0:
                    continue

                username = our_trade.get("wallet_username", "?")
                shares = our_trade.get("shares_held", 0)
                size = our_trade.get("actual_size") or our_trade.get("size") or 0

                # Berechne P&L
                if entry > 0 and current > 0:
                    pnl_shares = size / entry if entry > 0 else 0
                    pnl = round((current - entry) * pnl_shares, 2)
                else:
                    pnl = 0

                # Versuche zu verkaufen
                sell_success = False
                usdc_received = 0
                if config.LIVE_MODE:
                    try:
                        from bot.order_executor import sell_shares
                        sell_resp = sell_shares(cid, our_trade["side"], current)
                        if sell_resp:
                            sell_success = True
                            # Korrigiere P&L mit echtem Erloes
                            usdc_received = sell_resp.get("usdc_received", 0)
                            if usdc_received > 0:
                                pnl = round(usdc_received - size, 2)
                        else:
                            # Sell failed — vielleicht resolved market
                            # Trotzdem DB-close, die Shares sind wertlos oder redeemable
                            logger.info("[SMART-SELL] Sell failed for %s — closing DB anyway (likely resolved)",
                                        our_trade["market_question"][:40])
                    except Exception as e:
                        logger.debug("[SMART-SELL] Sell error: %s", e)

                # DB nur schliessen wenn Sell OK oder Markt resolved (Preis nahe 0/1)
                if sell_success or current <= 0.05 or not config.LIVE_MODE:  # PATCH-023: removed current>=0.95, retry sell instead of closing orphan
                    closed = db.close_copy_trade(our_trade["id"], pnl, close_price=current)
                    if closed:
                        # Persist usdc_received so future P&L analysis has verified fill data
                        if usdc_received > 0:
                            _real_usdc = usdc_received
                        elif current >= 0.95:
                            # Resolved as winner: each share pays $1
                            _shares = shares or (size / entry if entry > 0 else 0)
                            _real_usdc = round(_shares, 4)
                        else:
                            # Resolved as loser or paper mode
                            _real_usdc = 0
                        try:
                            db.update_closed_trade_pnl(our_trade["id"], pnl, _real_usdc)
                        except Exception:
                            pass
                else:
                    logger.warning("[SMART-SELL] Sell failed, keeping DB open (shares still in wallet): %s",
                                   our_trade["market_question"][:40])
                    closed = False
                if closed:
                    logger.info("[SMART-SELL] #%d CLOSED: %s exited %s — P&L $%.2f%s",
                                our_trade["id"], username,
                                our_trade["market_question"][:40], pnl,
                                " (sold)" if sell_success else " (DB only)")
                    db.log_activity(
                        "smart_sell", "WIN" if pnl > 0 else "LOSS",
                        "Smart-Sell: %s exited" % username,
                        "#%d %s — P&L $%+.2f" % (our_trade["id"], our_trade["market_question"][:40], pnl),
                        pnl
                    )
                    try:
                        db.update_trade_score_outcome(cid, username, pnl)
                    except Exception as _score_e:
                        logger.debug("[FEEDBACK] update_trade_score_outcome failed: %s", _score_e)
                    try:
                        from dashboard.app import broadcast_event
                        broadcast_event("smart_sell", {
                            "trader": username,
                            "market": our_trade["market_question"][:60],
                            "pnl": round(pnl, 2),
                        })
                    except Exception:
                        pass

                # Cooldown setzen
                with _sell_cooldown_lock:
                    _sell_cooldown[cid] = now + _COOLDOWN_SECS

        except Exception as e:
            logger.debug("[SMART-SELL] Error checking %s: %s", address[:10], e)

    # Cleanup alte cooldowns
    with _sell_cooldown_lock:
        for cid in list(_sell_cooldown):
            if _sell_cooldown[cid] < now:
                del _sell_cooldown[cid]
