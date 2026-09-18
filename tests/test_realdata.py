"""Tests built from what today's Choice scrip master actually contains.

The numbers here are copied from the real CSV for segment 13:

    Token 1769  USDINR26SEPFUT  Expiry 28SEP26  Instrument FUTCUR
    Token 1584  USDINR26NOVFUT  Expiry 26NOV26  Instrument FUTCUR
    PriceDivisor 10000000   PriceTick 25000   MarketLot 1000
    LowPriceRange 930800000  HighPriceRange 988350000

Two things in here caused real failures before they were fixed: the expiry is
written as 01OCT26, and the price scale is 10000000 rather than 1 or 100.
"""
from __future__ import annotations

import time
import unittest
from datetime import date, datetime

from rollover import gates, rule
from rollover.broker import Broker, BrokerError, InstrumentInfo, parse_expiry
from rollover.config import RollConfig
from rollover.money import D
from rollover.quotes import Quote, QuoteError, QuoteReader, top_of_book

DIVISOR = D("10000000")


def real_instrument(token, sec_desc, expiry_text, low, high, instrument="FUTCUR"):
    """Build an InstrumentInfo the way Broker.instrument does, from raw columns."""
    return InstrumentInfo(
        token=token,
        symbol="USDINR",
        sec_desc=sec_desc,
        segment="13",
        lot_size=1000,
        expiry=parse_expiry(expiry_text),
        instrument=instrument,
        price_divisor=DIVISOR,
        tick=(D("25000") / DIVISOR),
        tick_units=D("25000"),
        low_range=(D(low) / DIVISOR),
        high_range=(D(high) / DIVISOR),
    )


def near_leg(**kw):
    return real_instrument("1769", "USDINR26SEPFUT", "28SEP26",
                           "930800000", "988350000", **kw)


def far_leg(**kw):
    return real_instrument("1584", "USDINR26NOVFUT", "26NOV26",
                           "936175000", "994075000", **kw)


class FakeSession:
    def __init__(self, near, far, **kw):
        self.near, self.far = near, far
        self.logged_in = kw.get("logged_in", True)
        self.market_open = kw.get("market_open", True)
        self.near_position_qty = kw.get("near_position_qty", 1000)
        self.clips_done_today = kw.get("clips_done_today", 0)
        self.in_flight = kw.get("in_flight", False)
        self.halted_reason = kw.get("halted_reason")
        self.scrip_file_date = kw.get("scrip_file_date", date(2026, 9, 17))
        self.quote_source = kw.get("quote_source", "live feed")


class TestScripMasterColumns(unittest.TestCase):
    def test_the_two_digit_expiry_format_parses(self):
        self.assertEqual(parse_expiry("28SEP26"), date(2026, 9, 28))
        self.assertEqual(parse_expiry("26NOV26"), date(2026, 11, 26))
        self.assertEqual(parse_expiry("01OCT26"), date(2026, 10, 1))
        self.assertEqual(parse_expiry("24FEB27"), date(2027, 2, 24))

    def test_the_declared_tick_matches_the_configured_one(self):
        self.assertEqual(near_leg().tick, D("0.0025"))
        self.assertEqual(near_leg().tick, RollConfig().tick_d)

    def test_circuit_limits_unscale_to_sane_rupees(self):
        info = near_leg()
        self.assertEqual(info.low_range, D("93.08"))
        self.assertEqual(info.high_range, D("98.835"))

    def test_futures_are_recognised_and_options_are_not(self):
        self.assertTrue(near_leg().is_futures)
        self.assertFalse(near_leg(instrument="OPTCUR").is_futures)

    def test_month_end_contracts_are_told_apart_from_weeklies(self):
        monthly = real_instrument("1769", "USDINR26SEPFUT", "28SEP26", "1", "2")
        weekly_digits = real_instrument("1883", "USDINR26918FUT", "18SEP26", "1", "2")
        weekly_letter = real_instrument("6893", "USDINR26O01FUT", "01OCT26", "1", "2")
        self.assertTrue(monthly.is_month_end)
        self.assertFalse(weekly_digits.is_month_end)
        self.assertFalse(weekly_letter.is_month_end)


class TestQuotesAtTheRealScale(unittest.TestCase):
    """The feed is assumed to use the same PriceDivisor the scrip master states."""

    def setUp(self):
        self.cfg = RollConfig(near_token="1769", far_token="1584")
        self.days = (far_leg().expiry - near_leg().expiry).days
        self.reader = QuoteReader(client=None, cfg=self.cfg)
        self.near, self.far = near_leg(), far_leg()
        self.reader.set_instruments({"1769": self.near, "1584": self.far})

    def payload(self, nb="959400000", na="959450000",
                fb="965150000", fa="965200000"):
        return {"Status": "Success", "Response": [
            {"Token": "1769", "BestBidPrice": nb, "BestAskPrice": na},
            {"Token": "1584", "BestBidPrice": fb, "BestAskPrice": fa},
        ]}

    def test_the_declared_divisor_is_used(self):
        quotes = self.reader.parse(self.payload(), ["1769", "1584"], time.monotonic())
        self.assertEqual(quotes["1769"].divisor, DIVISOR)
        self.assertEqual(quotes["1769"].bid, D("95.9400"))
        self.assertEqual(quotes["1584"].ask, D("96.5200"))

    def test_the_worked_example_comes_out_of_real_raw_prices(self):
        quotes = self.reader.parse(self.payload(), ["1769", "1584"], time.monotonic())
        decision = rule.compute(quotes["1769"], quotes["1584"], self.cfg,
                                days=self.days)
        self.assertEqual(decision.roll_cost, D("0.5800"))
        self.assertEqual(decision.cost_per_lot, D("580.0000"))
        self.assertFalse(decision.qualifies)

    def test_a_guessed_scale_would_have_failed(self):
        """Without the declared divisor these prices fit no supported scale."""
        bare = QuoteReader(client=None, cfg=self.cfg)
        with self.assertRaises(QuoteError):
            bare.parse(self.payload(), ["1769", "1584"], time.monotonic())

    def test_a_wildly_wrong_price_fits_no_scale_at_all(self):
        # 88.00 is below both contracts' low circuit, so no divisor works.
        with self.assertRaises(QuoteError) as ctx:
            self.reader.parse(self.payload(nb="880000000", na="880050000"),
                              ["1769", "1584"], time.monotonic())
        self.assertIn("not inside the band", str(ctx.exception))

    def test_a_price_outside_only_its_own_contracts_limits_is_refused(self):
        """99.00 sits inside the pair's combined band but above the near
        contract's own high circuit of 98.835, so it must still be refused."""
        with self.assertRaises(QuoteError) as ctx:
            self.reader.parse(self.payload(nb="990000000", na="990050000"),
                              ["1769", "1584"], time.monotonic())
        self.assertIn("outside the day's limits", str(ctx.exception))


class TestGatesOnRealContracts(unittest.TestCase):
    def setUp(self):
        self.cfg = RollConfig(near_token="1769", far_token="1584",
                              near_expiry="2026-09-28", far_expiry="2026-11-26")
        self.near, self.far = near_leg(), far_leg()
        self.now = datetime(2026, 9, 17, 11, 0, 0)
        self.quotes = {
            "1769": Quote("1769", D("95.9400"), D("95.9450"), time.monotonic(),
                          DIVISOR, bid_qty=5000, ask_qty=5000),
            "1584": Quote("1584", D("96.2370"), D("96.2375"), time.monotonic(),
                          DIVISOR, bid_qty=5000, ask_qty=5000),
        }
        self.days = (self.far.expiry - self.near.expiry).days
        self.decision = rule.compute(self.quotes["1769"], self.quotes["1584"],
                                     self.cfg, days=self.days)

    def report(self):
        session = FakeSession(self.near, self.far)
        return gates.evaluate(self.cfg, session, self.quotes, self.decision, self.now)

    def test_the_real_pair_passes_every_gate(self):
        report = self.report()
        self.assertTrue(report.ok, msg="\n" + str(report))

    def test_an_option_leg_is_blocked(self):
        self.near = near_leg(instrument="OPTCUR")
        report = self.report()
        self.assertFalse(report.ok)
        self.assertIn("not a futures contract", str(report))

    def test_a_tick_that_disagrees_with_config_is_blocked(self):
        self.cfg.tick = "0.0100"
        self.days = (self.far.expiry - self.near.expiry).days
        self.decision = rule.compute(self.quotes["1769"], self.quotes["1584"],
                                     self.cfg, days=self.days)
        report = self.report()
        self.assertFalse(report.ok)
        self.assertIn("the exchange tick is", str(report))

    def test_a_missing_price_divisor_is_reported(self):
        self.near.price_divisor = None
        report = self.report()
        self.assertFalse(report.ok)
        self.assertIn("did not declare a PriceDivisor", str(report))


class TestOrderPriceScale(unittest.TestCase):
    """The single most dangerous number in the app.

    place_order takes the price in the contract's own exchange units, which the
    scrip master declares as PriceDivisor. The vendor documentation only shows
    the equity case, where PriceDivisor is 100 and it calls the result "paisa".
    For USDINR futures the divisor is 10000000, so sending a paisa value would
    understate the price by a factor of a hundred thousand.
    """

    def setUp(self):
        class StubLog:
            def info(self, m): pass
            def warn(self, m): pass
            def error(self, m): pass

        self.broker = Broker(RollConfig(), StubLog())
        self.near = near_leg()

    def test_a_rupee_price_becomes_the_right_integer(self):
        self.assertEqual(self.broker.exchange_price(self.near, D("95.9400")),
                         D("959400000"))

    def test_the_paisa_reading_would_have_been_wrong_by_100000x(self):
        correct = self.broker.exchange_price(self.near, D("95.9400"))
        paisa = D("95.9400") * 100
        self.assertEqual(correct / paisa, D("100000"))

    def test_a_price_off_the_tick_grid_is_refused(self):
        # 95.9410 is not a multiple of the 0.0025 tick.
        with self.assertRaises(BrokerError) as ctx:
            self.broker.exchange_price(self.near, D("95.9410"))
        self.assertIn("not a multiple of the tick", str(ctx.exception))

    def test_every_tick_step_converts_cleanly(self):
        price = D("95.9400")
        for _ in range(8):
            units = self.broker.exchange_price(self.near, price)
            self.assertEqual(units % self.near.tick_units, 0)
            price += self.near.tick

    def test_a_contract_with_no_divisor_is_refused(self):
        self.near.price_divisor = None
        with self.assertRaises(BrokerError) as ctx:
            self.broker.exchange_price(self.near, D("95.9400"))
        self.assertIn("no PriceDivisor", str(ctx.exception))

    def test_the_limit_prices_from_the_rule_convert_cleanly(self):
        cfg = RollConfig(allowance_ticks=1, limit_mode="absolute")
        quotes = {
            "1769": Quote("1769", D("95.9400"), D("95.9450"), time.monotonic(),
                          DIVISOR, bid_qty=5000, ask_qty=5000),
            "1584": Quote("1584", D("96.2370"), D("96.2375"), time.monotonic(),
                          DIVISOR, bid_qty=5000, ask_qty=5000),
        }
        decision = rule.compute(quotes["1769"], quotes["1584"], cfg)
        self.assertEqual(self.broker.exchange_price(self.near, decision.sell_limit),
                         D("959375000"))
        self.assertEqual(self.broker.exchange_price(far_leg(), decision.buy_limit),
                         D("962400000"))


class TestLiveTouchlineShape(unittest.TestCase):
    """The real MultipleTouchline payload, copied from a live response.

    Three things here are nothing like what was first assumed: the fields are
    called Buy and Sell, each is the five level depth ladder rather than a
    price, and the prices arrive as 32 bit floats already in rupees.
    """

    LIVE = {
        "Status": "Success",
        "Response": {"MultipleTouchline": [
            {
                "SegmentId": 13, "Token": 1769,
                "LTP": 95.91999816894531, "LTQ": 162,
                "TBQ": 33299, "TSQ": 13327,
                "Close": 95.9574966430664, "Open": 96.0199966430664,
                "High": 96.14250183105469, "Low": 95.80000305175781,
                "Volume": 484272, "AvgTradePrice": 95.94278717041016,
                "Buy": [
                    {"Price": 95.91999816894531, "Qty": 338, "NoOfOrders": 1},
                    {"Price": 95.91500091552734, "Qty": 1000, "NoOfOrders": 1},
                ],
                "Sell": [
                    {"Price": 95.92250061035156, "Qty": 29, "NoOfOrders": 2},
                    {"Price": 95.92749786376953, "Qty": 18, "NoOfOrders": 1},
                ],
                "OI": 2974345,
            },
            {
                "SegmentId": 13, "Token": 1584,
                "LTP": 96.5199966430664,
                "Buy": [{"Price": 96.44999694824219, "Qty": 60, "NoOfOrders": 1}],
                "Sell": [{"Price": 96.5199966430664, "Qty": 7, "NoOfOrders": 1}],
            },
        ]},
    }

    def setUp(self):
        self.cfg = RollConfig(near_token="1769", far_token="1584")
        self.days = (far_leg().expiry - near_leg().expiry).days
        self.reader = QuoteReader(client=None, cfg=self.cfg)
        self.reader.set_instruments({"1769": near_leg(), "1584": far_leg()})
        self.quotes = self.reader.parse(self.LIVE, ["1769", "1584"], time.monotonic())

    def test_the_buy_and_sell_ladders_are_found(self):
        self.assertEqual(self.reader.bid_key, "Buy")
        self.assertEqual(self.reader.ask_key, "Sell")

    def test_the_feed_is_already_in_rupees(self):
        """The order API divisor is 10000000, but the feed is not scaled."""
        self.assertEqual(self.reader.divisor, D("1"))

    def test_float_noise_is_snapped_back_onto_the_tick_grid(self):
        near = self.quotes["1769"]
        self.assertEqual(near.bid, D("95.9200"))
        self.assertEqual(near.ask, D("95.9225"))
        for price in (near.bid, near.ask):
            self.assertEqual(price % D("0.0025"), 0)

    def test_without_snapping_the_sell_limit_would_be_a_tick_too_low(self):
        from rollover.money import floor_tick
        raw = D("95.91999816894531")
        self.assertEqual(floor_tick(raw, D("0.0025")), D("95.9175"))
        self.assertEqual(self.quotes["1769"].bid, D("95.9200"))

    def test_the_top_of_book_size_is_carried_through(self):
        self.assertEqual(self.quotes["1769"].bid_qty, 338)
        self.assertEqual(self.quotes["1584"].ask_qty, 7)

    def test_the_top_rung_is_used_not_a_deeper_one(self):
        self.assertEqual(self.quotes["1769"].bid, D("95.9200"))   # not 95.9150

    def test_an_empty_ladder_is_refused(self):
        with self.assertRaises(QuoteError) as ctx:
            top_of_book([], "bid", "1769")
        self.assertIn("nothing resting", str(ctx.exception))

    def test_a_price_genuinely_off_the_grid_is_refused(self):
        payload = {"Response": {"MultipleTouchline": [
            {"Token": 1769, "Buy": [{"Price": 95.9210, "Qty": 10}],
             "Sell": [{"Price": 95.9250, "Qty": 10}]},
            {"Token": 1584, "Buy": [{"Price": 96.4500, "Qty": 10}],
             "Sell": [{"Price": 96.5200, "Qty": 10}]},
        ]}}
        with self.assertRaises(QuoteError) as ctx:
            self.reader.parse(payload, ["1769", "1584"], time.monotonic())
        self.assertIn("away from the tick grid", str(ctx.exception))

    def test_a_thin_far_offer_blocks_the_roll(self):
        """7 units on the far offer cannot fill a 1000 unit clip."""
        decision = rule.compute(self.quotes["1769"], self.quotes["1584"],
                                self.cfg, days=59)
        session = FakeSession(near_leg(), far_leg())
        report = gates.evaluate(self.cfg, session, self.quotes, decision,
                                datetime(2026, 9, 17, 11, 0, 0))
        self.assertFalse(report.ok)
        self.assertIn("7 resting at the ask", str(report))

    def test_the_size_check_can_be_turned_off(self):
        self.cfg.require_touch_size = False
        decision = rule.compute(self.quotes["1769"], self.quotes["1584"],
                                self.cfg, days=59)
        session = FakeSession(near_leg(), far_leg())
        report = gates.evaluate(self.cfg, session, self.quotes, decision,
                                datetime(2026, 9, 17, 11, 0, 0))
        self.assertIn("size check disabled", str(report))


class TestMarketStatusShape(unittest.TestCase):
    """The real MarketStatus payload keys the segment as a dictionary key.

    The first parser looked for a record with a SegmentId field and so never
    matched anything, which failed the market-open gate on every tick.
    """

    LIVE = {
        "Status": "Success",
        "Response": {
            "lstMktStatus": {
                "1": {"1": {"MktType": 1, "Status": "1"},
                      "8": {"MktType": 8, "Status": "1"}},
                "13": {"1": {"MktType": 1, "Status": "1"},
                       "8": {"MktType": 8, "Status": "1"}},
            },
            "MktStatusResp": "64=110|1=1|38=1$85=1|1=13|38=1$85=1",
        },
        "Reason": "",
    }

    def broker(self, payload, segment=13):
        class StubLog:
            def info(self, m): pass
            def warn(self, m): pass
            def error(self, m): pass

        class StubMarket:
            def get_market_status(inner): return payload

        class StubClient:
            market = StubMarket()

        b = Broker(RollConfig(segment_id=segment), StubLog())
        b.client = StubClient()
        return b

    def test_the_currency_segment_is_read_as_open(self):
        self.assertIs(self.broker(self.LIVE).market_open(), True)

    def test_a_closed_status_is_read_as_closed(self):
        import copy
        payload = copy.deepcopy(self.LIVE)
        payload["Response"]["lstMktStatus"]["13"]["1"]["Status"] = "0"
        self.assertIs(self.broker(payload).market_open(), False)

    def test_an_unrecognised_status_counts_as_closed_not_unknown(self):
        import copy
        payload = copy.deepcopy(self.LIVE)
        payload["Response"]["lstMktStatus"]["13"]["1"]["Status"] = "7"
        self.assertIs(self.broker(payload).market_open(), False)

    def test_a_missing_segment_is_unknown_not_open(self):
        self.assertIsNone(self.broker(self.LIVE, segment=99).market_open())

    def test_an_unreadable_shape_is_unknown_not_open(self):
        self.assertIsNone(self.broker({"Status": "Success"}).market_open())

    def test_the_normal_market_is_used_not_another_market_type(self):
        import copy
        payload = copy.deepcopy(self.LIVE)
        payload["Response"]["lstMktStatus"]["13"]["1"]["Status"] = "0"
        payload["Response"]["lstMktStatus"]["13"]["8"]["Status"] = "1"
        self.assertIs(self.broker(payload).market_open(), False)


class TestNetPositionShape(unittest.TestCase):
    """An account holding nothing returns an empty NetPositions list."""

    def broker(self, payload):
        class StubLog:
            def info(self, m): pass
            def warn(self, m): pass
            def error(self, m): pass

        class StubPortfolio:
            def get_net_position(inner): return payload

        class StubClient:
            portfolio = StubPortfolio()

        b = Broker(RollConfig(), StubLog())
        b.client = StubClient()
        return b

    def test_an_empty_book_means_zero_not_unknown(self):
        payload = {"Status": "Success", "Response": {"NetPositions": []}, "Reason": ""}
        self.assertEqual(self.broker(payload).long_qty("1769"), 0)

    def test_a_held_contract_is_read(self):
        payload = {"Status": "Success", "Response": {"NetPositions": [
            {"Token": 1769, "NetQty": 2000, "SegmentId": 13}]}}
        self.assertEqual(self.broker(payload).long_qty("1769"), 2000)

    def test_a_short_position_does_not_count_as_long(self):
        payload = {"Status": "Success", "Response": {"NetPositions": [
            {"Token": 1769, "NetQty": -2000, "SegmentId": 13}]}}
        self.assertEqual(self.broker(payload).long_qty("1769"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
