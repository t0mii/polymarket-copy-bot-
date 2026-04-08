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


def get_fee_rate(condition_id: str, side: str) -> int:
    """Get fee rate in bps for a market. Returns 0 if lookup fails."""
    try:
        client = _get_client()
        token_id = get_token_id(condition_id, side)
        if not token_id:
            return 0
        return int(client.get_fee_rate_bps(token_id))
    except Exception:
        return 0


def _get_token_balance(client, token_id: str) -> float:
    """Query conditional token balance (number of shares held)."""
    try:
        params = BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=token_id)
        bal = client.get_balance_allowance(params)
        raw = float(bal.get("balance", 0)) if isinstance(bal, dict) else 0
        return raw / 1_000_000
    except Exception:
        return 0.0


def _build_fill_result(bal_before_usdc: float, shares_before: float,
                       token_id: str, fee_rate: int, price: float,
                       amount_usd: float, response) -> dict:
    """Measure actual fill by checking USDC + token balance deltas."""
    client = _get_client()
    usdc_spent = amount_usd  # fallback
    shares_bought = 0.0
    effective_price = price  # fallback = limit_price (with slippage)

    try:
        time.sleep(config.FILL_VERIFY_DELAY_SECS)
        bal_after_usdc = get_wallet_balance()

        _usdc_delta = bal_before_usdc - bal_after_usdc
        if _usdc_delta > config.MIN_FILL_AMOUNT:
            usdc_spent = round(_usdc_delta, 4)

        # Token balance: retry with backoff (settlement takes 3-5 seconds)
        _shares_delta = 0
        for _attempt in range(5):
            shares_after = _get_token_balance(client, token_id)
            _shares_delta = shares_after - shares_before
            if _shares_delta > 0:
                break
            time.sleep(1 + _attempt)  # 1s, 2s, 3s, 4s, 5s backoff

        if _shares_delta > 0:
            shares_bought = round(_shares_delta, 6)
            effective_price = round(usdc_spent / shares_bought, 6)
        else:
            # Fallback: estimate shares from USDC spent and limit price
            # limit_price (= price param) already includes slippage, so this is reasonable
            shares_bought = round(usdc_spent / price, 6) if price > 0 else 0
            effective_price = price
            logger.debug("Token balance unchanged, using limit_price %.4f as eff_price", price)

        logger.info("FILL DETAILS: spent=$%.2f shares=%.4f eff_price=%.4f fee=%dbps",
                    usdc_spent, shares_bought, effective_price, fee_rate)
    except Exception as e:
        logger.debug("Fill verification failed, using fallbacks: %s", e)

    return {
        "status": "filled",
        "usdc_spent": usdc_spent,
        "shares_bought": shares_bought,
        "effective_price": effective_price,
        "fee_rate_bps": fee_rate,
        "token_id": token_id,
        "_raw": response,
    }


def buy_shares(condition_id: str, side: str, amount_usd: float, price: float) -> dict | None:
    """Market-Buy auf Polymarket ausfuehren.

    Args:
        condition_id: Markt Condition-ID
        side: Seite (z.B. "YES", "Katie Volynets", etc.)
        amount_usd: Betrag in USD (USDC)
        price: Max-Preis pro Share (Limit)

    Returns:
        Fill-Details dict with usdc_spent, shares_bought, effective_price,
        fee_rate_bps, token_id — or None on failure.
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

        # Get USDC + token balance BEFORE order for fill verification
        bal_before_usdc = 0
        shares_before = 0.0
        try:
            bal_before_usdc = get_wallet_balance()
            shares_before = _get_token_balance(client, token_id)
        except Exception:
            pass

        # Market Order: try with increasing slippage until filled
        # IMPORTANT: never retry after "delayed" — the queued order might still fill,
        # and a retry would create a SECOND order that also fills (double spend).
        _buy_slips = [float(x) for x in config.BUY_SLIPPAGE_LEVELS.split(",")]
        for slippage in _buy_slips:
            limit_price = round(min(price + slippage, 0.99), 2)

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
                    # "delayed" = queued, may or may not fill.
                    # Check USDC balance multiple times (fill may arrive late).
                    # Do NOT retry — a second order would cause double spending.
                    success = False
                    for _dv_attempt in range(3):
                        _dv_wait = config.DELAYED_BUY_VERIFY_SECS if _dv_attempt == 0 else 4
                        time.sleep(_dv_wait)
                        try:
                            _usdc_after = get_wallet_balance()
                            _usdc_delta = bal_before_usdc - _usdc_after
                            if _usdc_delta > config.MIN_FILL_AMOUNT:
                                success = True
                                logger.info("ORDER VERIFY: delayed → FILLED (attempt %d, USDC before=%.2f after=%.2f spent=%.2f)",
                                            _dv_attempt + 1, bal_before_usdc, _usdc_after, _usdc_delta)
                                break
                        except Exception:
                            logger.warning("ORDER VERIFY: could not check USDC balance (attempt %d)", _dv_attempt + 1)
                    if not success:
                        logger.info("ORDER VERIFY: delayed → NOT FILLED after %d checks", 3)
                    # Whether filled or not, do NOT retry after delayed — return result
                    if success:
                        logger.info("ORDER BUY: $%.2f @ %.0fc (limit %.0fc +%.0fc slip) | %s | FILLED (delayed)",
                                    amount_usd, price * 100, limit_price * 100, slippage * 100, side)
                        return _build_fill_result(bal_before_usdc, shares_before,
                                                  token_id, fee_rate, limit_price, amount_usd, response)
                    else:
                        logger.warning("ORDER BUY: delayed order did not fill after %ds — giving up (no retry to prevent double spend)",
                                        config.DELAYED_BUY_VERIFY_SECS)
                        return None
            elif response:
                success = True

            if success:
                logger.info("ORDER BUY: $%.2f @ %.0fc (limit %.0fc +%.0fc slip) | %s | FILLED",
                            amount_usd, price * 100, limit_price * 100, slippage * 100, side)
                return _build_fill_result(bal_before_usdc, shares_before,
                                          token_id, fee_rate, limit_price, amount_usd, response)
            else:
                logger.info("ORDER BUY: attempt +%.0fc slip failed, %s",
                            slippage * 100, "retrying..." if slippage < _buy_slips[-1] else "giving up")

        # All attempts failed
        logger.warning("ORDER BUY FAILED: all slippage levels tried | %s / %s / $%.2f", condition_id[:20], side, amount_usd)
        return None

    except Exception as e:
        logger.error("ORDER FEHLER (BUY): %s | %s / %s / $%.2f", e, condition_id[:20], side, amount_usd)
        return None


def _build_sell_result(bal_before_usdc: float, shares_sold: float,
                       fee_rate: int, response) -> dict:
    """Measure actual USDC received from sell."""
    usdc_received = 0.0
    try:
        time.sleep(config.FILL_VERIFY_DELAY_SECS)
        bal_after_usdc = get_wallet_balance()
        _delta = bal_after_usdc - bal_before_usdc
        if _delta > 0:
            usdc_received = round(_delta, 4)
        logger.info("SELL FILL: received=$%.2f shares_sold=%.4f fee=%dbps",
                    usdc_received, shares_sold, fee_rate)
    except Exception as e:
        logger.debug("Sell fill verification failed: %s", e)

    return {
        "status": "filled",
        "usdc_received": usdc_received,
        "shares_sold": shares_sold,
        "fee_rate_bps": fee_rate,
        "_raw": response,
    }


def sell_shares(condition_id: str, side: str, price: float) -> dict | None:
    """Alle Shares einer Position verkaufen.

    Args:
        condition_id: Markt Condition-ID
        side: Seite
        price: Min-Preis pro Share (Limit)

    Returns:
        Fill-Details dict with usdc_received, shares_sold, fee_rate_bps
        — or None on failure.
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

        # Fee Rate (braucht token_id), default 200bps = 2%
        fee_rate = 200
        try:
            fee_rate = int(client.get_fee_rate_bps(token_id))
        except Exception:
            logger.debug("Fee rate lookup failed for sell, using default 200bps")

        # Get USDC balance BEFORE sell for fill verification
        bal_before_usdc = 0
        try:
            bal_before_usdc = get_wallet_balance()
        except Exception:
            pass

        # Market Sell with slippage retry (like buy) — try increasing slippage until filled
        # IMPORTANT: never retry after "delayed" to prevent double sell
        _sell_slips = [float(x) for x in config.SELL_SLIPPAGE_LEVELS.split(",")]
        for slippage in _sell_slips:
            sell_price = round(min(max(price - slippage, 0.01), 0.99), 2)
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

            success = False
            if isinstance(response, dict):
                status = (response.get("status") or response.get("orderStatus") or "").lower()
                if status in ("matched", "filled", "live"):
                    success = True
                elif status == "delayed":
                    # "delayed" = queued, may or may not fill. Do NOT retry.
                    time.sleep(config.DELAYED_SELL_VERIFY_SECS)
                    # Check if shares are gone (= sold)
                    try:
                        _params2 = BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=token_id)
                        _bal2 = client.get_balance_allowance(_params2)
                        _raw2 = float(_bal2.get("balance", 0)) if isinstance(_bal2, dict) else 0
                        _shares2 = _raw2 / 1_000_000
                        success = _shares2 < shares * config.SELL_VERIFY_THRESHOLD
                        logger.info("SELL VERIFY: delayed → %s (shares before=%.2f after=%.2f)",
                                    "FILLED" if success else "NOT FILLED", shares, _shares2)
                    except Exception:
                        success = False
                    if success:
                        logger.info("ORDER SELL: %.2f shares @ %.0fc (limit %.0fc -%.0fc slip) | %s | FILLED (delayed)",
                                    shares, price * 100, sell_price * 100, slippage * 100, side)
                        return _build_sell_result(bal_before_usdc, shares, fee_rate, response)
                    else:
                        logger.warning("ORDER SELL: delayed order did not fill after %ds — giving up (no retry to prevent double sell)",
                                        config.DELAYED_SELL_VERIFY_SECS)
                        return None
            elif response:
                success = True

            if success:
                logger.info("ORDER SELL: %.2f shares @ %.0fc (limit %.0fc -%.0fc slip) | %s | FILLED",
                            shares, price * 100, sell_price * 100, slippage * 100, side)
                return _build_sell_result(bal_before_usdc, shares, fee_rate, response)
            else:
                logger.info("ORDER SELL: attempt -%.0fc slip failed, %s",
                            slippage * 100, "retrying..." if slippage < _sell_slips[-1] else "giving up")

        # All attempts failed
        logger.warning("ORDER SELL FAILED: all slippage levels tried | %s / %s / %.2f shares",
                        condition_id[:20], side, shares)
        return None

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
        return raw / 1_000_000
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
