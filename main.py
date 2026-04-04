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
logger = logging.getLogger("wallet-scanner")


def scheduled_scan():
    """Run a scheduled wallet scan."""
    from scan_wallets import run_scan
    try:
        run_scan(
            limit=config.SCAN_WALLET_LIMIT,
            max_analyze=config.MAX_AI_ANALYSES,
            top_n=config.TOP_N_REPORT,
            open_report=False,
        )
    except Exception as e:
        logger.exception("Error in scheduled scan: %s", e)


def auto_follow_scan():
    """Auto-follow Top-Trader nach ROI-Effizienz."""
    from bot.wallet_scanner import auto_follow_top_traders
    try:
        top = auto_follow_top_traders(count=config.AUTO_FOLLOW_COUNT)
        logger.info("Auto-Follow abgeschlossen: %d Trader werden jetzt gefolgt.", len(top))
    except Exception as e:
        logger.exception("Error in auto-follow: %s", e)


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

def update_prices():
    """Update copy trade prices (every 30s), save snapshot every 5 min."""
    global _update_counter
    from bot.copy_trader import update_copy_positions
    from database import db as _db
    try:
        update_copy_positions()
        _update_counter += 1
        # Snapshot alle 10 Updates (= 5 Min bei 30s Intervall)
        if _update_counter >= 10:
            _update_counter = 0
            try:
                from bot.order_executor import get_wallet_balance
                from bot.wallet_scanner import fetch_wallet_positions
                import requests
                bal = get_wallet_balance()
                r = requests.get("https://data-api.polymarket.com/positions", params={
                    "user": config.POLYMARKET_FUNDER, "limit": 500, "sizeThreshold": 0
                }, timeout=15)
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
                logger.debug("Snapshot error: %s", e)
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
    logger.info("Polymarket Wallet Scanner starting...")
    logger.info("Scan interval: %d hours | Dashboard port: %d",
                config.SCAN_INTERVAL_HOURS, config.DASHBOARD_PORT)
    logger.info("=" * 60)

    # Initialize database
    init_db()
    logger.info("Database initialized.")

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
    logger.info("Mode: %s | Startkapital: $%d", "LIVE" if LIVE_MODE else "PAPER", STARTING_BALANCE)

    from database.db import log_activity

    # STARTUP BASELINE: Immer beim Start neue Baseline erstellen
    # → verhindert, dass bestehende Positionen kopiert werden
    run_startup_baseline()

    # Start WebSocket price tracker (real-time prices for open positions)
    from bot.ws_price_tracker import price_tracker
    price_tracker.start()
    logger.info("WebSocket price tracker started.")

    # Schedule daily scans
    scheduler = BackgroundScheduler()
    # AI-Scan deaktiviert — Copy Bot braucht keine AI, spart Tokens
    # scheduler.add_job(
    #     scheduled_scan,
    #     "interval",
    #     hours=config.SCAN_INTERVAL_HOURS,
    #     id="wallet_scan",
    #     next_run_time=datetime.now(),
    # )
    # Auto-Follow deaktiviert — nur manuell gefolgte Trader (RN1)
    # scheduler.add_job(
    #     auto_follow_scan,
    #     "interval",
    #     hours=1,
    #     id="auto_follow",
    #     next_run_time=datetime.now() + timedelta(minutes=5),
    # )
    # Copy-Scan (Intervall einstellbar via COPY_SCAN_INTERVAL in .env)
    scheduler.add_job(
        copy_scan,
        "interval",
        seconds=config.COPY_SCAN_INTERVAL,
        id="copy_scan",
        next_run_time=datetime.now(),
    )
    # Update copy trade prices + close-check alle 30 Sekunden (kostenlos)
    scheduler.add_job(
        update_prices,
        "interval",
        seconds=30,
        id="price_update",
        next_run_time=datetime.now(),
    )
    scheduler.start()
    logger.info("Scheduler started (copy scan every %ds, prices every 30s).", config.COPY_SCAN_INTERVAL)

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


if __name__ == "__main__":
    main()
