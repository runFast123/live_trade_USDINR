"""Tests for the live websocket feed. No socket is opened.

The FIX payloads here are copied from the real feed, which is the only place
the tag numbers are documented at all.
"""
from __future__ import annotations

import time
import unittest

from rollover import feed as feedmod
from rollover.config import RollConfig
from rollover.feed import LiveFeed, Tick
from rollover.money import D
from rollover.quotes import QuoteError, QuoteReader

# A real touchline message for USDINR26SEPFUT, trimmed to the tags used.
LIVE_209 = {
    "63": "FIX3.0", "64": "209", "1": "13", "7": "1769",
    "74": "2026-09-17 145238", "73": "2026-09-17 145037",
    "2": "15", "3": "959475000",          # best bid qty, best bid price
    "5": "29", "6": "959600000",          # best ask qty, best ask price
    "8": "959600000", "79": "643253", "88": "2942758",
    "399": "10000000",                     # price divisor
    "380": "93.0800-98.8350",
    "393": "+0.0025",                      # net change, NOT the tick size
}

FAR_209 = dict(LIVE_209, **{"7": "1584", "3": "962125000", "6": "962475000",
                            "2": "59", "5": "9"})


class StubLog:
    def info(self, m): pass
    def warn(self, m): pass
    def error(self, m): pass


def make_feed(tokens=("1769", "1584")):
    f = LiveFeed(RollConfig(), StubLog())
    f.tokens = list(tokens)
    return f


class TestTagDecoding(unittest.TestCase):
    def test_the_touchline_tags_decode_to_the_right_prices(self):
        tick = Tick("1769", LIVE_209, time.monotonic())
        self.assertTrue(tick.usable)
        self.assertEqual(D(tick.bid) / D(tick.divisor), D("95.9475"))
        self.assertEqual(D(tick.ask) / D(tick.divisor), D("95.9600"))
        self.assertEqual(tick.bid_qty, "15")
        self.assertEqual(tick.ask_qty, "29")
        self.assertEqual(tick.feed_time, "2026-09-17 145238")

    def test_tag_393_is_not_treated_as_a_tick_size(self):
        """It is the net change: it reads +0.0000 on an unmoved contract."""
        tick = Tick("1769", LIVE_209, time.monotonic())
        self.assertNotIn("393", (feedmod.TAG_BID, feedmod.TAG_ASK,
                                 feedmod.TAG_DIVISOR))
        self.assertIsNone(getattr(tick, "tick", None))

    def test_a_message_missing_a_price_is_not_usable(self):
        broken = dict(LIVE_209)
        broken.pop("3")
        self.assertFalse(Tick("1769", broken, time.monotonic()).usable)


class TestFeedState(unittest.TestCase):
    def setUp(self):
        self.feed = make_feed()

    def test_a_touchline_message_is_stored(self):
        self.feed._on_message({"Raw": LIVE_209})
        tick = self.feed.tick("1769")
        self.assertIsNotNone(tick)
        self.assertEqual(tick.bid, "959475000")

    def test_messages_for_other_tokens_are_ignored(self):
        self.feed._on_message({"Raw": dict(LIVE_209, **{"7": "9999"})})
        self.assertIsNone(self.feed.tick("9999"))

    def test_non_touchline_messages_are_ignored(self):
        self.feed._on_message({"Raw": dict(LIVE_209, **{"64": "128"})})
        self.assertIsNone(self.feed.tick("1769"))

    def connect(self):
        """Stand in for an open socket."""
        class Socket:
            _connected = True
        self.feed._socket = Socket()
        self.feed._connected = True

    def test_health_needs_every_leg(self):
        self.connect()
        self.feed._on_message({"Raw": LIVE_209})
        self.assertFalse(self.feed.healthy(["1769", "1584"]))
        self.feed._on_message({"Raw": FAR_209})
        self.assertTrue(self.feed.healthy(["1769", "1584"]))

    def test_a_quiet_book_is_still_healthy(self):
        """Silence on a push feed means nothing moved, not that data is stale."""
        self.connect()
        self.feed._on_message({"Raw": LIVE_209})
        self.feed._on_message({"Raw": FAR_209})
        with self.feed._lock:
            for tick in self.feed._ticks.values():
                tick.at -= 45          # no ticks for 45 seconds
        self.assertTrue(self.feed.healthy(["1769", "1584"], max_silence=120.0))

    def test_a_long_silence_is_doubted(self):
        self.connect()
        self.feed._on_message({"Raw": LIVE_209})
        self.feed._on_message({"Raw": FAR_209})
        self.feed._last_any -= 300
        self.assertFalse(self.feed.healthy(["1769", "1584"], max_silence=120.0))

    def test_a_dropped_socket_is_not_healthy(self):
        self.connect()
        self.feed._on_message({"Raw": LIVE_209})
        self.feed._on_message({"Raw": FAR_209})
        self.feed._socket._connected = False
        self.assertFalse(self.feed.healthy(["1769", "1584"]))

    def test_nothing_is_healthy_before_a_message_arrives(self):
        self.connect()
        self.assertFalse(self.feed.healthy(["1769", "1584"]))


class TestQuotesFromTheFeed(unittest.TestCase):
    """The websocket path goes through the same validation as the REST path."""

    def setUp(self):
        self.cfg = RollConfig(near_token="1769", far_token="1584")
        self.reader = QuoteReader(client=None, cfg=self.cfg)

        class Info:
            price_divisor = D("10000000")
            tick = D("0.0025")
            low_range = D("93.08")
            high_range = D("99.41")

        self.reader.set_instruments({"1769": Info(), "1584": Info()})
        self.feed = make_feed()
        self.feed._on_message({"Raw": LIVE_209})
        self.feed._on_message({"Raw": FAR_209})

    def test_prices_come_out_in_rupees(self):
        quotes = self.reader.from_feed(self.feed.ticks(), ["1769", "1584"])
        self.assertEqual(quotes["1769"].bid, D("95.9475"))
        self.assertEqual(quotes["1769"].ask, D("95.9600"))
        self.assertEqual(quotes["1584"].bid, D("96.2125"))
        self.assertEqual(quotes["1584"].ask, D("96.2475"))

    def test_the_declared_divisor_is_used(self):
        quotes = self.reader.from_feed(self.feed.ticks(), ["1769", "1584"])
        self.assertEqual(quotes["1769"].divisor, D("10000000"))

    def test_sizes_carry_through(self):
        quotes = self.reader.from_feed(self.feed.ticks(), ["1769", "1584"])
        self.assertEqual(quotes["1769"].bid_qty, 15)
        self.assertEqual(quotes["1584"].ask_qty, 9)

    def test_a_leg_with_no_tick_is_left_out_with_a_reason(self):
        """It used to raise for the whole batch, which blinded the sections
        whose legs were fine. A leg that cannot be read is ABSENT, so no
        section can price against it -- which is where the safety was all
        along."""
        got = self.reader.from_feed({"1769": self.feed.tick("1769")},
                                    ["1769", "1584"])
        self.assertIn("1769", got)
        self.assertNotIn("1584", got)
        self.assertIn("no live tick", self.reader.problems["1584"])

    def test_an_empty_side_produces_no_quote_for_that_leg(self):
        self.feed._on_message({"Raw": dict(LIVE_209, **{"3": "0"})})
        got = self.reader.from_feed(self.feed.ticks(), ["1769", "1584"])
        self.assertNotIn("1769", got)
        self.assertIn("no two-sided market", self.reader.problems["1769"])

    def test_nothing_readable_at_all_still_raises(self):
        with self.assertRaises(QuoteError):
            self.reader.from_feed({}, ["1769", "1584"])

    def test_a_crossed_book_is_refused(self):
        self.feed._on_message({"Raw": dict(LIVE_209, **{"3": "959700000"})})
        with self.assertRaises(QuoteError):
            self.reader.from_feed(self.feed.ticks(), ["1769", "1584"])

    def test_a_price_outside_the_band_is_refused(self):
        self.feed._on_message({"Raw": dict(LIVE_209,
                                           **{"3": "880000000", "6": "880050000"})})
        with self.assertRaises(QuoteError):
            self.reader.from_feed(self.feed.ticks(), ["1769", "1584"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
