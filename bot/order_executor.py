"""
Order Executor - Echte Polymarket CLOB Orders.
Kauft und verkauft Shares auf Polymarket mit echtem Geld.
"""
import logging
import time

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, MarketOrderArgs, OrderType
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY, SELL

import config

logger = logging.getLogger(__name__)

_client: ClobClient | None = None


def _get_client() -> ClobClient:
    """CLOB Client initialisieren (Singleton).

    signature_type=1 = Polymarket Proxy Wallet (dort liegt das USDC).
    """
    global _client
    if _client is None:
        if not config.POLYMARKET_PRIVATE_KEY:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY nicht gesetzt!")
        _client = ClobClient(
            host="https://clob.polymarket.com",
            key=config.POLYMARKET_PRIVATE_KEY,
            chain_id=POLYGON,
            signature_type=1,
            funder=config.POLYMARKET_FUNDER,
        )
        # API Credentials ableiten (passend zum Private Key)
        creds = _client.create_or_derive_api_creds()
        _client.set_api_creds(creds)
        logger.info("CLOB Client initialisiert: %s (sig_type=1)", _client.get_address())
    return _client


def get_token_id(condition_id: str, side: str) -> str | None:
    """Token-ID fuer eine Seite eines Marktes holen.

    Polymarket hat pro Markt 2 Tokens (z.B. YES/NO oder TeamA/TeamB).
    Die API gibt tokens=[{outcome: "...", token_id: "..."}, ...] zurueck.
    Wir matchen anhand des outcome-Namens (case-insensitive).
    """
    try:
        client = _get_client()
        market = client.get_market(condition_id)
        tokens = market.get("tokens", [])
        if not tokens:
            logger.error("Keine Tokens fuer condition_id %s", condition_id[:20])
            return None

        # Exakter Match (case-insensitive)
        for t in tokens:
            if t.get("outcome", "").lower() == side.lower():
                return t["token_id"]

        # YES/NO Fallback
        side_upper = side.upper()
        if side_upper == "YES" and len(tokens) >= 1:
            return tokens[0]["token_id"]
        if side_upper == "NO" and len(tokens) >= 2:
            return tokens[1]["token_id"]

        # Erster Token als letzter Fallback
        logger.warning("Kein Token-Match fuer side='%s' in %s — nehme erstes Token",
                       side, [t.get("outcome") for t in tokens])
        return tokens[0]["token_id"]
    except Exception as e:
        logger.error("Fehler beim Holen der Token-ID: %s", e)
        return None


def buy_shares(condition_id: str, side: str, amount_usd: float, price: float) -> dict | None:
    """Market-Buy auf Polymarket ausfuehren.

    Args:
        condition_id: Markt Condition-ID
        side: Seite (z.B. "YES", "Katie Volynets", etc.)
        amount_usd: Betrag in USD (USDC)
        price: Max-Preis pro Share (Limit)

    Returns:
        Order-Response oder None bei Fehler
    """
    try:
        client = _get_client()
        token_id = get_token_id(condition_id, side)
        if not token_id:
            logger.error("Kein Token fuer %s / %s", condition_id[:20], side)
            return None

        # Fee Rate holen (braucht token_id), default 200bps = 2%
        fee_rate = 200
        try:
            fee_rate = int(client.get_fee_rate_bps(token_id))
        except Exception:
            logger.debug("Fee rate lookup failed, using default 200bps")

        # Market Order: price mit +3c Slippage damit Order eher füllt
        limit_price = round(min(price + 0.03, 0.99), 2)

        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=round(amount_usd, 2),
            side=BUY,
            price=limit_price,
            fee_rate_bps=fee_rate,
        )

        signed_order = client.create_market_order(order_args)
        response = client.post_order(signed_order, OrderType.FOK)

        # Check ob Order tatsaechlich gefuellt wurde
        success = False
        if isinstance(response, dict):
            status = (response.get("status") or response.get("orderStatus") or "").lower()
            if status in ("matched", "filled", "live"):
                success = True
            elif status == "delayed":
                # "delayed" = queued, may or may not fill. Verify after short wait.
                time.sleep(3)
                try:
                    params = BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=token_id)
                    bal_after = float(client.get_balance_allowance(params).get("balance", 0)) / 1_000_000
                    success = bal_after > 0.1
                    logger.info("ORDER VERIFY: delayed → %s (balance=%.2f shares)",
                                "FILLED" if success else "NOT FILLED", bal_after)
                except Exception:
                    success = False
                    logger.warning("ORDER VERIFY: could not check balance, treating as failed")
        elif response:
            success = True

        logger.info("ORDER BUY: $%.2f @ %.0fc (limit %.0fc) | %s | %s | %s",
                    amount_usd, price * 100, limit_price * 100, side,
                    "FILLED" if success else "REJECTED", response)
        return response if success else None

    except Exception as e:
        logger.error("ORDER FEHLER (BUY): %s | %s / %s / $%.2f", e, condition_id[:20], side, amount_usd)
        return None


def sell_shares(condition_id: str, side: str, price: float) -> dict | None:
    """Alle Shares einer Position verkaufen.

    Args:
        condition_id: Markt Condition-ID
        side: Seite
        price: Min-Preis pro Share (Limit)

    Returns:
        Order-Response oder None bei Fehler
    """
    try:
        client = _get_client()
        token_id = get_token_id(condition_id, side)
        if not token_id:
            return None

        # Aktuelle Balance fuer dieses Token holen
        params = BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=token_id)
        balance = client.get_balance_allowance(params)
        raw_balance = float(balance.get("balance", 0)) if isinstance(balance, dict) else 0
        # Conditional token balance — API returns raw units, always divide
        shares = raw_balance / 1_000_000

        if shares <= 0:
            logger.info("SELL: Keine Shares fuer %s / %s", condition_id[:20], side)
            return None

        # Fee Rate (braucht token_id)
        fee_rate = 0
        try:
            fee_rate = int(client.get_fee_rate_bps(token_id))
        except Exception:
            pass

        # Market Sell — price muss zwischen 0.001 und 0.999 liegen
        sell_price = round(min(max(price - 0.01, 0.01), 0.99), 2)
        sell_amount = round(shares * sell_price, 2)
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=sell_amount,
            side=SELL,
            price=sell_price,
            fee_rate_bps=fee_rate,
        )

        signed_order = client.create_market_order(order_args)
        response = client.post_order(signed_order, OrderType.FOK)

        logger.info("ORDER SELL: %.2f shares @ %.0fc | %s | Response: %s",
                    shares, price * 100, side, response)
        return response

    except Exception as e:
        logger.error("ORDER FEHLER (SELL): %s | %s / %s", e, condition_id[:20], side)
        return None


def get_wallet_balance() -> float:
    """USDC-Balance der Wallet abfragen."""
    try:
        client = _get_client()
        params = BalanceAllowanceParams(asset_type="COLLATERAL")
        collateral = client.get_balance_allowance(params)
        raw = float(collateral.get("balance", 0)) if isinstance(collateral, dict) else 0
        # USDC hat 6 Dezimalstellen (1000000 = $1.00)
        return raw / 1_000_000 if raw > 1000 else raw
    except Exception as e:
        logger.error("Fehler beim Abfragen der Balance: %s", e)
        return 0.0


def test_connection() -> bool:
    """Verbindung testen."""
    try:
        client = _get_client()
        ok = client.get_ok()
        addr = client.get_address()
        logger.info("CLOB OK: %s | Wallet: %s", ok, addr)
        return ok == "OK"
    except Exception as e:
        logger.error("CLOB Verbindungstest fehlgeschlagen: %s", e)
        return False
