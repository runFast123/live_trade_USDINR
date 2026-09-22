"""The websocket killed itself every ninety seconds, and the fallback blanked
the screen.

Reported as "No live quotes". Two faults behind it, both measured against the
live broker on 22 Sep rather than reasoned about:

  * the vendor client runs run_forever(ping_interval=30, ping_timeout=10).
    The Choice broadcast server never answers a WebSocket PING, so
    websocket-client raised "ping/pong timed out" and closed a connection
    that was delivering ticks a second earlier. Measured twice: drops 91
    seconds apart, all session. With ping_timeout=None the same socket ran
    the whole test without one close.

  * every drop put the app on the REST touchline for a few seconds, and that
    endpoint fails -- HTTP 500, "Timeout performing HGET MarketData", and
    {"MultipleTouchline": None} -- often enough to have blanked the screen
    several times an hour. One failed poll threw the whole tick away, so both
    leg cards read "no quote" and the roll cost vanished.

The second fix has to earn its safety: showing a held price must never let
one be traded. It cannot, because a held quote is dated from when the tick
ARRIVED, so gates' freshness check refuses it the moment it passes
max_quote_age_sec. That is asserted below, because it is the whole argument.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import date

from rollover.broker import InstrumentInfo
from rollover.config import RollConfig
from rollover.money import D
from rollover.quotes import QuoteError

NEAR, FAR = "1284", "1584"


class Log:
    def __init__(self):
        self.lines = []

    def info(self, m): self.lines.append(f"INFO {m}")
    def warn(self, m): self.lines.append(f"WARN {m}")
    def error(self, m): self.lines.append(f"ERROR {m}")
    def alert(self, m): self.lines.append(f"ALERT {m}")

    def text(self):
        return " ".join(self.lines)


class Broker:
    logged_in = True
    scrip_file_date = date.today()


class Tick:
    """The shape LiveFeed.ticks() hands back."""

    def __init__(self, token, bid, ask, at, qty=50):
        self.token = token
        self.bid, self.ask = bid, ask
        self.bid_qty = self.ask_qty = qty
        self.divisor = "10000000"
        self.at = at
        self.raw = {"7": token}

    @property
    def usable(self):
        return None not in (self.bid, self.ask, self.divisor)


class FakeFeed:
    """Stands in for the websocket. connected/healthy are set by each test."""

    def __init__(self):
        self._ticks = {}
        self.connected = False
        self._healthy = False

    def healthy(self, tokens, max_silence=120.0):
        return self._healthy

    def ticks(self):
        return dict(self._ticks)

    def stop(self):
        pass


class FakeReader:
    """A QuoteReader that answers from ticks, or fails on command."""

    def __init__(self, real):
        self.real = real
        self.fetch_error = None
        self.fetches = 0
        self.problems = {}

    def from_feed(self, ticks, tokens, at=None):
        return self.real.from_feed(ticks, tokens, at=at)

    def fetch(self, tokens):
        self.fetches += 1
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.real.from_feed(
            {t: Tick(t, "960000000", "960100000", 0.0) for t in tokens}, tokens)

    def set_instruments(self, instruments):
        self.real.set_instruments(instruments)


def contract(token, desc, expiry):
    return InstrumentInfo(token=token, symbol="USDINR", sec_desc=desc,
                          segment="13", lot_size=1000, expiry=expiry,
                          instrument="FUTCUR", price_divisor=D("10000000"),
                          tick=D("0.0025"), tick_units=D("25000"),
                          low_range=D("93"), high_range=D("99"))


class EngineCase(unittest.TestCase):
    def setUp(self):
        import time

        from rollover.engine import RollEngine
        from rollover.quotes import QuoteReader

        self.dir = tempfile.mkdtemp(prefix="feedfix_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.log = Log()
        self.now = time.monotonic

        cfg = RollConfig(near_token=NEAR, near_expiry="2026-10-28",
                         far_token=FAR, far_expiry="2026-11-26",
                         limit_bps_schedule={"1": "30", "2": "50"},
                         use_live_feed=True, record_market=False,
                         update_check=False, journal=False, dry_run=True,
                         max_quote_age_sec=3.0)
        self.engine = RollEngine(cfg, self.log, self.dir, broker=Broker())
        self.engine.sections[0].near = contract(NEAR, "USDINR26OCTFUT",
                                                date(2026, 10, 28))
        self.engine.sections[0].far = contract(FAR, "USDINR26NOVFUT",
                                               date(2026, 11, 26))
        real = QuoteReader(None, cfg)
        real.set_instruments({NEAR: self.engine.sections[0].near,
                              FAR: self.engine.sections[0].far})
        self.reader = FakeReader(real)
        self.engine.reader = self.reader
        self.feed = FakeFeed()
        self.engine.feed = self.feed

    def tick_both(self, age=0.0, **kw):
        at = self.now() - age
        self.feed._ticks = {NEAR: Tick(NEAR, "960000000", "960100000", at, **kw),
                            FAR: Tick(FAR, "962000000", "962100000", at, **kw)}


class TestTheFeedIsPreferredWhileItReconnects(EngineCase):
    def test_a_healthy_feed_is_used_and_nothing_is_polled(self):
        self.tick_both()
        self.feed._healthy = True
        got = self.engine._read_quotes([NEAR, FAR])
        self.assertEqual(sorted(got), [NEAR, FAR])
        self.assertEqual(self.engine.quote_source, "live feed")
        self.assertEqual(self.reader.fetches, 0)

    def test_a_reconnecting_feed_with_fresh_ticks_is_still_used(self):
        """The drop lasts a few seconds. Ticks from a second ago describe the
        book better than a cached REST snapshot, and far better than a REST
        call that fails."""
        self.tick_both(age=1.0)
        self.feed._healthy = False
        got = self.engine._read_quotes([NEAR, FAR])
        self.assertEqual(sorted(got), [NEAR, FAR])
        self.assertEqual(self.engine.quote_source, "live feed, reconnecting")
        self.assertEqual(self.reader.fetches, 0)

    def test_ticks_older_than_the_freshness_limit_fall_through_to_the_poll(self):
        self.tick_both(age=10.0)
        self.feed._healthy = False
        self.engine._read_quotes([NEAR, FAR])
        self.assertEqual(self.reader.fetches, 1)
        self.assertIn("polled", self.engine.quote_source)

    def test_a_leg_the_feed_has_never_sent_falls_through_to_the_poll(self):
        """Coverage matters as much as age: the touchline may have the leg the
        websocket has not sent, so a partial answer must not skip the poll."""
        self.tick_both()
        del self.feed._ticks[FAR]
        self.feed._healthy = False
        self.engine._read_quotes([NEAR, FAR])
        self.assertEqual(self.reader.fetches, 1)

    def test_the_feed_being_off_never_uses_held_ticks(self):
        self.engine.cfg.use_live_feed = False
        self.tick_both()
        self.engine._read_quotes([NEAR, FAR])
        self.assertEqual(self.reader.fetches, 1)


class TestAFailedPollDoesNotBlankTheScreen(EngineCase):
    def setUp(self):
        super().setUp()
        self.feed._healthy = False
        self.reader.fetch_error = RuntimeError(
            "HTTP Request failed: 500 Server Error: Internal Server Error "
            "for url: .../MultipleTouchline")

    def test_the_last_feed_prices_are_shown_instead_of_nothing(self):
        self.tick_both(age=10.0)
        got = self.engine._read_quotes([NEAR, FAR])
        self.assertEqual(sorted(got), [NEAR, FAR])
        self.assertEqual(self.engine.quote_source, "polled, not answering")

    def test_it_says_so_once_rather_than_failing_silently(self):
        self.tick_both(age=10.0)
        self.engine._read_quotes([NEAR, FAR])
        self.assertIn("not answering", self.log.text())
        self.assertIn("500", self.log.text())

    def test_with_no_held_ticks_it_still_raises(self):
        """Nothing to show is nothing to show. It must not invent a price."""
        self.feed._ticks = {}
        with self.assertRaises(QuoteError):
            self.engine._read_quotes([NEAR, FAR])

    def test_a_broker_http_failure_is_reported_as_a_quote_problem(self):
        """It used to escape as an unexpected error, which reads like a fault
        in this app rather than in the broker's endpoint."""
        self.feed._ticks = {}
        with self.assertRaises(QuoteError) as caught:
            self.engine._read_quotes([NEAR, FAR])
        self.assertIn("500", str(caught.exception))

    def test_a_quote_error_is_passed_through_unchanged(self):
        self.feed._ticks = {}
        self.reader.fetch_error = QuoteError("no prices to scale")
        with self.assertRaises(QuoteError) as caught:
            self.engine._read_quotes([NEAR, FAR])
        self.assertEqual(str(caught.exception), "no prices to scale")

    def test_the_warning_is_not_repeated_every_tick(self):
        """_tick used to clear the throttle after any successful read, and a
        fallback IS a successful read, so this warned once a second."""
        self.tick_both(age=10.0)
        for _ in range(5):
            self.engine._read_quotes([NEAR, FAR])
            if not self.engine._quotes_degraded:
                self.engine._last_complaint = ("", 0.0)
        # The source line is logged once too, by _set_source, and only once
        # because the source did not change again.
        self.assertEqual(self.log.text().count("touchline is not answering"), 1)
        self.assertEqual(
            self.log.text().count("Quotes now coming from the polled, not "
                                  "answering"), 1)

    def test_a_clean_read_clears_the_degraded_flag(self):
        self.tick_both(age=10.0)
        self.engine._read_quotes([NEAR, FAR])
        self.assertTrue(self.engine._quotes_degraded)
        self.reader.fetch_error = None
        self.engine._read_quotes([NEAR, FAR])
        self.assertFalse(self.engine._quotes_degraded)


class TestAHeldPriceCanBeSeenButNeverTraded(EngineCase):
    """The safety argument for showing held prices, asserted rather than
    claimed: they are dated from when the tick arrived, so the freshness gate
    refuses them."""

    def held(self, age):
        self.tick_both(age=age)
        self.feed._healthy = False
        self.reader.fetch_error = RuntimeError("touchline down")
        return self.engine._read_quotes([NEAR, FAR])

    def test_a_held_quote_carries_the_age_of_the_tick_not_of_the_read(self):
        got = self.held(9.0)
        self.assertGreaterEqual(got[NEAR].age(), 9.0)

    def test_the_oldest_leg_sets_the_age_shown(self):
        """So the number on screen is the worst of what is up there."""
        at = self.now()
        self.feed._ticks = {NEAR: Tick(NEAR, "960000000", "960100000", at - 1),
                            FAR: Tick(FAR, "962000000", "962100000", at - 20)}
        self.feed._healthy = False
        self.reader.fetch_error = RuntimeError("touchline down")
        got = self.engine._read_quotes([NEAR, FAR])
        self.assertGreaterEqual(got[NEAR].age(), 20.0)

    def test_the_freshness_gate_refuses_a_held_quote(self):
        from rollover import gates

        got = self.held(9.0)
        section = self.engine.sections[0]
        section.near_position_qty = 100000
        report = gates.evaluate(self.engine.cfg, section, got, None)
        blocked = {g.name: g for g in report.gates if not g.ok}
        self.assertIn("near quote fresh", blocked)
        self.assertIn("far quote fresh", blocked)
        self.assertFalse(report.ok)

    def test_a_reconnecting_quote_inside_the_limit_passes_it(self):
        """The same mechanism must not block the ordinary three-second blip."""
        from rollover import gates

        self.tick_both(age=1.0)
        self.feed._healthy = False
        got = self.engine._read_quotes([NEAR, FAR])
        section = self.engine.sections[0]
        report = gates.evaluate(self.engine.cfg, section, got, None)
        blocked = {g.name for g in report.gates if not g.ok}
        self.assertNotIn("near quote fresh", blocked)
        self.assertNotIn("far quote fresh", blocked)


class TestTheSocketIsNotKilledByItsOwnPing(unittest.TestCase):
    """The vendor's run_forever(ping_interval=30, ping_timeout=10) against a
    server that never pongs. Measured: a close every 91 seconds; with the
    deadline removed, no close at all in the same window."""

    class VendorLike:
        host = "wss://brd.choiceindia.co.in:4520"
        port = None
        _is_running = True

        def __init__(self):
            self.ws = None

        def _on_open(self, ws): pass
        def _on_message(self, ws, m): pass
        def _on_error(self, ws, e): pass
        def _on_close(self, ws, c, m): pass

    def feed(self):
        from rollover.feed import LiveFeed

        cfg = RollConfig(near_token=NEAR, far_token=FAR)
        return LiveFeed(cfg, Log())

    def run_forever_kwargs(self, socket):
        """Run the patched loop once and capture how run_forever was called."""
        import rollover.feed as feedlib

        captured = {}

        class App:
            def __init__(self, url, **handlers):
                captured["url"] = url
                captured["handlers"] = handlers

            def run_forever(self, **kw):
                captured.update(kw)
                socket._is_running = False

        real = feedlib.websocket if hasattr(feedlib, "websocket") else None
        import websocket as websocket_module
        original = websocket_module.WebSocketApp
        websocket_module.WebSocketApp = App
        try:
            socket._ws_run()
        finally:
            websocket_module.WebSocketApp = original
            del real
        return captured

    def test_the_pong_deadline_is_removed(self):
        socket = self.VendorLike()
        self.assertTrue(self.feed()._keep_alive(socket))
        got = self.run_forever_kwargs(socket)
        self.assertIsNone(got["ping_timeout"])

    def test_the_keepalive_ping_is_still_sent(self):
        """Removing the ping altogether would let a firewall drop the socket."""
        socket = self.VendorLike()
        self.feed()._keep_alive(socket)
        got = self.run_forever_kwargs(socket)
        self.assertGreater(got["ping_interval"], 0)

    def test_the_vendor_s_own_callbacks_are_kept(self):
        socket = self.VendorLike()
        self.feed()._keep_alive(socket)
        got = self.run_forever_kwargs(socket)
        handlers = got["handlers"]
        self.assertEqual(handlers["on_message"], socket._on_message)
        self.assertEqual(handlers["on_open"], socket._on_open)
        self.assertEqual(handlers["on_close"], socket._on_close)
        self.assertEqual(got["url"], socket.host)

    def test_an_unexpected_client_shape_is_left_alone(self):
        """A vendor upgrade that renames things must not stop the feed; it
        falls back to the vendor's own loop and says so."""
        feed = self.feed()

        class Other:
            pass

        other = Other()
        self.assertFalse(feed._keep_alive(other))
        self.assertFalse(hasattr(other, "_ws_run"))
        self.assertIn("keepalive", feed.log.text())

    def test_start_patches_before_the_socket_runs(self):
        """Patching after start_websocket would leave the first connection on
        the vendor's settings, which is the one that matters most."""
        import inspect

        from rollover.feed import LiveFeed
        source = inspect.getsource(LiveFeed.start)
        self.assertLess(source.index("_keep_alive"),
                        source.index("start_websocket()"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
