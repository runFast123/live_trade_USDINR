"""Can the account afford this roll, before any of it is sent.

There was no margin check at all. A shortfall surfaces as a filled near leg and
a rejected far one: a naked short in the month that is about to expire, which is
the worst state this program can produce.
"""
from __future__ import annotations

import unittest

from rollover import margin
from rollover.config import RollConfig
from rollover.margin import Estimate, available, estimate, required
from rollover.money import D
from tests.test_execution import ExecutionCase, outcome


class StubLog:
    def __init__(self):
        self.lines = []

    def info(self, m): self.lines.append(m)
    def warn(self, m): self.lines.append(m)
    def error(self, m): self.lines.append(m)
    def alert(self, m): self.lines.append(m)

    def text(self):
        return "\n".join(self.lines)


class StubAPI:
    def __init__(self, margin_response=None, funds_response=None, raises=False):
        self.margin_response = margin_response
        self.funds_response = funds_response
        self.raises = raises
        self.asked = []

    def get_margin(self, **kw):
        self.asked.append(kw)
        if self.raises:
            raise RuntimeError("gateway down")
        return self.margin_response

    def get_funds_view(self):
        if self.raises:
            raise RuntimeError("gateway down")
        return self.funds_response


class StubBroker:
    def __init__(self, api):
        self.cfg = RollConfig(near_token="1769", far_token="1584")
        self.log = StubLog()
        self.client = type("C", (), {"orders": api, "funds": api})()


def ok(body):
    return {"Status": "Success", "Response": body, "Reason": ""}


class TestAskingForTheMargin(unittest.TestCase):
    def test_both_legs_go_in_one_request(self):
        """A calendar spread nets. Asking twice would add two outrights.

        Two separate outright numbers would refuse rolls the account can
        comfortably afford, which is why get_margin takes them together.
        """
        api = StubAPI(margin_response=ok({"TotalMargin": 50000}))
        required(StubBroker(api), "1769", "1584", 1000)

        self.assertEqual(len(api.asked), 1)
        self.assertEqual(api.asked[0]["token_qty"],
                         [("1769", 1000), ("1584", 1000)])

    def test_the_segment_is_passed_through(self):
        api = StubAPI(margin_response=ok({"TotalMargin": 1}))
        required(StubBroker(api), "1769", "1584", 1000)
        self.assertEqual(api.asked[0]["segment_id"], 13)

    def test_the_figure_is_found_wherever_it_sits(self):
        for key in ("TotalMargin", "RequiredMargin", "Margin", "SpanMargin"):
            api = StubAPI(margin_response=ok({key: 1234.5}))
            self.assertEqual(required(StubBroker(api), "1769", "1584", 1000),
                             D("1234.5"), msg=key)

    def test_it_is_found_when_nested(self):
        api = StubAPI(margin_response=ok({"Data": {"Detail": {"TotalMargin": 99}}}))
        self.assertEqual(required(StubBroker(api), "1769", "1584", 1000), D(99))

    def test_an_unrecognised_shape_is_unknown_not_zero(self):
        api = StubAPI(margin_response=ok({"SomethingElse": 500}))
        self.assertIsNone(required(StubBroker(api), "1769", "1584", 1000))

    def test_a_refusal_is_unknown(self):
        api = StubAPI(margin_response={"Status": "Fail", "Response": "nope"})
        self.assertIsNone(required(StubBroker(api), "1769", "1584", 1000))

    def test_an_exception_is_unknown_rather_than_raised(self):
        self.assertIsNone(required(StubBroker(StubAPI(raises=True)),
                                   "1769", "1584", 1000))

    def test_an_old_client_without_get_margin_is_unknown(self):
        class Old:
            pass

        broker = StubBroker(StubAPI())
        broker.client = type("C", (), {"orders": Old(), "funds": Old()})()
        self.assertIsNone(required(broker, "1769", "1584", 1000))


class TestAvailableFunds(unittest.TestCase):
    def test_the_figure_is_found(self):
        for key in ("AvailableMargin", "AvailableBalance", "NetCash"):
            api = StubAPI(funds_response=ok({key: 900000}))
            self.assertEqual(available(StubBroker(api)), D(900000), msg=key)

    def test_an_unrecognised_shape_is_unknown(self):
        api = StubAPI(funds_response=ok({"Mystery": 1}))
        self.assertIsNone(available(StubBroker(api)))

    def test_a_failure_is_unknown(self):
        self.assertIsNone(available(StubBroker(StubAPI(raises=True))))


class TestTheVerdict(unittest.TestCase):
    def build(self, margin, funds):
        api = StubAPI(margin_response=ok({"TotalMargin": margin}) if margin is not None
                      else {"Status": "Fail", "Response": "no"},
                      funds_response=ok({"AvailableMargin": funds}) if funds is not None
                      else {"Status": "Fail", "Response": "no"})
        return estimate(StubBroker(api), "1769", "1584", 1000)

    def test_enough_is_affordable(self):
        got = self.build(50000, 900000)
        self.assertTrue(got.affordable)
        self.assertEqual(got.headroom, D(850000))
        self.assertIn("enough", got.describe())

    def test_not_enough_is_not(self):
        got = self.build(900000, 50000)
        self.assertFalse(got.affordable)
        self.assertIn("NOT ENOUGH", got.describe())

    def test_exactly_enough_is_enough(self):
        self.assertTrue(self.build(50000, 50000).affordable)

    def test_an_unreadable_margin_is_unknown_not_affordable(self):
        """The optimistic guess here is how a naked short happens."""
        got = self.build(None, 900000)
        self.assertIsNone(got.affordable)
        self.assertFalse(got.known)
        self.assertIn("could not be read", got.detail)

    def test_unreadable_funds_are_unknown_too(self):
        got = self.build(50000, None)
        self.assertIsNone(got.affordable)
        self.assertIn("available funds could not be read", got.detail)

    def test_neither_readable_says_so(self):
        self.assertIn("neither", self.build(None, None).detail)

    def test_no_quantity_needs_no_margin(self):
        api = StubAPI(margin_response=ok({"TotalMargin": 1}))
        got = estimate(StubBroker(api), "1769", "1584", 0)
        self.assertIsNone(got.required)
        self.assertIn("nothing to size", got.detail)

    def test_the_description_is_in_rupees(self):
        self.assertIn("Rs", self.build(50000, 900000).describe())


class TestTheGate(unittest.TestCase):
    def report(self, margin_estimate, **cfg_kw):
        from rollover import gates
        from tests.test_execution import instrument
        from rollover import rule
        from rollover.quotes import Quote

        cfg_kw.setdefault("dry_run", False)
        cfg_kw.setdefault("limit_mode", "absolute")
        cfg = RollConfig(near_token="1769", far_token="1584", **cfg_kw)

        class Session:
            logged_in = True
            near = instrument("1769", "USDINR26SEPFUT")
            far = instrument("1584", "USDINR26NOVFUT")
            market_open = True
            near_position_qty = 5000
            clips_done_today = 0
            in_flight = False
            halted_reason = None
            margin = margin_estimate

        near_q = Quote("1769", D("95.9400"), D("95.9450"), 0.0, D("1"),
                       bid_qty=5000, ask_qty=5000)
        far_q = Quote("1584", D("96.2370"), D("96.2375"), 0.0, D("1"),
                      bid_qty=5000, ask_qty=5000)
        decision = rule.compute(near_q, far_q, cfg)
        return gates.evaluate(cfg, Session(), {"1769": near_q, "1584": far_q},
                              decision)

    def gate(self, report):
        return next(g for g in report.gates if g.name == "margin")

    def test_enough_passes(self):
        self.assertTrue(self.gate(self.report(
            Estimate(D(50000), D(900000), ""))).ok)

    def test_not_enough_blocks(self):
        self.assertFalse(self.gate(self.report(
            Estimate(D(900000), D(50000), ""))).ok)

    def test_unknown_blocks(self):
        self.assertFalse(self.gate(self.report(
            Estimate(None, D(900000), "could not be read"))).ok)

    def test_not_yet_checked_blocks(self):
        self.assertFalse(self.gate(self.report(None)).ok)

    def test_dry_run_does_not_block(self):
        self.assertTrue(self.gate(self.report(None, dry_run=True)).ok)

    def test_it_can_be_switched_off(self):
        self.assertTrue(self.gate(self.report(None, require_margin=False)).ok)


class TestNothingIsSentWithoutIt(ExecutionCase):
    """The gate is a screen. This is the thing that stops the order."""

    def test_an_unaffordable_roll_sends_nothing(self):
        engine = self.build([outcome(1000, 1000), outcome(1000, 1000)],
                            margin=900000, funds=50000)
        engine._execute(self.decision, self.near_q, self.far_q)

        self.assertEqual(self.broker.sent, [])
        self.assertIn("NOT SENDING", self.log.text())
        self.assertIn("NOT ENOUGH", self.log.text())

    def test_an_unknown_margin_sends_nothing(self):
        engine = self.build([outcome(1000, 1000)], margin=None)
        engine._execute(self.decision, self.near_q, self.far_q)
        self.assertEqual(self.broker.sent, [])
        self.assertIn("NOT SENDING", self.log.text())

    def test_it_disarms_rather_than_retrying_every_tick(self):
        engine = self.build([outcome(1000, 1000)], margin=900000, funds=50000)
        engine.arm()
        engine._execute(self.decision, self.near_q, self.far_q)
        self.assertFalse(engine.armed)

    def test_it_is_a_skip_not_a_halt(self):
        """Nothing was sent, so nothing is broken. A halt needs a person."""
        engine = self.build([outcome(1000, 1000)], margin=900000, funds=50000)
        engine._execute(self.decision, self.near_q, self.far_q)
        self.assertIsNone(engine.session.halted_reason)
        self.assertFalse(engine.session.in_flight)

    def test_an_affordable_roll_goes_ahead(self):
        engine = self.build([outcome(1000, 1000), outcome(1000, 1000)],
                            margin=50000, funds=900000)
        engine._execute(self.decision, self.near_q, self.far_q)
        self.assertEqual(len(self.broker.sent), 2)
        self.assertIn("ROLL COMPLETE", self.log.text())

    def test_it_asks_for_the_quantity_actually_being_sent(self):
        """The gate's copy was sized on the configured clip and may be stale."""
        engine = self.build([outcome(300, 300), outcome(300, 300)])
        self.decision.qty = 300
        engine._execute(self.decision, self.near_q, self.far_q)

        # The first ask is the pre-send check. A later one comes from the
        # slow refresh after the roll, which is sized on the configured clip.
        asked = self.broker.margin_api.asked[0]["token_qty"]
        self.assertEqual(asked, [("1769", 300), ("1584", 300)])

    def test_dry_run_asks_nothing(self):
        engine = self.build([outcome(1000, 1000)], dry_run=True)
        engine._execute(self.decision, self.near_q, self.far_q)
        self.assertEqual(self.broker.margin_api.asked, [])

    def test_it_can_be_switched_off(self):
        engine = self.build([outcome(1000, 1000), outcome(1000, 1000)],
                            margin=900000, funds=50000, require_margin=False)
        engine._execute(self.decision, self.near_q, self.far_q)
        self.assertEqual(len(self.broker.sent), 2)


class TestItIsOnByDefault(unittest.TestCase):
    def test_the_default_config_checks_margin(self):
        self.assertTrue(RollConfig().require_margin)

    def test_the_installed_client_can_answer(self):
        """The check is useless against kkunal 1.2.0, which has no get_margin."""
        from choice_api.orders import OrdersAPI
        self.assertTrue(hasattr(OrdersAPI, "get_margin"),
                        "kkunal 1.3.0 or newer is required")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTheRealMarginResponse(unittest.TestCase):
    """The shape, seen from a live account on 21 September 2026.

    Before this the app could not read it at all: the figure lives in
    Span_Summary under the name TotalMgn, which was in none of the spellings
    tried, so every margin estimate came back "could not be read". That
    blocks rather than permits, so it was safe -- and it also meant the gate
    could never pass, which would have stopped a funded account trading.

    The trap is the other direction. _iter_records walks a per-leg record
    BEFORE the summary, so a generic scan that recognised a per-leg spelling
    would report one leg's margin as the roll's: 1,817 instead of 4,597, an
    undercount of about half, in the direction that lets an order through.
    """

    LIVE = {
        "Status": "Success",
        "Response": {
            "Margins": [
                {"Segment": 13, "Token": 1769, "Strike": 0.0, "QTY": 1,
                 "InitialMargin": 1817.0, "ExpMgn": 478.9, "LastRate": 95.78},
                {"Segment": 13, "Token": 1284, "Strike": 0.0, "QTY": 1,
                 "InitialMargin": 1821.0, "ExpMgn": 480.45, "LastRate": 96.09},
            ],
            "Span_Summary": {"Span": 3638.0, "ExpMgn": 959.35,
                             "OptionPremium": 0.0, "MgnBenefit": 0.0,
                             "TotalMgn": 4597.35},
        },
        "Reason": "",
    }

    def test_it_reads_the_basket_total(self):
        self.assertEqual(margin.total_from(self.LIVE), D("4597.35"))

    def test_and_not_one_leg(self):
        """1,817 is the near leg alone. Half the answer, and the dangerous
        half, because it is the one that lets an order through."""
        self.assertNotEqual(margin.total_from(self.LIVE), D("1817.0"))

    def test_nor_the_span_without_the_exposure(self):
        self.assertNotEqual(margin.total_from(self.LIVE), D("3638.0"))

    def test_a_response_with_no_summary_falls_back(self):
        got = margin.total_from({"Response": {"TotalMargin": 1234.5}})
        self.assertEqual(got, D("1234.5"))

    def test_a_response_it_cannot_read_is_unknown_not_zero(self):
        self.assertIsNone(margin.total_from({"Response": {"Something": 1}}))

    def test_no_per_leg_spelling_is_in_the_fallback_list(self):
        """The fallback scans generically and a per-leg record is walked
        first, so a spelling that appears on one leg would be read as the
        whole roll."""
        per_leg = {k.lower() for leg in self.LIVE["Response"]["Margins"]
                   for k in leg}
        listed = {k.lower() for k in margin._MARGIN_KEYS}
        self.assertEqual(per_leg & listed, set())

    def test_the_quantity_sent_comes_back_as_contracts(self):
        """A second, independent confirmation of the unit: 1000 units was
        sent for each leg and the broker echoed QTY 1."""
        legs = self.LIVE["Response"]["Margins"]
        self.assertEqual([leg["QTY"] for leg in legs], [1, 1])

    def test_this_broker_gives_no_calendar_spread_benefit(self):
        """The plan assumed a calendar spread would net down. It does not --
        MgnBenefit is zero and the total is the simple sum -- so the roll
        needs about 4,600 a lot, not the ~2,300 a netted spread would."""
        summary = self.LIVE["Response"]["Span_Summary"]
        self.assertEqual(summary["MgnBenefit"], 0.0)
        legs = self.LIVE["Response"]["Margins"]
        summed = sum(leg["InitialMargin"] + leg["ExpMgn"] for leg in legs)
        self.assertAlmostEqual(summary["TotalMgn"], summed, places=2)
