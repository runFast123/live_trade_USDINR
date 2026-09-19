"""Tests for the tenor based limit.

The client's rule is thirty basis points for a one month roll and fifty for a
two month roll. Two things make that easy to get wrong, and both are pinned
here:

  * A basis point is a share of the price. Thirty bps is 0.2879 at 95.97 and
    0.3150 at 105.00. It equals 0.30 only at exactly 100.
  * A limit set for one tenor is wrong for another. The live config had a one
    month roll running a two month limit and was 74% too loose.
"""
from __future__ import annotations

import time
import unittest
from datetime import date

from rollover import limits, rule
from rollover.config import ConfigError, RollConfig
from rollover.limits import LimitError, from_bps, resolve, to_bps
from rollover.money import D
from rollover.quotes import Quote

SPOT = D("95.9675")


def quote(bid, ask, token="1"):
    return Quote(token, D(bid), D(ask), time.monotonic(), D("1"),
                 bid_qty=5000, ask_qty=5000)


class TestBasisPointMaths(unittest.TestCase):
    def test_the_clients_numbers_at_the_live_price(self):
        self.assertEqual(from_bps(D("30"), SPOT), D("0.2879"))
        self.assertEqual(from_bps(D("50"), SPOT), D("0.4798"))

    def test_thirty_bps_is_not_thirty_paise(self):
        """The near miss that makes this worth a module of its own."""
        self.assertNotEqual(from_bps(D("30"), SPOT), D("0.30"))
        self.assertEqual(from_bps(D("30"), D("100")), D("0.3000"))

    def test_it_scales_with_the_price(self):
        self.assertEqual(from_bps(D("30"), D("90")), D("0.2700"))
        self.assertEqual(from_bps(D("30"), D("105")), D("0.3150"))

    def test_the_round_trip_holds(self):
        self.assertEqual(to_bps(from_bps(D("30"), SPOT), SPOT), D("30.0"))

    def test_the_measured_rolls_land_where_the_client_said(self):
        """One month traded at 32.6 bps and two months at 60.5."""
        self.assertEqual(to_bps(D("0.3125"), D("95.9675")), D("32.6"))
        self.assertEqual(to_bps(D("0.5800"), D("95.9400")), D("60.5"))

    def test_a_nonsense_reference_gives_nothing_rather_than_dividing_by_zero(self):
        self.assertIsNone(to_bps(D("0.30"), D("0")))


class TestTenor(unittest.TestCase):
    def test_the_real_contract_spacings(self):
        sep, oct_, nov = date(2026, 9, 28), date(2026, 10, 28), date(2026, 11, 26)
        self.assertEqual(limits.tenor_days(sep, oct_), 30)
        self.assertEqual(limits.tenor_days(sep, nov), 59)
        self.assertEqual(limits.tenor_months(30), 1)
        self.assertEqual(limits.tenor_months(59), 2)

    def test_a_missing_expiry_gives_no_tenor(self):
        self.assertIsNone(limits.tenor_days(None, date(2026, 10, 28)))
        self.assertIsNone(limits.tenor_months(None))


class TestScheduleMatching(unittest.TestCase):
    schedule = {1: D("30"), 2: D("50")}

    def test_a_one_month_roll_takes_the_one_month_limit(self):
        self.assertEqual(limits.match_tenor(30, self.schedule, 10), (1, D("30")))

    def test_a_two_month_roll_takes_the_two_month_limit(self):
        self.assertEqual(limits.match_tenor(59, self.schedule, 10), (2, D("50")))

    def test_a_weekly_roll_is_refused_rather_than_given_a_months_allowance(self):
        """Three days is not one month, however the calendar labels it.

        On a plain month-name comparison 28 Sep to 1 Oct looks like one month,
        and would be handed ten times the allowance it deserves.
        """
        with self.assertRaises(LimitError) as ctx:
            limits.match_tenor(3, self.schedule, 10)
        self.assertIn("not within", str(ctx.exception))

    def test_a_tenor_with_no_entry_is_refused(self):
        with self.assertRaises(LimitError) as ctx:
            limits.match_tenor(180, self.schedule, 10)     # six months
        self.assertIn("Add an entry", str(ctx.exception))

    def test_the_nearest_tenor_wins(self):
        self.assertEqual(limits.match_tenor(36, self.schedule, 10)[0], 1)
        self.assertEqual(limits.match_tenor(55, self.schedule, 10)[0], 2)

    def test_an_empty_schedule_is_refused(self):
        with self.assertRaises(LimitError):
            limits.match_tenor(30, {}, 10)


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.cfg = RollConfig()          # bps mode, the client's schedule

    def test_one_month(self):
        limit = resolve(self.cfg, SPOT, 30)
        self.assertEqual(limit.mode, "bps")
        self.assertEqual(limit.bps, D("30"))
        self.assertEqual(limit.rupees, D("0.2879"))
        self.assertEqual(limit.tenor_months, 1)

    def test_two_months(self):
        limit = resolve(self.cfg, SPOT, 59)
        self.assertEqual(limit.bps, D("50"))
        self.assertEqual(limit.rupees, D("0.4798"))
        self.assertEqual(limit.tenor_months, 2)

    def test_absolute_mode_ignores_tenor(self):
        cfg = RollConfig(limit_mode="absolute", roll_limit="0.30")
        for days in (30, 59, 180):
            self.assertEqual(resolve(cfg, SPOT, days).rupees, D("0.3000"))

    def test_an_unknown_tenor_is_refused_not_defaulted(self):
        with self.assertRaises(LimitError) as ctx:
            resolve(self.cfg, SPOT, None)
        self.assertIn("tenor", str(ctx.exception))

    def test_no_reference_price_is_refused(self):
        with self.assertRaises(LimitError):
            resolve(self.cfg, None, 30)


class TestTheRuleWithTenorLimits(unittest.TestCase):
    """End to end through rule.compute, at the prices actually observed."""

    def setUp(self):
        self.cfg = RollConfig()

    def decide(self, near_bid, near_ask, far_ask, days):
        return rule.compute(quote(near_bid, near_ask),
                            quote(str(D(far_ask) - D("0.0025")), far_ask),
                            self.cfg, days=days)

    def test_the_one_month_roll_as_measured_is_too_dear(self):
        d = self.decide("95.9675", "95.9700", "96.2800", 30)
        self.assertEqual(d.roll_cost, D("0.3125"))
        self.assertEqual(d.cost_bps, D("32.6"))
        self.assertEqual(d.limit_bps, D("30"))
        self.assertFalse(d.qualifies)

    def test_the_two_month_roll_as_measured_is_too_dear(self):
        d = self.decide("95.9400", "95.9450", "96.5200", 59)
        self.assertEqual(d.roll_cost, D("0.5800"))
        self.assertEqual(d.cost_bps, D("60.5"))
        self.assertEqual(d.limit_bps, D("50"))
        self.assertFalse(d.qualifies)

    def test_a_one_month_roll_inside_thirty_bps_trades(self):
        d = self.decide("95.9675", "95.9700", "96.2450", 30)
        self.assertLess(d.cost_bps, D("30"))
        self.assertTrue(d.qualifies)

    def test_the_same_cost_is_judged_differently_by_tenor(self):
        """0.40 is too dear for one month and fine for two.

        This is the whole point of the change: one number cannot serve both.
        """
        cheap_for_two = self.decide("95.9675", "95.9700", "96.3675", 59)
        dear_for_one = self.decide("95.9675", "95.9700", "96.3675", 30)
        self.assertEqual(cheap_for_two.roll_cost, dear_for_one.roll_cost)
        self.assertTrue(cheap_for_two.qualifies)
        self.assertFalse(dear_for_one.qualifies)

    def test_the_old_fixed_limit_would_have_been_too_loose(self):
        """0.50 against a one month roll was 52 bps, not 30."""
        loose = RollConfig(limit_mode="absolute", roll_limit="0.50")
        d = rule.compute(quote("95.9675", "95.9700"),
                         quote("96.2775", "96.2800"), loose, days=30)
        self.assertTrue(d.qualifies)             # it would have rolled

        strict = rule.compute(quote("95.9675", "95.9700"),
                              quote("96.2775", "96.2800"), RollConfig(), days=30)
        self.assertFalse(strict.qualifies)       # under the client's rule it does not

    def test_an_unmatched_tenor_blocks_rather_than_guessing(self):
        d = self.decide("95.9675", "95.9700", "96.2800", 3)     # a weekly
        self.assertFalse(d.qualifies)
        self.assertIn("not within", " ".join(d.blockers))

    def test_no_tenor_at_all_blocks(self):
        d = self.decide("95.9675", "95.9700", "96.2800", None)
        self.assertFalse(d.qualifies)
        self.assertIsNone(d.limit_detail)

    def test_the_reference_is_the_near_mid_not_our_own_side(self):
        d = self.decide("95.9675", "95.9700", "96.2800", 30)
        self.assertEqual(d.reference, D("95.9688"))


class TestConfigValidation(unittest.TestCase):
    def test_the_default_is_the_clients_schedule(self):
        cfg = RollConfig()
        self.assertEqual(cfg.limit_mode, "bps")
        self.assertEqual(cfg.limit_bps_schedule, {"1": "30", "2": "50"})

    def test_a_bad_mode_is_rejected(self):
        with self.assertRaises(ConfigError):
            RollConfig(limit_mode="percent").validate()

    def test_an_empty_schedule_in_bps_mode_is_rejected(self):
        with self.assertRaises(ConfigError):
            RollConfig(limit_bps_schedule={}).validate()

    def test_a_wide_tolerance_is_rejected(self):
        """Past a fortnight the tenors start to overlap."""
        with self.assertRaises(ConfigError):
            RollConfig(tenor_tolerance_days=20).validate()

    def test_absolute_mode_does_not_need_a_schedule(self):
        RollConfig(limit_mode="absolute", limit_bps_schedule={}).validate()


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTheLimitIsNeverLooserThanAsked(unittest.TestCase):
    """A limit rounds DOWN onto the price grid, never to nearest.

    A roll cost is the difference of two tick-grid prices, so it always lands
    exactly on the four-place grid. A limit rounded to nearest can land on
    that grid from below -- and then a cost sitting exactly on it is accepted
    although it is above what the client asked for. Found by recomputing the
    limit in full precision and comparing: up to 0.0000477 rupees a unit,
    about five paise on a thousand. Nothing in money, the wrong direction in
    principle, and free to fix.
    """

    def exact(self, bps, reference):
        return D(bps) / D(10000) * D(reference)

    def test_it_never_exceeds_the_exact_figure(self):
        over = []
        for bps in ("25", "30", "50", "57"):
            for cents in range(9300000, 9900000, 911):
                reference = D(cents).scaleb(-5)
                used = limits.from_bps(D(bps), reference)
                if used > self.exact(bps, reference):
                    over.append((bps, reference))
        self.assertEqual(over, [])

    def test_the_known_case_rounds_down_now(self):
        """25 bps of 95.98092 is 0.2399523, which used to become 0.2400."""
        self.assertEqual(limits.from_bps(D("25"), D("95.98092")), D("0.2399"))

    def test_it_stays_on_the_four_place_grid(self):
        got = limits.from_bps(D("30"), D("95.9562"))
        self.assertEqual(got.as_tuple().exponent, -4)

    def test_an_exact_figure_is_not_moved(self):
        """0.2500 is already on the grid; rounding down must not shave it."""
        self.assertEqual(limits.from_bps(D("25"), D("100")), D("0.2500"))

    def test_the_loss_is_never_more_than_one_grid_step(self):
        for cents in range(9500000, 9600000, 137):
            reference = D(cents).scaleb(-5)
            gap = self.exact("30", reference) - limits.from_bps(D("30"), reference)
            self.assertGreaterEqual(gap, 0)
            self.assertLess(gap, D("0.0001"))


class TestTheBookAndTheOrderUseDifferentUnits(unittest.TestCase):
    """The broker gave both answers, and they only fit together one way.

    An order quantity is in units of the underlying: one USDINR contract is
    qty 1000, and a quantity must be an exact multiple of the lot size. The
    book is quoted in contracts. That second part is not a guess -- it
    follows from the first. If every resting order is a multiple of 1,000
    units then the total resting at a price is a sum of multiples of 1,000,
    so a book showing 7, or 29, or 302 cannot be in units.

    Read as units, a one lot clip needed a thousand lots resting to trade,
    and the touch-size gate blocked essentially every observation of a full
    trading day.
    """

    def reader(self, lot=1000, convert=True):
        from datetime import date

        from rollover.broker import InstrumentInfo
        from rollover.quotes import QuoteReader

        cfg = RollConfig(near_token="1769", far_token="1584",
                         depth_in_lots=convert)
        reader = QuoteReader(None, cfg)
        reader.set_instruments({
            t: InstrumentInfo(token=t, symbol="USDINR", sec_desc=t,
                              segment="13", lot_size=lot,
                              expiry=date(2026, 9, 28), instrument="FUTCUR",
                              price_divisor=D("1"), tick=D("0.0025"),
                              tick_units=D("25000"), low_range=D("93"),
                              high_range=D("99"))
            for t in ("1769", "1584")})
        return reader

    def payload(self, near_size=338, far_size=7):
        return {"Response": {"MultipleTouchline": [
            {"Token": 1769,
             "Buy": [{"Price": 95.9200, "Qty": near_size}],
             "Sell": [{"Price": 95.9225, "Qty": near_size}]},
            {"Token": 1584,
             "Buy": [{"Price": 96.4500, "Qty": far_size}],
             "Sell": [{"Price": 96.5200, "Qty": far_size}]},
        ]}}

    def quotes(self, **kw):
        reader = self.reader(**{k: v for k, v in kw.items()
                                if k in ("lot", "convert")})
        return reader.parse(self.payload(**{k: v for k, v in kw.items()
                                            if k.endswith("_size")}),
                            ["1769", "1584"], time.time())

    def test_a_size_of_seven_becomes_seven_thousand_units(self):
        self.assertEqual(self.quotes()["1584"].ask_qty, 7000)

    def test_and_the_near_side_too(self):
        self.assertEqual(self.quotes()["1769"].bid_qty, 338000)

    def test_a_different_lot_size_scales_differently(self):
        self.assertEqual(self.quotes(lot=100)["1584"].ask_qty, 700)

    def test_it_can_be_switched_off_if_the_feed_ever_changes(self):
        self.assertEqual(self.quotes(convert=False)["1584"].ask_qty, 7)

    def test_an_unknown_lot_size_leaves_the_figure_alone(self):
        """Scaling by a guess is worse than not scaling: the gate then
        blocks, which is the safe direction."""
        self.assertEqual(self.quotes(lot=0)["1584"].ask_qty, 7)

    def test_a_missing_size_stays_missing(self):
        got = self.quotes(far_size=None)
        self.assertIsNone(got["1584"].ask_qty)
