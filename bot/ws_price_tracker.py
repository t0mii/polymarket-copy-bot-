"""
Real-time price tracker via Polymarket CLOB WebSocket.
Subscribes to open position markets and provides instant live prices.
"""
import json
import logging
import threading
import time

import requests
import websocket

logger = logging.getLogger(__name__)

CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_API = "https://gamma-api.polymarket.com"
PING_INTERVAL_SEC = 15  # Server drops connection without periodic ping


class PriceTracker:
    """WebSocket-based real-time price tracker for open positions."""

    def __init__(self):
        self._prices = {}         # {token_id: float}  best bid
        self._asks = {}           # {token_id: float}  best ask
        self._condition_map = {}  # {condition_id: {"YES": token_id, "NO": token_id, ...}}
        self._lock = threading.Lock()
        self._ws = None
        self._ws_thread = None
        self._running = False
        self._keep_running = False
        self._pending_tokens = []  # tokens to subscribe on next reconnect

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Start the WebSocket connection in a background thread."""
        self._keep_running = True
        self._ws_thread = threading.Thread(
            target=self._run_loop, daemon=True, name="ws-price-tracker"
        )
        self._ws_thread.start()
        logger.info("WS price tracker started.")

    def stop(self):
        self._keep_running = False
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    @property
    def is_connected(self) -> bool:
        return self._running

    def get_price(self, condition_id: str, side: str) -> float | None:
        """Return cached WebSocket price for a condition+side, or None if unknown."""
        with self._lock:
            tokens = self._condition_map.get(condition_id, {})
            token_id = tokens.get(side.upper()) or tokens.get("YES")
            if token_id:
                return self._prices.get(token_id)
        return None

    def get_spread(self, condition_id: str, side: str) -> float | None:
        """Return bid/ask spread as fraction (e.g. 0.03 = 3%), or None if unknown."""
        with self._lock:
            tokens = self._condition_map.get(condition_id, {})
            token_id = tokens.get(side.upper()) or tokens.get("YES")
            if not token_id:
                return None
            bid = self._prices.get(token_id)
            ask = self._asks.get(token_id)
            if bid is None or ask is None or ask <= 0:
                return None
            return round(ask - bid, 4)

    def subscribe_condition(self, condition_id: str):
        """Non-blocking: resolve condition_id to token IDs and subscribe."""
        with self._lock:
            if condition_id in self._condition_map:
                return
        t = threading.Thread(
            target=self._resolve_and_subscribe,
            args=(condition_id,),
            daemon=True,
        )
        t.start()

    # ------------------------------------------------------------------
    # Internal: token resolution
    # ------------------------------------------------------------------

    def _resolve_and_subscribe(self, condition_id: str):
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={"conditionId": condition_id},
                timeout=10,
            )
            if not (resp.ok and resp.json()):
                return

            market = resp.json()[0]
            clob_ids = market.get("clobTokenIds", [])
            if isinstance(clob_ids, str):
                clob_ids = json.loads(clob_ids)

            outcomes = market.get("outcomes", "[]")
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)

            token_map = {}
            for i, tid in enumerate(clob_ids):
                key = outcomes[i].upper() if i < len(outcomes) else ("YES" if i == 0 else "NO")
                token_map[key] = str(tid)

            with self._lock:
                self._condition_map[condition_id] = token_map
                new_tokens = [t for t in token_map.values() if t not in self._prices]

            if new_tokens:
                self._subscribe_tokens(new_tokens)
                logger.debug("WS: Subscribed condition %s (%d tokens)", condition_id[:10], len(new_tokens))

        except Exception as e:
            logger.debug("WS: resolve failed for %s: %s", condition_id[:10], e)

    # ------------------------------------------------------------------
    # Internal: subscription
    # ------------------------------------------------------------------

    def _subscribe_tokens(self, token_ids: list):
        with self._lock:
            ws = self._ws
            running = self._running

        if not running or not ws:
            with self._lock:
                self._pending_tokens.extend(token_ids)
            return

        try:
            ws.send(json.dumps({
                "assets_ids": token_ids,
                "type": "market",
                "custom_feature_enabled": True,
            }))
        except Exception as e:
            logger.debug("WS: subscribe send error: %s", e)
            with self._lock:
                self._pending_tokens.extend(token_ids)

    # ------------------------------------------------------------------
    # Internal: WebSocket callbacks
    # ------------------------------------------------------------------

    def _on_open(self, ws):
        self._running = True
        logger.info("WS: Connected to Polymarket CLOB.")

        # Re-subscribe to all known tokens + any pending
        with self._lock:
            all_tokens = list({
                tid
                for tm in self._condition_map.values()
                for tid in tm.values()
            })
            all_tokens += self._pending_tokens
            self._pending_tokens.clear()
            all_tokens = list(set(all_tokens))

        if all_tokens:
            try:
                ws.send(json.dumps({
                    "assets_ids": all_tokens,
                    "type": "market",
                    "custom_feature_enabled": True,
                }))
                logger.info("WS: Subscribed to %d markets.", len(all_tokens))
            except Exception as e:
                logger.debug("WS: Re-subscribe error on open: %s", e)

        # Start PING loop to keep connection alive
        threading.Thread(target=self._ping_loop, daemon=True).start()

    def _on_message(self, ws, message):
        try:
            events = json.loads(message)
            if not isinstance(events, list):
                events = [events]
            for ev in events:
                self._handle_event(ev)
        except Exception:
            pass

    def _handle_event(self, ev):
        etype = ev.get("event_type", "")
        asset_id = ev.get("asset_id", "")
        if not asset_id:
            return

        # book event — may or may not have event_type field
        if etype in ("book", "") and "bids" in ev:
            bids = ev.get("bids", [])
            asks = ev.get("asks", [])
            with self._lock:
                if bids:
                    try:
                        self._prices[asset_id] = max(float(b["price"]) for b in bids)
                    except Exception:
                        pass
                if asks:
                    try:
                        self._asks[asset_id] = min(float(a["price"]) for a in asks)
                    except Exception:
                        pass
            return

        if etype == "price_change":
            price = ev.get("price") or ev.get("best_bid")
            if price is not None:
                with self._lock:
                    self._prices[asset_id] = float(price)
            ask = ev.get("best_ask") or ev.get("ask")
            if ask is not None:
                with self._lock:
                    self._asks[asset_id] = float(ask)

        elif etype == "last_trade_price":
            price = ev.get("price")
            if price is not None:
                with self._lock:
                    self._prices[asset_id] = float(price)

        elif etype == "best_bid_ask":
            bid = ev.get("best_bid") or ev.get("bid")
            if bid is not None:
                with self._lock:
                    self._prices[asset_id] = float(bid)
            ask = ev.get("best_ask") or ev.get("ask")
            if ask is not None:
                with self._lock:
                    self._asks[asset_id] = float(ask)

    def _on_error(self, ws, error):
        logger.warning("WS: Error: %s", error)

    def _on_close(self, ws, close_status_code, close_msg):
        self._running = False
        logger.info("WS: Connection closed (code=%s).", close_status_code)

    def _ping_loop(self):
        """Send PING every 10s to keep connection alive."""
        while self._running:
            time.sleep(PING_INTERVAL_SEC)
            if not self._running:
                break
            try:
                if self._ws:
                    self._ws.send("ping")
            except Exception:
                break

    # ------------------------------------------------------------------
    # Internal: reconnect loop
    # ------------------------------------------------------------------

    def _run_loop(self):
        while self._keep_running:
            try:
                self._ws = websocket.WebSocketApp(
                    CLOB_WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever()
            except Exception as e:
                logger.warning("WS: Unexpected error in run_forever: %s", e)

            if self._keep_running:
                from config import WS_RECONNECT_SECS
                logger.info("WS: Reconnecting in %ds...", WS_RECONNECT_SECS)
                time.sleep(WS_RECONNECT_SECS)


# Global singleton — imported by copy_trader and main
price_tracker = PriceTracker()
