"""
Redeem resolved Polymarket positions via Builder Relayer.

Uses the Polymarket Relayer to execute redeemPositions through
the proxy wallet — no MATIC needed, Polymarket pays gas.

Requires Builder API credentials (get from polymarket.com/settings → Builder).

Usage:
    python redeem_positions.py          # dry run
    python redeem_positions.py --exec   # execute redemptions
"""
import argparse
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("redeem")


def main():
    parser = argparse.ArgumentParser(description="Redeem resolved Polymarket positions")
    parser.add_argument("--exec", action="store_true", help="Execute (default: dry run)")
    args = parser.parse_args()

    # Load config
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config

    if not config.POLYMARKET_PRIVATE_KEY:
        logger.error("POLYMARKET_PRIVATE_KEY not set!")
        return

    builder_key = os.getenv("BUILDER_KEY", "")
    builder_secret = os.getenv("BUILDER_SECRET", "")
    builder_passphrase = os.getenv("BUILDER_PASSPHRASE", "")

    if not all([builder_key, builder_secret, builder_passphrase]):
        logger.error("Builder API credentials not set!")
        logger.error("Go to polymarket.com/settings -> Builder -> Create New")
        logger.error("Then add to .env: BUILDER_KEY, BUILDER_SECRET, BUILDER_PASSPHRASE")
        return

    # Show what needs redeeming (dry run always)
    from bot.wallet_scanner import fetch_wallet_positions
    import requests

    funder = config.POLYMARKET_FUNDER
    positions = fetch_wallet_positions(funder)

    # Use Polymarket API directly — positions at 99c+ with value are redeemable
    # Skip the slow Gamma API check (1 call per position)
    import requests as _req
    all_api = []
    _offset = 0
    while True:
        _r = _req.get("https://data-api.polymarket.com/positions", params={
            "user": funder, "limit": 500, "offset": _offset, "sizeThreshold": 0
        }, timeout=15)
        if not _r.ok: break
        _page = _r.json()
        if not _page: break
        all_api.extend(_page)
        if len(_page) < 500: break
        _offset += 500

    resolved = []
    for p in all_api:
        cp = float(p.get("curPrice", 0) or 0)
        cv = float(p.get("currentValue", 0) or 0)
        cid = p.get("conditionId", "")
        if cp >= 0.99 and cv > 0.05 and cid:
            resolved.append({
                "condition_id": cid,
                "size": cv,
                "side": p.get("outcome", ""),
                "market_question": p.get("title", ""),
            })

    if not resolved:
        logger.info("No resolved positions with value to redeem!")
        return

    total = sum(p.get("size", 0) for p in resolved)
    logger.info("Found %d resolved positions (total value: $%.2f):", len(resolved), total)
    for p in resolved:
        logger.info("  $%.2f | %s | %s", p.get("size", 0), p["side"],
                     (p.get("market_question") or "")[:50])

    if not args.exec:
        logger.info("DRY RUN — use --exec to redeem")
        return

    # Execute via poly-web3 Relayer
    logger.info("Connecting to Polymarket Relayer...")

    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON
    from py_builder_relayer_client.client import RelayClient
    from py_builder_signing_sdk.config import BuilderConfig
    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
    from poly_web3 import RELAYER_URL, PolyWeb3Service

    client = ClobClient(
        host="https://clob.polymarket.com",
        key=config.POLYMARKET_PRIVATE_KEY,
        chain_id=POLYGON,
        signature_type=1,
        funder=config.POLYMARKET_FUNDER,
    )
    client.set_api_creds(client.create_or_derive_api_creds())

    relayer_client = RelayClient(
        RELAYER_URL, POLYGON, config.POLYMARKET_PRIVATE_KEY,
        BuilderConfig(local_builder_creds=BuilderApiKeyCreds(
            key=builder_key,
            secret=builder_secret,
            passphrase=builder_passphrase,
        )),
    )

    service = PolyWeb3Service(
        clob_client=client,
        relayer_client=relayer_client,
        rpc_url="https://polygon-bor-rpc.publicnode.com",
    )

    # Balance before
    from bot.order_executor import get_wallet_balance
    bal_before = get_wallet_balance()

    logger.info("Redeeming all resolved positions...")
    condition_ids = [p.get("condition_id") for p in resolved if p.get("condition_id")]
    redeemed_total = 0
    failed_total = 0

    # Process in batches of 10, then retry failed ones individually
    batch_size = 10
    for i in range(0, len(condition_ids), batch_size):
        batch = condition_ids[i:i + batch_size]
        try:
            r = service.redeem(batch, batch_size=len(batch))
            success = len(r.success_list) if hasattr(r, 'success_list') else 0
            errors = len(r.error_condition_ids) if hasattr(r, 'error_condition_ids') else 0
            redeemed_total += success
            logger.info("Batch %d-%d: %d redeemed, %d errors", i, i + len(batch), success, errors)

            # Retry failed ones individually
            if hasattr(r, 'error_condition_ids') and r.error_condition_ids:
                import time as _rt
                for failed_cid in r.error_condition_ids:
                    _rt.sleep(2)
                    try:
                        r2 = service.redeem([failed_cid], batch_size=1)
                        if hasattr(r2, 'success_list') and r2.success_list:
                            redeemed_total += 1
                            logger.info("Retry OK: %s", failed_cid[:16])
                        else:
                            failed_total += 1
                    except Exception:
                        failed_total += 1
        except Exception as e:
            logger.error("Batch %d-%d failed: %s — trying individually", i, i + len(batch), e)
            import time as _rt
            for cid in batch:
                try:
                    _rt.sleep(2)
                    r = service.redeem([cid], batch_size=1)
                    redeemed_total += 1
                    logger.info("Individual OK: %s", cid[:16])
                except Exception as ex:
                    failed_total += 1
                    logger.error("Individual failed: %s: %s", cid[:16], ex)

        # Short pause between batches to avoid rate limiting
        import time as _rt
        _rt.sleep(3)

    logger.info("Redeem complete: %d redeemed, %d failed out of %d total",
                redeemed_total, failed_total, len(condition_ids))

    # Check new balance + remaining positions
    from bot.wallet_scanner import fetch_wallet_positions
    from database.db import log_activity

    new_bal = get_wallet_balance()
    remaining = fetch_wallet_positions(config.POLYMARKET_FUNDER)
    remaining_value = sum(p.get("size", 0) for p in remaining)

    redeemed_amount = new_bal - bal_before
    if redeemed_amount > 0.10:
        log_activity("redeem", "CASH", "Shares redeemed — $%.2f returned to wallet" % redeemed_amount,
                     "Balance: $%.2f → $%.2f (+$%.2f)" % (bal_before, new_bal, redeemed_amount), redeemed_amount)
    logger.info("=== AFTER REDEEM ===")
    logger.info("Wallet USDC:    $%.2f", new_bal)
    logger.info("Remaining shares: $%.2f (%d positions)", remaining_value, len(remaining))
    logger.info("Total value:    $%.2f", new_bal + remaining_value)

    # Log to a status file for monitoring
    import datetime
    status_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "redeem_status.txt")
    with open(status_file, "a") as f:
        f.write("%s | USDC=$%.2f | Shares=$%.2f | Positions=%d\n" % (
            datetime.datetime.now().isoformat()[:19], new_bal, remaining_value, len(remaining)))


if __name__ == "__main__":
    main()
