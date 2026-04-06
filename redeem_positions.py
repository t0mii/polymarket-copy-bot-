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

    # On-chain verification: check payoutDenominator directly on Polygon
    from web3 import Web3
    _w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
    CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
    # Minimal ABI for payoutDenominator(bytes32) -> uint256
    CTF_ABI = [{"inputs":[{"name":"","type":"bytes32"}],"name":"payoutDenominator","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]
    _ctf = _w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)

    resolved = []
    skipped_awaiting = 0
    skipped_not_onchain = 0
    skipped_dust = 0
    for p in all_api:
        cp = float(p.get("curPrice", 0) or 0)
        cv = float(p.get("currentValue", 0) or 0)
        cid = p.get("conditionId", "")
        if cp < 0.99 or cv <= 0.05 or not cid:
            continue
        # API says not redeemable — skip
        if not p.get("redeemable", False):
            skipped_awaiting += 1
            continue
        # Skip dust positions
        if cv < 0.20:
            skipped_dust += 1
            continue
        # On-chain check: payoutDenominator must be > 0
        try:
            cid_bytes = bytes.fromhex(cid.replace("0x", ""))
            payout_den = _ctf.functions.payoutDenominator(cid_bytes).call()
            if payout_den == 0:
                skipped_not_onchain += 1
                continue
        except Exception as e:
            logger.debug("On-chain check failed for %s: %s", cid[:16], e)
            skipped_not_onchain += 1
            continue
        resolved.append({
            "condition_id": cid,
            "size": cv,
            "side": p.get("outcome", ""),
            "market_question": p.get("title", ""),
        })
    if skipped_awaiting:
        logger.info("Skipped %d positions awaiting on-chain resolve", skipped_awaiting)
    if skipped_not_onchain:
        logger.info("Skipped %d positions (API says ready but on-chain NOT resolved)", skipped_not_onchain)
    if skipped_dust:
        logger.info("Skipped %d dust positions (< $0.20)", skipped_dust)

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

    logger.info("Redeeming %d resolved positions ($%.2f)...", len(resolved), total)
    condition_ids = [p.get("condition_id") for p in resolved if p.get("condition_id")]
    redeemed_total = 0
    failed_total = 0
    import time as _rt

    # Try redeem_all first (sometimes works for all at once)
    try:
        result = service.redeem_all(batch_size=10)
        success = len(result.success_list) if hasattr(result, 'success_list') else 0
        if success > 0:
            redeemed_total += success
            logger.info("redeem_all: %d redeemed", success)
    except Exception as e:
        logger.info("redeem_all failed: %s — trying individually", e)

    # Process remaining one by one (Relayer only handles 1 per transaction)
    if redeemed_total < len(condition_ids):
        for i, cid in enumerate(condition_ids):
            try:
                r = service.redeem([cid], batch_size=1)
                if hasattr(r, 'success_list') and r.success_list:
                    redeemed_total += 1
                    vol = r.success_list[0].get('derivedMetadata', {}).get('operationCount', 1) if r.success_list else 0
                    logger.info("[%d/%d] Redeemed: %s", i + 1, len(condition_ids), cid[:20])
                else:
                    failed_total += 1
            except Exception as ex:
                failed_total += 1
                logger.debug("[%d/%d] Failed: %s: %s", i + 1, len(condition_ids), cid[:16], ex)
            _rt.sleep(2)  # pause between calls to avoid rate limiting

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
        log_activity("redeem", "CASH", "Payout",
                     "Won shares converted to USDC. Wallet: $%.2f → $%.2f (+$%.2f)" % (bal_before, new_bal, redeemed_amount), redeemed_amount)
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
