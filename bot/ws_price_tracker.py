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

    _MAX_PRICE_AGE = 120  # seconds before a cached price is considered stale

    def __init__(self):
        self._prices = {}         # {token_id: float}  best bid
        self._asks = {}           # {token_id: float}  best ask
        self._prices_ts = {}      # {token_id: float}  timestamp of last price update
        self._price_history = {}  # {token_id: [(timestamp, price), ...]}  last 10min
        self._condition_map = {}  # {condition_id: {"YES": token_id, "NO": token_id, ...}}
        self._lock = threading.Lock()
        self._ws = None
        self._ws_thread = None
        self._running = False
        self._keep_running = False
        self._pending_tokens = []  # tokens to subscribe on next reconnect
        self._consecutive_failures = 0  # for exponential backoff
        self._last_successful_event_ts = 0  # reset failure counter on successful event

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
        """Return cached WebSocket price for a condition+side, or None if unknown/stale."""
        with self._lock:
            tokens = self._condition_map.get(condition_id, {})
            token_id = tokens.get(side.upper())
            if token_id:
                price = self._prices.get(token_id)
                if price is not None:
                    age = time.time() - self._prices_ts.get(token_id, 0)
                    if age < self._MAX_PRICE_AGE:
                        return price
                    # Price too old, treat as unknown
        return None

    def get_spread(self, condition_id: str, side: str) -> float | None:
        """Return bid/ask spread as fraction (e.g. 0.03 = 3%), or None if unknown."""
        with self._lock:
            tokens = self._condition_map.get(condition_id, {})
            token_id = tokens.get(side.upper())
            if not token_id:
                return None
            bid = self._prices.get(token_id)
            ask = self._asks.get(token_id)
            if bid is None or ask is None or ask <= 0:
                return None
            return round(ask - bid, 4)

    def get_momentum(self, condition_id: str, side: str, window_secs: int = 300) -> float | None:
        """Price momentum over window. Returns pct change or None if insufficient data."""
        with self._lock:
            tokens = self._condition_map.get(condition_id, {})
            token_id = tokens.get(side.upper())
            if not token_id:
                return None
            history = list(self._price_history.get(token_id, []))
        if len(history) < 2:
            return None
        now = time.time()
        cutoff = now - window_secs
        # Find oldest price within or near the window
        old_entries = [(ts, p) for ts, p in history if ts <= cutoff + 30]
        if not old_entries:
            return None
        old_price = old_entries[-1][1]
        current_price = history[-1][1]
        if old_price <= 0:
            return None
        return (current_price - old_price) / old_price

    def _record_price_history(self, token_id: str, price: float):
        """Append price to history, prune entries older than 10 minutes."""
        now = time.time()
        if token_id not in self._price_history:
            self._price_history[token_id] = []
        self._price_history[token_id].append((now, price))
        # Prune old entries (>10min)
        cutoff = now - 600
        self._price_history[token_id] = [
            (ts, p) for ts, p in self._price_history[token_id] if ts >= cutoff
        ]

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
            self._last_successful_event_ts = time.time()
            for ev in events:
                self._handle_event(ev)
        except Exception as e:
            logger.debug("WS message parse error: %s", e)

    def _handle_event(self, ev):
        etype = ev.get("event_type", "")
        asset_id = ev.get("asset_id", "")
        if not asset_id:
            return

        _now = time.time()

        # book event — may or may not have event_type field
        if etype in ("book", "") and "bids" in ev:
            bids = ev.get("bids", [])
            asks = ev.get("asks", [])
            with self._lock:
                if bids:
                    try:
                        _bp = max(float(b["price"]) for b in bids)
                        self._prices[asset_id] = _bp
                        self._prices_ts[asset_id] = _now
                        self._record_price_history(asset_id, _bp)
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
                    self._prices_ts[asset_id] = _now
                    self._record_price_history(asset_id, float(price))
            ask = ev.get("best_ask") or ev.get("ask")
            if ask is not None:
                with self._lock:
                    self._asks[asset_id] = float(ask)

        elif etype == "last_trade_price":
            price = ev.get("price")
            if price is not None:
                with self._lock:
                    self._prices[asset_id] = float(price)
                    self._prices_ts[asset_id] = _now
                    self._record_price_history(asset_id, float(price))

        elif etype == "best_bid_ask":
            bid = ev.get("best_bid") or ev.get("bid")
            if bid is not None:
                with self._lock:
                    self._prices[asset_id] = float(bid)
                    self._prices_ts[asset_id] = _now
                    self._record_price_history(asset_id, float(bid))
            ask = ev.get("best_ask") or ev.get("ask")
            if ask is not None:
                with self._lock:
                    self._asks[asset_id] = float(ask)

    def _on_error(self, ws, error):
        # Routine connection drops are noise — log at DEBUG.
        # Only surface ERRORs if we've had many consecutive failures.
        if self._consecutive_failures >= 5:
            logger.warning("WS: Error (attempt %d): %s", self._consecutive_failures, error)
        else:
            logger.debug("WS: Error: %s", error)

    def _on_close(self, ws, close_status_code, close_msg):
        self._running = False
        logger.debug("WS: Connection closed (code=%s).", close_status_code)

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

    def _has_work_to_do(self) -> bool:
        """Return True if there are positions worth watching via WS.
        Avoids constant reconnect loops when there are no open trades.
        """
        with self._lock:
            if self._condition_map or self._pending_tokens:
                return True
        # Fall back to DB — query open copy_trades directly
        try:
            from database import db as _db
            with _db.get_connection() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM copy_trades WHERE status='open' AND condition_id != ''"
                ).fetchone()
                return (row[0] if row else 0) > 0
        except Exception:
            return True  # on DB error, assume yes (don't break the reconnect loop)

    def _run_loop(self):
        from config import WS_RECONNECT_SECS
        while self._keep_running:
            # Skip connect attempts if there's nothing to subscribe to — saves
            # the server from being pinged constantly when the bot has no open
            # positions. Check every ~30s whether work has appeared.
            if not self._has_work_to_do():
                logger.debug("WS: no active subscriptions, sleeping 30s before retry")
                time.sleep(30)
                continue

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
                logger.debug("WS: Unexpected error in run_forever: %s", e)

            if not self._keep_running:
                break

            # Did we receive any data during this session? If yes, reset backoff.
            if self._last_successful_event_ts > time.time() - 60:
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1

            # Exponential backoff capped at 60s. Avoids hammering a rate-limited
            # server — 10s → 20s → 40s → 60s → 60s → ...
            backoff = min(WS_RECONNECT_SECS * (2 ** min(self._consecutive_failures, 3)), 60)
            if self._consecutive_failures >= 3:
                logger.info("WS: Reconnecting in %ds (consecutive failures=%d)",
                            backoff, self._consecutive_failures)
            else:
                logger.debug("WS: Reconnecting in %ds", backoff)
            time.sleep(backoff)


# Global singleton — imported by copy_trader and main
price_tracker = PriceTracker()
