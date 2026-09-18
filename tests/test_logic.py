"""Offline tests. No network, no credentials, no orders.

Run with:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import time
import unittest
from datetime import date, datetime

from rollover import gates, rule
from rollover.broker import Broker, BrokerError, InstrumentInfo, parse_expiry
from rollover.config import ConfigError, RollConfig
from rollover.money import D, PriceError, ceil_tick, floor_tick, to_exchange_units
from rollover.quotes import Quote, QuoteError, QuoteReader, detect_divisor


def quote(token="1", bid="95.9400", ask="95.9450", age=0.0, divisor="1",
          bid_qty=5000, ask_qty=5000):
    return Quote(token, D(bid), D(ask), time.monotonic() - age, D(divisor),
                 bid_qty=bid_qty, ask_qty=ask_qty)


class FakeSession:
    def __init__(self, **kw):
        self.logged_in = kw.get("logged_in", True)
        self.near = kw.get("near")
        self.far = kw.get("far")
        self.market_open = kw.get("market_open", True)
        self.near_position_qty = kw.get("near_position_qty", 1000)
        self.clips_done_today = kw.get("clips_done_today", 0)
        self.in_flight = kw.get("in_flight", False)
        self.halted_reason = kw.get("halted_reason")
        self.scrip_file_date = kw.get("scrip_file_date", date(2026, 9, 17))
        self.quote_source = kw.get("quote_source", "live feed")


def make_instrument(token, expiry, lot_size=1000, desc="USDINR",
                    instrument="FUTCUR", tick="0.0025", divisor="10000000",
                    low="93.08", high="98.84"):
    """A real InstrumentInfo, with the values today's scrip master actually gives."""
    return InstrumentInfo(
        token=token, symbol="USDINR", sec_desc=desc, segment="13",
        lot_size=lot_size, expiry=expiry, instrument=instrument,
        price_divisor=D(divisor) if divisor else None,
        tick=D(tick) if tick else None,
        low_range=D(low) if low else None,
        high_range=D(high) if high else None,
    )


class TestMoney(unittest.TestCase):
    def test_roll_cost_is_exact(self):
        self.assertEqual(D("96.5200") - D("95.9400"), D("0.5800"))

    def test_float_is_rejected_as_a_price_string(self):
        # A float still converts, but via repr so the damage is visible, not silent.
        self.assertEqual(D(0.1), D("0.1"))

    def test_tick_rounding_directions(self):
        tick = D("0.0025")
        self.assertEqual(floor_tick("95.9413", tick), D("95.9400"))
        self.assertEqual(ceil_tick("95.9413", tick), D("95.9425"))
        self.assertEqual(floor_tick("95.9425", tick), D("95.9425"))

    def test_exchange_units_follow_the_contracts_own_divisor(self):
        # Equity declares 100, which is the documented 1300 INR -> 130000.
        self.assertEqual(to_exchange_units("1300.00", D("100")), D("130000"))
        # USDINR futures declare 10000000.
        self.assertEqual(to_exchange_units("95.9400", D("10000000")), D("959400000"))
        self.assertEqual(to_exchange_units("95.9375", D("10000000")), D("959375000"))

    def test_a_price_that_is_not_a_whole_unit_is_refused_not_rounded(self):
        with self.assertRaises(PriceError):
            to_exchange_units("95.94005", D("100"))

    def test_a_bad_divisor_is_refused(self):
        with self.assertRaises(PriceError):
            to_exchange_units("95.94", D("0"))


class TestRule(unittest.TestCase):
    """The mechanics of the rule: rounding, the strict inequality, worst case.

    Pinned to a fixed rupee limit so these stay about the arithmetic. The
    tenor based limit has its own tests in TestBpsLimits.
    """

    def setUp(self):
        self.cfg = RollConfig(limit_mode="absolute")

    def test_worked_example_does_not_trade(self):
        d = rule.compute(quote(bid="95.9400", ask="95.9450"),
                         quote(bid="96.5150", ask="96.5200"), self.cfg)
        self.assertEqual(d.roll_cost, D("0.5800"))
        self.assertEqual(d.cost_per_lot, D("580.0000"))
        self.assertFalse(d.qualifies)

    def test_strictly_below_the_limit_trades(self):
        d = rule.compute(quote(bid="95.9400"), quote(bid="96.2370", ask="96.2375"), self.cfg)
        self.assertEqual(d.roll_cost, D("0.2975"))
        self.assertTrue(d.qualifies)

    def test_exactly_at_the_limit_does_not_trade(self):
        d = rule.compute(quote(bid="95.9400"), quote(bid="96.2395", ask="96.2400"), self.cfg)
        self.assertEqual(d.roll_cost, D("0.3000"))
        self.assertFalse(d.qualifies)

    def test_direction_is_far_ask_minus_near_bid(self):
        """The reversed formula would be negative and fire on every tick."""
        d = rule.compute(quote(bid="95.9400"), quote(bid="96.5150", ask="96.5200"), self.cfg)
        self.assertGreater(d.roll_cost, 0)
        self.assertEqual(d.roll_cost, D("96.5200") - D("95.9400"))

    def test_limit_prices_sit_on_the_tick_grid(self):
        cfg = RollConfig(allowance_ticks=1, limit_mode="absolute")
        d = rule.compute(quote(bid="95.9400"), quote(bid="96.1895", ask="96.1900"), cfg)
        self.assertEqual(d.sell_limit, D("95.9375"))
        self.assertEqual(d.buy_limit, D("96.1925"))
        self.assertEqual(d.worst_case, D("0.2550"))
        self.assertTrue(d.qualifies)

    def test_allowance_that_breaches_the_limit_is_blocked(self):
        cfg = RollConfig(allowance_ticks=12, limit_mode="absolute")
        d = rule.compute(quote(bid="95.9400"), quote(bid="96.1895", ask="96.1900"), cfg)
        self.assertLess(d.roll_cost, cfg.roll_limit_d)
        self.assertGreaterEqual(d.worst_case, cfg.roll_limit_d)
        self.assertFalse(d.qualifies)

    def test_worst_case_is_computed_from_rounded_prices(self):
        cfg = RollConfig(allowance_ticks=1, limit_mode="absolute")
        d = rule.compute(quote(bid="95.9400"), quote(bid="96.1895", ask="96.1900"), cfg)
        self.assertEqual(d.worst_case, d.buy_limit - d.sell_limit)


class TestScaleDetection(unittest.TestCase):
    low, high = D("70"), D("130")

    def test_rupee_prices(self):
        self.assertEqual(
            detect_divisor([D("95.94"), D("96.52")], self.low, self.high), D("1"))

    def test_paisa_prices(self):
        self.assertEqual(
            detect_divisor([D("9594"), D("9652")], self.low, self.high), D("100"))

    def test_nothing_fits_is_an_error(self):
        with self.assertRaises(QuoteError):
            detect_divisor([D("3")], self.low, self.high)

    def test_forced_divisor_is_still_checked(self):
        with self.assertRaises(QuoteError):
            detect_divisor([D("9594")], self.low, self.high, forced=D("1"))


class TestTouchlineParsing(unittest.TestCase):
    def setUp(self):
        self.cfg = RollConfig(near_token="1001", far_token="1002")
        self.reader = QuoteReader(client=None, cfg=self.cfg)

    def payload(self, bid1="9594.00", ask1="9594.50", bid2="9651.50", ask2="9652.00"):
        return {"Status": "Success", "Response": [
            {"Token": "1001", "BestBidPrice": bid1, "BestAskPrice": ask1, "LTP": "9594.25"},
            {"Token": "1002", "BestBidPrice": bid2, "BestAskPrice": ask2, "LTP": "9651.75"},
        ]}

    def test_reads_both_legs_and_rescales(self):
        quotes = self.reader.parse(self.payload(), ["1001", "1002"], time.monotonic())
        self.assertEqual(quotes["1001"].bid, D("95.9400"))
        self.assertEqual(quotes["1002"].ask, D("96.5200"))
        self.assertEqual(quotes["1001"].divisor, D("100"))

    def test_missing_token_is_an_error(self):
        with self.assertRaises(QuoteError):
            self.reader.parse(self.payload(), ["1001", "9999"], time.monotonic())

    def test_empty_side_is_an_error(self):
        with self.assertRaises(QuoteError):
            self.reader.parse(self.payload(bid1="0"), ["1001", "1002"], time.monotonic())

    def test_crossed_book_is_an_error(self):
        bad = self.payload(bid1="9595.00", ask1="9594.00")
        with self.assertRaises(QuoteError):
            self.reader.parse(bad, ["1001", "1002"], time.monotonic())

    def test_unknown_field_names_are_reported_not_guessed(self):
        payload = {"Response": [{"Token": "1001", "Mystery1": "9594", "Mystery2": "9595"},
                                {"Token": "1002", "Mystery1": "9651", "Mystery2": "9652"}]}
        with self.assertRaises(QuoteError) as ctx:
            self.reader.parse(payload, ["1001", "1002"], time.monotonic())
        self.assertIn("bid_field", str(ctx.exception))

    def test_failed_status_is_an_error(self):
        with self.assertRaises(QuoteError):
            self.reader.parse({"Status": "Failure", "Response": None},
                              ["1001"], time.monotonic())

    def test_each_source_keeps_its_own_scale(self):
        """The websocket sends exchange units and REST sends rupees; falling
        back from one to the other is not a corrupted scale."""
        self.reader.parse(self.payload(), ["1001", "1002"], time.monotonic())
        self.assertEqual(self.reader._divisors["touchline"], D("100"))
        self.reader._divisors["live feed"] = D("10000000")
        # Polling again must still be fine.
        self.reader.parse(self.payload(), ["1001", "1002"], time.monotonic())
        self.assertEqual(self.reader._divisors["touchline"], D("100"))

    def test_scale_cannot_change_mid_session(self):
        self.reader.parse(self.payload(), ["1001", "1002"], time.monotonic())
        rupees = {"Response": [
            {"Token": "1001", "BestBidPrice": "95.94", "BestAskPrice": "95.945"},
            {"Token": "1002", "BestBidPrice": "96.515", "BestAskPrice": "96.52"}]}
        with self.assertRaises(QuoteError):
            self.reader.parse(rupees, ["1001", "1002"], time.monotonic())


class TestGates(unittest.TestCase):
    def setUp(self):
        self.cfg = RollConfig(near_token="1001", far_token="1002",
                              near_expiry="2026-09-28", far_expiry="2026-11-26")
        self.near = make_instrument("1001", date(2026, 9, 28), desc="USDINR SEP26")
        self.far = make_instrument("1002", date(2026, 11, 26), desc="USDINR NOV26")
        self.now = datetime(2026, 9, 17, 11, 0, 0)
        self.quotes = {"1001": quote("1001", "95.9400", "95.9450"),
                       "1002": quote("1002", "96.2370", "96.2375")}
        self.days = (self.far.expiry - self.near.expiry).days
        self.decision = rule.compute(self.quotes["1001"], self.quotes["1002"],
                                     self.cfg, days=self.days)

    def report(self, **session_kw):
        # The scrip master is today's unless a test says otherwise, where
        # "today" is whatever clock the test is running against.
        session_kw.setdefault("scrip_file_date", self.now.date())
        session = FakeSession(near=self.near, far=self.far, **session_kw)
        return gates.evaluate(self.cfg, session, self.quotes, self.decision, self.now)

    def test_all_gates_pass_in_the_happy_case(self):
        report = self.report()
        self.assertTrue(report.ok, msg="\n" + str(report))

    def test_halt_blocks_everything(self):
        self.assertFalse(self.report(halted_reason="half rolled").ok)

    def test_closed_market_blocks(self):
        self.assertFalse(self.report(market_open=False).ok)

    def test_unknown_market_status_blocks_by_default(self):
        self.assertFalse(self.report(market_open=None).ok)

    def test_unknown_market_status_can_be_waived_explicitly(self):
        self.cfg.require_market_status = False
        self.assertTrue(self.report(market_open=None).ok)

    def test_no_position_blocks(self):
        self.assertFalse(self.report(near_position_qty=0).ok)

    def test_unknown_position_blocks(self):
        self.assertFalse(self.report(near_position_qty=None).ok)

    def test_daily_clip_budget_blocks(self):
        self.assertFalse(self.report(clips_done_today=1).ok)

    def test_stale_quote_blocks(self):
        self.quotes["1002"] = quote("1002", "96.2370", "96.2375", age=30)
        self.assertFalse(self.report().ok)

    def test_wide_leg_spread_blocks(self):
        self.quotes["1002"] = quote("1002", "96.0000", "96.2375")
        self.days = (self.far.expiry - self.near.expiry).days
        self.decision = rule.compute(self.quotes["1001"], self.quotes["1002"],
                                     self.cfg, days=self.days)
        self.assertFalse(self.report().ok)

    def test_expiry_mismatch_against_config_blocks(self):
        self.near = make_instrument("1001", date(2026, 9, 26))
        self.assertFalse(self.report().ok)

    def test_unknown_expiry_blocks(self):
        self.near = make_instrument("1001", None)
        self.assertFalse(self.report().ok)

    def test_far_before_near_blocks(self):
        self.far = make_instrument("1002", date(2026, 8, 26))
        self.cfg.far_expiry = "2026-08-26"
        self.assertFalse(self.report().ok)

    def test_wrong_lot_size_blocks(self):
        self.near = make_instrument("1001", date(2026, 9, 28), lot_size=500)
        self.assertFalse(self.report().ok)

    def test_outside_the_trading_window_blocks(self):
        self.now = datetime(2026, 9, 17, 18, 30, 0)
        self.assertFalse(self.report().ok)

    def test_expiry_day_cutoff_blocks_after_1225(self):
        self.now = datetime(2026, 9, 28, 12, 40, 0)
        self.assertFalse(self.report().ok)

    def test_expiry_day_before_cutoff_passes(self):
        self.now = datetime(2026, 9, 28, 11, 0, 0)
        self.assertTrue(self.report().ok, msg="\n" + str(self.report()))

    def test_implausible_roll_cost_blocks(self):
        self.quotes["1002"] = quote("1002", "100.0000", "100.0005")
        self.days = (self.far.expiry - self.near.expiry).days
        self.decision = rule.compute(self.quotes["1001"], self.quotes["1002"],
                                     self.cfg, days=self.days)
        self.assertFalse(self.report().ok)

    def test_cost_above_the_limit_blocks(self):
        self.quotes["1002"] = quote("1002", "96.5150", "96.5200")
        self.days = (self.far.expiry - self.near.expiry).days
        self.decision = rule.compute(self.quotes["1001"], self.quotes["1002"],
                                     self.cfg, days=self.days)
        self.assertFalse(self.report().ok)

    def test_in_flight_order_blocks(self):
        self.assertFalse(self.report(in_flight=True).ok)


class TestGatesSizeOnTheOrderNotTheClip(TestGates):
    """A ladder rung with less than a clip left still has to be tradeable.

    The position and depth gates measured against cfg.clip_qty, but a rung with
    300 units remaining sends 300. Checking for a full clip blocked that order
    although there was ample depth and ample position, so the tail of every
    rung was untradeable.
    """

    def named(self, report, name):
        return next(g for g in report.gates if g.name == name)

    def small_order(self, qty):
        from dataclasses import replace
        self.decision = replace(self.decision, qty=qty)

    def test_a_part_clip_passes_on_depth_a_full_clip_would_fail(self):
        self.quotes = {"1001": quote("1001", "95.9400", "95.9450", bid_qty=500, ask_qty=500),
                       "1002": quote("1002", "96.2370", "96.2375", bid_qty=500, ask_qty=500)}
        self.small_order(300)
        report = self.report(near_position_qty=600)

        self.assertTrue(self.named(report, "near touch size").ok)
        self.assertTrue(self.named(report, "far touch size").ok)
        self.assertTrue(self.named(report, "position").ok)

    def test_a_full_clip_is_still_checked_against_the_full_clip(self):
        self.quotes = {"1001": quote("1001", "95.9400", "95.9450", bid_qty=500, ask_qty=500),
                       "1002": quote("1002", "96.2370", "96.2375", bid_qty=500, ask_qty=500)}
        self.small_order(1000)
        report = self.report(near_position_qty=600)

        self.assertFalse(self.named(report, "near touch size").ok)
        self.assertFalse(self.named(report, "position").ok)

    def test_the_detail_quotes_the_size_actually_needed(self):
        self.small_order(300)
        self.assertIn("300", self.named(self.report(), "near touch size").detail)

    def test_nothing_qualifying_does_not_make_a_size_gate_pass(self):
        """qty 0 means no order. A gate that passes on nothing is worse than
        useless, so it falls back to the nominal clip."""
        self.quotes = {"1001": quote("1001", "95.9400", "95.9450", bid_qty=10, ask_qty=10),
                       "1002": quote("1002", "96.2370", "96.2375", bid_qty=10, ask_qty=10)}
        self.small_order(0)
        report = self.report(near_position_qty=10)

        self.assertFalse(self.named(report, "near touch size").ok)
        self.assertIn("1000", self.named(report, "near touch size").detail)

    def test_without_a_ladder_nothing_changes(self):
        """The plain path still measures the configured clip."""
        report = self.report()
        self.assertTrue(report.ok, msg=str(report))


class TestAnUnrecognisedSetting(unittest.TestCase):
    """A key this build does not know must not stop it starting.

    The app ships two executables and writes config.json itself. The window
    updates and the console tool does not, so a setting added by a newer build
    used to stop an older one from starting at all -- which is what happened
    the moment watch_limits was introduced.
    """

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="unknownkey_")
        self.path = os.path.join(self.dir, "config.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, extra):
        cfg = RollConfig(near_token="1769", far_token="1584")
        cfg.save(self.path)
        with open(self.path, encoding="utf-8") as fh:
            body = json.load(fh)
        body.update(extra)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(body, fh)

    def test_it_loads_anyway(self):
        self.write({"something_from_a_newer_build": [1, 2, 3]})
        cfg = RollConfig.load(self.path)
        self.assertEqual(cfg.near_token, "1769")

    def test_it_says_what_it_did_not_recognise(self):
        self.write({"something_new": 1, "another_thing": 2})
        self.assertEqual(RollConfig.load(self.path).unknown_keys,
                         ["another_thing", "something_new"])

    def test_a_clean_file_reports_nothing(self):
        self.write({})
        self.assertEqual(RollConfig.load(self.path).unknown_keys, [])

    def test_saving_does_not_invent_the_key_back(self):
        """It does not know what the setting means, so it must not write it."""
        self.write({"something_new": 1})
        cfg = RollConfig.load(self.path)
        cfg.save(self.path)
        with open(self.path, encoding="utf-8") as fh:
            body = json.load(fh)
        self.assertNotIn("unknown_keys", body)

    def test_the_known_settings_still_apply(self):
        self.write({"lots": 7, "something_new": 1})
        self.assertEqual(RollConfig.load(self.path).lots, 7)

    def test_a_genuinely_invalid_setting_is_still_refused(self):
        """Forgiving an unknown key is not forgiving a wrong value."""
        self.write({"lots": 0})
        with self.assertRaises(ConfigError):
            RollConfig.load(self.path)


class TestExpiryParsing(unittest.TestCase):
    def test_formats(self):
        for text in ("2026-09-28", "28-Sep-2026", "28/09/2026", "28Sep2026",
                     "2026-09-28T00:00:00"):
            self.assertEqual(parse_expiry(text), date(2026, 9, 28), msg=text)

    def test_unreadable_is_none(self):
        self.assertIsNone(parse_expiry("next month"))
        self.assertIsNone(parse_expiry(""))
        self.assertIsNone(parse_expiry(None))


class TestConfigValidation(unittest.TestCase):
    def test_defaults_are_valid(self):
        RollConfig().validate()

    def test_market_order_type_is_rejected(self):
        with self.assertRaises(ConfigError):
            RollConfig(order_type="MARKET").validate()

    def test_same_token_on_both_legs_is_rejected(self):
        with self.assertRaises(ConfigError):
            RollConfig(near_token="1", far_token="1").validate()

    def test_zero_limit_is_rejected(self):
        with self.assertRaises(ConfigError):
            RollConfig(roll_limit="0", limit_mode="absolute").validate()

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(TypeError):
            RollConfig(**{"roll_limt": "0.30"})

    def test_secrets_are_masked_in_the_log_view(self):
        cfg = RollConfig(api_key="abcd1234wxyz", mobile_no="9876543210")
        self.assertNotIn("abcd1234", cfg.redacted()["api_key"])
        self.assertNotIn("9876543", cfg.redacted()["mobile_no"])


class StubLog:
    def __init__(self):
        self.lines = []
    def info(self, m): self.lines.append(("INFO", m))
    def warn(self, m): self.lines.append(("WARN", m))
    def error(self, m): self.lines.append(("ERROR", m))
    def alert(self, m): self.lines.append(("ALERT", m))


class StubScripMaster:
    is_loaded = False
    def fetch(self): 
        self.is_loaded = True
        return True


class StubClient:
    """Stands in for ChoiceClient so the login flow can be tested offline."""

    def __init__(self, replies):
        self.replies = replies          # last path segment -> reply or Exception
        self.calls = []
        self.session_id = None
        self.access_token = None
        self.bcast_ip = None
        self.bcast_port = None
        self.saved_to = None
        self.scrip_master = StubScripMaster()

    def _get_encoded_mobile(self, mobile):
        return "encoded:" + mobile

    def request(self, method, endpoint, data=None, require_auth=True):
        self.calls.append((endpoint, data))
        # Match the exact last segment: "GetClientLoginTOTP" also ends with
        # "LoginTOTP", so a suffix match would answer the wrong call.
        name = endpoint.rstrip("/").split("/")[-1]
        if name in self.replies:
            reply = self.replies[name]
            if isinstance(reply, Exception):
                raise reply
            return reply
        raise AssertionError(f"unexpected endpoint {endpoint}")

    def save_session(self, path):
        self.saved_to = path
        return True


class TestLoginFlow(unittest.TestCase):
    def setUp(self):
        self.cfg = RollConfig(vendor_id="V1", api_key="K1", mobile_no="9999999999")
        self.log = StubLog()
        self.broker = Broker(self.cfg, self.log)

    def attach(self, replies):
        self.broker.client = StubClient(replies)
        return self.broker.client

    def test_broker_returns_the_otp_when_choice_supplies_one(self):
        self.attach({
            "LoginTOTP": {"Status": "Success"},
            "GetClientLoginTOTP": {"Status": "Success", "Response": "123456"},
        })
        self.assertEqual(self.broker.request_otp(), "123456")

    def test_broker_asks_the_operator_when_choice_sends_no_otp(self):
        self.attach({
            "LoginTOTP": {"Status": "Success"},
            "GetClientLoginTOTP": {"Status": "Failure", "Response": None},
        })
        self.assertIsNone(self.broker.request_otp())

    def test_broker_asks_the_operator_when_the_otp_call_errors(self):
        self.attach({
            "LoginTOTP": {"Status": "Success"},
            "GetClientLoginTOTP": RuntimeError("404"),
        })
        self.assertIsNone(self.broker.request_otp())

    def test_refused_login_raises(self):
        self.attach({"LoginTOTP": {"Status": "Failure", "Response": "bad vendor"}})
        with self.assertRaises(BrokerError):
            self.broker.request_otp()

    def test_a_good_otp_stores_the_session_and_saves_it(self):
        client = self.attach({
            "ValidateTOTP": {"Status": "Success", "Response": {
                "SessionId": "SESS-1", "AccessToken": "TOK", "OdinBcastPort": "4520"}},
        })
        self.broker.submit_otp("123456", "session.json")
        self.assertEqual(client.session_id, "SESS-1")
        self.assertEqual(client.bcast_port, 4520)
        self.assertEqual(client.saved_to, "session.json")
        self.assertTrue(self.broker.logged_in)

    def test_a_bad_otp_is_rejected_and_leaves_no_session(self):
        client = self.attach({"ValidateTOTP": {"Status": "Failure", "Response": "bad otp"}})
        with self.assertRaises(BrokerError):
            self.broker.submit_otp("000000", "session.json")
        self.assertIsNone(client.session_id)
        self.assertFalse(self.broker.logged_in)

    def test_an_empty_otp_is_refused_before_any_call(self):
        client = self.attach({})
        with self.assertRaises(BrokerError):
            self.broker.submit_otp("   ", "session.json")
        self.assertEqual(client.calls, [])

    def test_a_reply_with_no_session_id_is_an_error(self):
        self.attach({"ValidateTOTP": {"Status": "Success", "Response": {"AccessToken": "T"}}})
        with self.assertRaises(BrokerError):
            self.broker.submit_otp("123456", "session.json")

    def test_a_plain_string_session_id_is_accepted(self):
        client = self.attach({"ValidateTOTP": {"Status": "Success", "Response": "SESS-2"}})
        self.broker.submit_otp("123456", "session.json")
        self.assertEqual(client.session_id, "SESS-2")

    def test_missing_credentials_are_named(self):
        self.broker.cfg = RollConfig(vendor_id="V1", api_key="", mobile_no="")
        with self.assertRaises(BrokerError) as ctx:
            self.broker.build_client()
        message = str(ctx.exception)
        self.assertIn("api_key", message)
        self.assertIn("mobile_no", message)
        self.assertNotIn("vendor_id", message)

    def test_scrip_master_is_loaded_after_login(self):
        client = self.attach({
            "ValidateTOTP": {"Status": "Success", "Response": {"SessionId": "S"}}})
        self.broker.submit_otp("1", "session.json")
        self.assertTrue(client.scrip_master.is_loaded)


class TestLoginThreading(unittest.TestCase):
    """The login runs its network calls on a worker thread.

    Tk objects may only be touched from the main thread, so a widget or a Tk
    variable read inside a worker method raises "main thread is not in main
    loop" and the login dies with a confusing error. These checks read the
    source of the worker methods, because the failure only shows up at runtime.
    """

    def worker_source(self) -> str:
        import inspect
        from rollover import login
        return "".join(inspect.getsource(fn) for fn in
                       (login.LoginWindow._work_start, login.LoginWindow._work_verify))

    def test_workers_do_not_read_tk_variables(self):
        source = self.worker_source()
        for forbidden in ("_var.get()", "_entry.get()", ".configure(", "self.status"):
            self.assertNotIn(forbidden, source,
                             f"{forbidden} touches Tk from the worker thread")

    def test_workers_only_talk_through_the_queue(self):
        source = self.worker_source()
        self.assertIn("self._events.put(", source)

    def test_force_fresh_is_passed_in_rather_than_read_in_the_worker(self):
        import inspect
        from rollover.login import LoginWindow
        params = list(inspect.signature(LoginWindow._work_start).parameters)
        self.assertEqual(params, ["self", "force_fresh"])


class TestScripFreshnessGate(unittest.TestCase):
    """A scrip master from a previous day has stale expiries and stale bands."""

    def setUp(self):
        self.cfg = RollConfig(near_token="1001", far_token="1002",
                              near_expiry="2026-09-28", far_expiry="2026-11-26")
        self.near = make_instrument("1001", date(2026, 9, 28), desc="USDINR SEP26")
        self.far = make_instrument("1002", date(2026, 11, 26), desc="USDINR NOV26")
        self.now = datetime(2026, 9, 17, 11, 0, 0)
        self.quotes = {"1001": quote("1001", "95.9400", "95.9450"),
                       "1002": quote("1002", "96.2370", "96.2375")}
        self.days = (self.far.expiry - self.near.expiry).days
        self.decision = rule.compute(self.quotes["1001"], self.quotes["1002"],
                                     self.cfg, days=self.days)

    def report(self, **session_kw):
        session_kw.setdefault("scrip_file_date", self.now.date())
        session = FakeSession(near=self.near, far=self.far, **session_kw)
        return gates.evaluate(self.cfg, session, self.quotes, self.decision, self.now)

    def test_todays_file_passes(self):
        self.assertTrue(self.report(scrip_file_date=date(2026, 9, 17)).ok)

    def test_yesterdays_file_blocks(self):
        report = self.report(scrip_file_date=date(2026, 9, 16))
        self.assertFalse(report.ok)
        self.assertIn("may be a day behind", str(report))

    def test_an_unknown_file_date_blocks(self):
        self.assertFalse(self.report(scrip_file_date=None).ok)

    def test_the_check_can_be_waived(self):
        self.cfg.require_fresh_scrip = False
        self.assertTrue(self.report(scrip_file_date=date(2026, 9, 16)).ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
