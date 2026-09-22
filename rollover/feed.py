"""The live price feed.

The REST touchline is a cached snapshot. Polling it three seconds apart returns
the identical payload, server timestamp included, and on a quiet far month it
has been observed thirteen minutes behind. Prices that stale are not something
to compute a roll cost from, let alone trade on.

The websocket is the real feed. It pushes a touchline message whenever the book
moves, and it carries the price scale with it.

The FIX tags below were read off the live feed and matched against REST at the
same instant, because the vendor documents none of them:

    1    segment              7    token
    2    best bid quantity    3    best bid price
    5    best ask quantity    6    best ask price
    8    last traded price   74    feed timestamp
  399    price divisor      380    the day's price band
   79    volume              88    open interest

Tag 393 looks like a tick size and is not: it is the net change against the
previous close, and it reads +0.0000 on a contract that has not moved.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

TAG_SEGMENT = "1"
TAG_TOKEN = "7"
TAG_BID_QTY = "2"
TAG_BID = "3"
TAG_ASK_QTY = "5"
TAG_ASK = "6"
TAG_LTP = "8"
TAG_FEED_TIME = "74"
TAG_DIVISOR = "399"

MSG_LOGON = "102"
MSG_TOUCHLINE = "209"
MSG_BEST_FIVE = "128"

#: Seconds between keepalive pings, and the wait before a reconnect. Both
#: match the vendor's own numbers; what differs is that no answer to a ping
#: is demanded. See LiveFeed._keep_alive.
PING_INTERVAL = 30
RECONNECT_WAIT = 3


class Tick:
    """One instrument's latest state, as the feed last sent it."""

    __slots__ = ("token", "bid", "ask", "bid_qty", "ask_qty", "divisor",
                 "feed_time", "at", "raw")

    def __init__(self, token: str, raw: dict, at: float):
        self.token = token
        self.raw = raw
        self.at = at
        self.bid = raw.get(TAG_BID)
        self.ask = raw.get(TAG_ASK)
        self.bid_qty = raw.get(TAG_BID_QTY)
        self.ask_qty = raw.get(TAG_ASK_QTY)
        self.divisor = raw.get(TAG_DIVISOR)
        self.feed_time = raw.get(TAG_FEED_TIME)

    def age(self, now: Optional[float] = None) -> float:
        return (time.monotonic() if now is None else now) - self.at

    @property
    def usable(self) -> bool:
        return None not in (self.bid, self.ask, self.divisor)


class LiveFeed:
    """Keeps the latest tick per token, and re-subscribes after a reconnect."""

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.client = None
        self.tokens: List[str] = []

        self._socket = None
        self._ticks: Dict[str, Tick] = {}
        self._lock = threading.Lock()
        self._connected = False
        self._subscribed_at = 0.0
        self._started = False
        self._last_any = 0.0        # any message at all, including other tokens

    # ------------------------------------------------------------------ state
    @property
    def connected(self) -> bool:
        return self._connected

    def tick(self, token: str) -> Optional[Tick]:
        with self._lock:
            return self._ticks.get(str(token))

    def ticks(self) -> Dict[str, Tick]:
        with self._lock:
            return dict(self._ticks)

    def healthy(self, tokens: List[str], max_silence: float = 120.0) -> bool:
        """True when the feed can be trusted as the current state of the book.

        A push feed says nothing while nothing changes, so silence is not
        staleness: the last tick still describes the book. What would be
        dangerous is a socket that has quietly died while we keep reading its
        last message, so health is the connection being up, both legs having
        been seen at least once, and something having arrived within
        max_silence.
        """
        if not self._connected or self._socket is None:
            return False
        if not getattr(self._socket, "_connected", False):
            return False

        with self._lock:
            for token in tokens:
                found = self._ticks.get(str(token))
                if found is None or not found.usable:
                    return False

        return (time.monotonic() - self._last_any) <= max_silence

    def last_tick_age(self, token: str) -> Optional[float]:
        """Seconds since this contract last moved, for display."""
        found = self.tick(token)
        return None if found is None else found.age()

    def silence(self) -> float:
        """Seconds since anything at all arrived on the socket."""
        return time.monotonic() - self._last_any if self._last_any else float("inf")

    # ------------------------------------------------------------- connection
    def _keep_alive(self, socket) -> bool:
        """Stop the library killing a connection that is working perfectly.

        The vendor's client runs ``run_forever(ping_interval=30,
        ping_timeout=10)``. The broadcast server never answers a WebSocket
        PING -- measured twice on the live socket, both times ending in
        ``WEBSOCKET ERROR => ping/pong timed out`` -- so websocket-client
        closes a connection that was delivering ticks a moment earlier,
        reconnects three seconds later, and does it again. Drops came 91
        seconds apart, all session.

        Each drop put the app on the REST touchline, and that is where the
        damage was: the touchline answers with an HTTP 500, a Redis timeout,
        or ``MultipleTouchline: None`` often enough that the screen went
        blank several times an hour.

        So the loop is run here instead, with the pong deadline removed. The
        pings still go out, which is what holds the connection open through a
        firewall; only the demand for an answer is dropped. A socket that has
        genuinely died is still caught -- by ``feed_max_silence`` above, and
        by the read failing.

        Returns False if the vendor client is not the shape expected, in
        which case its own loop runs unchanged.
        """
        import types

        try:
            import websocket
        except ImportError:                                  # pragma: no cover
            return False

        needed = ("host", "port", "_is_running", "_on_open", "_on_message",
                  "_on_error", "_on_close")
        if any(not hasattr(socket, name) for name in needed):
            self.log.warn("The price feed client is not the shape this app "
                          "knows; leaving its keepalive alone.")
            return False

        log = self.log

        def run(self) -> None:
            websocket.enableTrace(False)
            url = self.host
            if self.port and not url.startswith("ws"):
                url = f"wss://{self.host}:{self.port}"
            while self._is_running:
                try:
                    self.ws = websocket.WebSocketApp(
                        url, on_open=self._on_open, on_message=self._on_message,
                        on_error=self._on_error, on_close=self._on_close)
                    self.ws.run_forever(ping_interval=PING_INTERVAL,
                                        ping_timeout=None)
                except Exception as exc:                     # pragma: no cover
                    log.warn(f"Live feed connection error: {exc}")
                if self._is_running:
                    time.sleep(RECONNECT_WAIT)

        socket._ws_run = types.MethodType(run, socket)
        return True

    def start(self, client, tokens: List[str]) -> None:
        from choice_api import PriceFeedSocketClient

        if self._started:
            return
        self.client = client
        self.tokens = [str(t) for t in tokens]

        token = client.access_token or self.cfg.api_key
        self._socket = PriceFeedSocketClient(vendor_id=self.cfg.vendor_id,
                                             access_token=token)
        self._socket.on_message(self._on_message)
        self._keep_alive(self._socket)
        self._socket.start_websocket()
        self._started = True
        self.log.info("Live price feed starting.")

        # The socket logs on by itself; subscribing is ours to do, both now and
        # again after any reconnect.
        threading.Thread(target=self._subscribe_when_ready, daemon=True).start()

    def watch(self, tokens) -> bool:
        """Add tokens to the subscription, and subscribe them if we are up.

        A section added while the app is running needs its legs quoted, and
        start() returns early once the socket exists -- so without this the
        new section showed a dash in every column forever and the operator
        could not tell it from a dead market.

        Returns True when something new was added.
        """
        wanted = [str(t) for t in tokens if t]
        fresh = [t for t in wanted if t not in self.tokens]
        if not fresh:
            return False
        self.tokens.extend(fresh)
        self.log.info(f"Live feed also watching {', '.join(fresh)}.")
        if self._started and self._connected:
            try:
                for token in fresh:
                    self._socket.subscribe_touchline(
                        self.client.session_id, self.cfg.segment_id,
                        int(token))
                    time.sleep(0.2)
            except Exception as exc:
                # The periodic resubscribe will pick them up.
                self.log.warn(f"Live feed subscribe failed for the new "
                              f"token(s): {exc}")
        return True

    def _subscribe_when_ready(self) -> None:
        deadline = time.time() + 15
        while time.time() < deadline and self._started:
            if getattr(self._socket, "_connected", False):
                self._subscribe()
                return
            time.sleep(0.25)
        self.log.warn("Live feed did not connect; falling back to polling.")

    def _subscribe(self) -> None:
        if not self._socket or not self.client:
            return
        try:
            for token in self.tokens:
                self._socket.subscribe_touchline(self.client.session_id,
                                                 self.cfg.segment_id, int(token))
                time.sleep(0.2)
            self._subscribed_at = time.monotonic()
            self._connected = True
            self.log.info(f"Live feed subscribed to {', '.join(self.tokens)}.")
        except Exception as exc:
            self._connected = False
            self.log.warn(f"Live feed subscribe failed: {exc}")

    def stop(self) -> None:
        self._started = False
        self._connected = False
        if self._socket:
            try:
                self._socket.stop_websocket()
            except Exception:
                pass
            self._socket = None

    # -------------------------------------------------------------- messages
    def _on_message(self, message: dict) -> None:
        raw = message.get("Raw") or message
        code = raw.get("64")
        self._last_any = time.monotonic()

        if code == MSG_LOGON:
            # A logon after we were already up means the socket reconnected,
            # and subscriptions do not survive that.
            if self._subscribed_at:
                self.log.info("Live feed reconnected, re-subscribing.")
                self._connected = False
                threading.Thread(target=self._subscribe, daemon=True).start()
            return

        if code != MSG_TOUCHLINE:
            return

        token = raw.get(TAG_TOKEN)
        if token is None or str(token) not in self.tokens:
            return

        tick = Tick(str(token), raw, time.monotonic())
        if not tick.usable:
            return
        with self._lock:
            self._ticks[str(token)] = tick
        self._connected = True
