"""
Liquidity Check — prueft Orderbook-Tiefe bevor ein Trade kopiert wird.
Verhindert Einstieg in illiquide Maerkte wo man nicht mehr rauskommt.
"""
import logging
from bot.order_executor import _get_client, get_token_id

logger = logging.getLogger(__name__)

MIN_BOOK_DEPTH_USD = 50  # Mindestens $50 Liquiditaet auf unserer Seite
MIN_BOOK_DEPTH_FOR_SIZE = 3.0  # Unsere Order darf max 1/3 der Tiefe sein


def _get_attr_or_key(obj, name, default=None):
    """Support both attribute (OrderBookSummary) and dict ({"asks":[...]}) access."""
    if obj is None:
        return default
    if hasattr(obj, name):
        return getattr(obj, name)
    if isinstance(obj, dict):
        return obj.get(name, default)
    return default


def check_liquidity(condition_id, side, our_size):
    """Check ob genug Liquiditaet im Orderbook ist.
    Returns True wenn OK, False wenn zu duenn.

    Supports both py-clob-client OrderBookSummary (named tuple with .asks)
    and legacy dict format ({"asks": [...]}). Each level can be either
    OrderSummary (with .price/.size) or dict ({"price":..., "size":...}).
    """
    try:
        client = _get_client()
        token_id = get_token_id(condition_id, side)
        if not token_id:
            return True  # Cant check, allow trade

        # Get orderbook
        book = client.get_order_book(token_id)
        if not book:
            return True

        # Calculate depth on our buy side (asks = what we can buy)
        asks = _get_attr_or_key(book, "asks", []) or []  # PATCH-028: handle dict responses
        total_ask_depth = 0
        for level in asks:
            price = float(_get_attr_or_key(level, "price", 0) or 0)
            size = float(_get_attr_or_key(level, "size", 0) or 0)
            total_ask_depth += price * size

        if total_ask_depth < MIN_BOOK_DEPTH_USD:
            logger.info("[LIQUIDITY] Too thin: $%.0f depth < $%d min | %s",
                        total_ask_depth, MIN_BOOK_DEPTH_USD, condition_id[:16])
            return False

        # Our order should be max 1/3 of available depth
        if our_size > total_ask_depth / MIN_BOOK_DEPTH_FOR_SIZE:
            logger.info("[LIQUIDITY] Our size $%.2f > 1/3 of depth $%.0f | %s",
                        our_size, total_ask_depth, condition_id[:16])
            return False

        return True

    except Exception as e:
        logger.warning("[LIQUIDITY] Check failed, blocking for safety: %s", e)
        return False  # Block on error for safety
