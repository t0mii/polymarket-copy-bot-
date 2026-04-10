"""Redeem all won positions via PolyWeb3Service.

Run: python redeem_positions.py
"""
import logging

from py_builder_relayer_client.client import RelayClient
from py_builder_signing_sdk.config import BuilderConfig
from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
from py_clob_client.client import ClobClient

from poly_web3 import RELAYER_URL, PolyWeb3Service

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    # CLOB client (same as order_executor)
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=config.POLYMARKET_PRIVATE_KEY,
        chain_id=137,
        signature_type=1,
        funder=config.POLYMARKET_FUNDER,
    )
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)

    # Relayer client for redeem
    relayer_client = RelayClient(
        RELAYER_URL,
        137,
        config.POLYMARKET_PRIVATE_KEY,
        BuilderConfig(
            local_builder_creds=BuilderApiKeyCreds(
                key=config.BUILDER_KEY,
                secret=config.BUILDER_SECRET,
                passphrase=config.BUILDER_PASSPHRASE,
            )
        ),
    )

    # Web3 service with redeem capability
    service = PolyWeb3Service(
        clob_client=client,
        relayer_client=relayer_client,
        rpc_url="https://polygon-bor.publicnode.com",
    )

    logger.info("Starting redeem_all...")
    result = service.redeem_all(batch_size=10)
    logger.info("Result: %s", result)
    if hasattr(result, 'error_list') and result.error_list:
        logger.warning("Failed items: %s", result.error_list)
        logger.warning("Retry condition IDs: %s", result.error_condition_ids)


if __name__ == "__main__":
    main()
