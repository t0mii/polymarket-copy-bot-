"""Sell orphaned positions that the bot no longer tracks.

Run once manually: python sell_orphans.py [--dry-run]
"""
import sys
import time
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

DRY_RUN = "--dry-run" in sys.argv

from bot.order_executor import sell_shares, get_wallet_balance

# Orphan positions: (condition_id, side, current_price, title)
ORPHANS = [
    ("0x9049d96597900ba0f3013604dc7adc3dd6e7e4764c5a0c16318605c40ac1ee96", "No", 0.66, "Israeli forces cross Litani River"),
    ("0x68beecc857017df8663fa36c0c86310dec7305aa88fe830c94651391314afc1c", "No", 0.90, "Trump end military ops vs Houthis"),
    ("0x2f1f643ab7589b143e35a6545241cfb80890a5dbd63b74c600baa35c26d0b315", "Yes", 0.95, "DHS shutdown 60 days+"),
    ("0x13868b2e3652b3d226fa431be9aef48f1fe4ab34d6aad850eedaa337d15faf81", "No", 0.79, "Resni.ca next Govt Slovenia"),
    ("0x924a2942747dd75703321a7c8d809c68f6a514c3b0f2a2e64274e02310634669", "No", 0.80, "Hormuz traffic normal by end"),
    ("0x9c2938c2757ab547ed25e46ff926ea5fc2a03a18e40741688c938ceb9bd64db0", "Gamespace Mediterranean College Esports", 0.50, "LoL: Gamespace vs Te"),
    ("0xfcde02af15efe40e336d67f0ef9aa49e9d40c37b92961fb890afc4afdbfe54d6", "Yes", 0.77, "TISZA win Hungary"),
    ("0xc5675bc58c8117391cc243780c1b709ce1ef744aa27779c78ee82f3dc974ca1b", "No", 0.86, "Kharg Island hit by April (1)"),
    ("0xdb2980522a8c7d374343f2e1c23be060d8c8c6379c04c87829984ee414fc4f64", "No", 0.93, "Vesna next Govt Slovenia"),
    ("0x635b39b06348f41eff8424d738c1e8d03b0235537e6545ba22894a38c9e8105a", "No", 0.35, "SLS next Govt Slovenia"),
    ("0x93908e1864c6af41f6eae75716c4b68a5dd9ccb27cc1fca7401a560639d3e483", "No", 0.92, "Kharg Island hit by April (2)"),
    ("0x7d88881c817fddfd8a3605e4a496b901ec9f94e3938399a0957aadaa890fe29a", "Yes", 0.27, "Social Democrats next Govt Slovenia"),
]


def main():
    bal_before = get_wallet_balance()
    logger.info("USDC balance before: $%.2f", bal_before)
    logger.info("Selling %d orphan positions%s", len(ORPHANS), " (DRY RUN)" if DRY_RUN else "")

    sold = 0
    failed = 0
    for cid, side, price, title in ORPHANS:
        logger.info("--- Selling: %s @ %.0fc [%s]", title, price * 100, side)
        if DRY_RUN:
            logger.info("  [DRY RUN] would sell %s", cid[:20])
            continue

        result = sell_shares(cid, side, price)
        if result:
            usdc = result.get("usdc_received", 0)
            logger.info("  SOLD: +$%.2f USDC", usdc)
            sold += 1
        else:
            logger.warning("  FAILED to sell %s", title)
            failed += 1

        time.sleep(2)  # rate limit

    if not DRY_RUN:
        time.sleep(3)
        bal_after = get_wallet_balance()
        logger.info("=== Done: %d sold, %d failed", sold, failed)
        logger.info("=== USDC before: $%.2f  after: $%.2f  recovered: +$%.2f",
                     bal_before, bal_after, bal_after - bal_before)


if __name__ == "__main__":
    main()
