"""Which leg goes out first.

The decision that turns a half roll from the expected outcome into a rare one,
and the one that can invert the problem if it is made carelessly. Every case
here is a number and a verdict; there is no clock, no display and no network.
"""
from __future__ import annotations

import unittest

from rollover.sequencing import (AUTO, FAR_FIRST, NEAR_DEPTH_MULTIPLE,
                                 NEAR_FIRST, Sequence, choose, valid_mode)

CLIP = 25000


class TestTheThinFarBook(unittest.TestCase):
    """The case far-first exists for."""

    def test_a_far_book_that_cannot_fill_the_clip_goes_first(self):
        got = choose(CLIP, near_bid_qty=400000, far_ask_qty=8000)
        self.assertTrue(got.far_first)
        self.assertIn("far leg sets the size", got.reason)

    def test_a_far_book_that_can_fill_the_clip_does_not(self):
        """Both legs fill trivially, so near-first, which leaves you flat."""
        got = choose(CLIP, near_bid_qty=400000, far_ask_qty=80000)
        self.assertTrue(got.near_first)

    def test_exactly_enough_is_enough(self):
        self.assertTrue(choose(CLIP, 400000, far_ask_qty=CLIP).near_first)

    def test_one_short_is_thin(self):
        self.assertTrue(choose(CLIP, 400000, far_ask_qty=CLIP - 1).far_first)


class TestTheNearDepthGuard(unittest.TestCase):
    """Far-first commits you to selling the near leg afterwards.

    If that sale then fails you are long both months, which is the mirror of
    the problem it was meant to solve and no better.
    """

    def test_a_shallow_near_book_blocks_far_first(self):
        got = choose(CLIP, near_bid_qty=CLIP, far_ask_qty=8000)
        self.assertTrue(got.near_first)
        self.assertIn("before committing to sell it second", got.reason)

    def test_three_times_the_clip_is_enough(self):
        got = choose(CLIP, near_bid_qty=CLIP * 3, far_ask_qty=8000)
        self.assertTrue(got.far_first)

    def test_one_short_of_three_times_is_not(self):
        got = choose(CLIP, near_bid_qty=CLIP * 3 - 1, far_ask_qty=8000)
        self.assertTrue(got.near_first)

    def test_the_multiple_is_adjustable(self):
        got = choose(CLIP, near_bid_qty=CLIP * 2, far_ask_qty=8000,
                     near_depth_multiple=2)
        self.assertTrue(got.far_first)

    def test_a_nonsense_multiple_still_requires_the_clip(self):
        for multiple in (0, -5):
            got = choose(CLIP, near_bid_qty=CLIP - 1, far_ask_qty=8000,
                         near_depth_multiple=multiple)
            self.assertTrue(got.near_first, msg=multiple)

    def test_the_default_multiple_is_three(self):
        self.assertEqual(NEAR_DEPTH_MULTIPLE, 3)


class TestUnknownSizes(unittest.TestCase):
    """An unknown size is not a large one."""

    def test_an_unknown_near_size_blocks_far_first(self):
        got = choose(CLIP, near_bid_qty=None, far_ask_qty=8000)
        self.assertTrue(got.near_first)
        self.assertIn("near bid size is unknown", got.reason)

    def test_an_unknown_far_size_is_not_assumed_thin(self):
        got = choose(CLIP, near_bid_qty=400000, far_ask_qty=None)
        self.assertTrue(got.near_first)
        self.assertIn("far ask size is unknown", got.reason)

    def test_neither_known_is_near_first(self):
        self.assertTrue(choose(CLIP, None, None).near_first)


class TestExpiryDay(unittest.TestCase):
    """Unconditional, because after 12:30 the near contract is gone.

    A sold near leg with no far leg cannot be corrected at any price on expiry
    day: the instrument you would buy back stops trading.
    """

    def test_it_is_far_first_whatever_the_books_say(self):
        for near, far in ((400000, 80000), (0, 0), (None, None), (1, 999999)):
            got = choose(CLIP, near, far, expiry_day=True)
            self.assertTrue(got.far_first, msg=(near, far))
            self.assertIn("expiry day", got.reason)

    def test_it_overrides_a_configured_near_first(self):
        """"Unconditional" has to mean unconditional, or it is a preference."""
        got = choose(CLIP, 400000, 80000, expiry_day=True, mode=NEAR_FIRST)
        self.assertTrue(got.far_first)

    def test_it_overrides_a_shallow_near_book(self):
        got = choose(CLIP, near_bid_qty=1, far_ask_qty=1, expiry_day=True)
        self.assertTrue(got.far_first)


class TestTheConfiguredMode(unittest.TestCase):
    def test_near_first_is_honoured(self):
        got = choose(CLIP, 400000, far_ask_qty=8000, mode=NEAR_FIRST)
        self.assertTrue(got.near_first)
        self.assertIn("config", got.reason)

    def test_far_first_is_honoured_even_against_the_depth_guard(self):
        """An explicit instruction is the operator's to give."""
        got = choose(CLIP, near_bid_qty=1, far_ask_qty=999999, mode=FAR_FIRST)
        self.assertTrue(got.far_first)

    def test_auto_is_the_default(self):
        self.assertEqual(choose(CLIP, 400000, 8000).order,
                         choose(CLIP, 400000, 8000, mode=AUTO).order)

    def test_the_modes_are_recognised(self):
        for mode in (AUTO, NEAR_FIRST, FAR_FIRST):
            self.assertTrue(valid_mode(mode))
        for mode in ("", "sideways", "FIRST", None):
            self.assertFalse(valid_mode(mode))

    def test_case_and_space_are_forgiven_when_validating(self):
        self.assertTrue(valid_mode(" Far_First "))


class TestDegenerateInputs(unittest.TestCase):
    def test_no_clip_is_near_first(self):
        for clip in (0, -1000):
            got = choose(clip, 400000, 8000)
            self.assertTrue(got.near_first, msg=clip)

    def test_a_zero_far_book_is_thin(self):
        self.assertTrue(choose(CLIP, 400000, far_ask_qty=0).far_first)

    def test_a_zero_near_book_blocks_far_first(self):
        self.assertTrue(choose(CLIP, near_bid_qty=0, far_ask_qty=0).near_first)


class TestItSaysWhy(unittest.TestCase):
    """The reason is logged and shown, so it has to read as a sentence."""

    def test_every_branch_gives_a_reason(self):
        cases = [
            choose(CLIP, 400000, 8000),
            choose(CLIP, 400000, 80000),
            choose(CLIP, CLIP, 8000),
            choose(CLIP, None, 8000),
            choose(CLIP, 400000, None),
            choose(CLIP, 400000, 8000, expiry_day=True),
            choose(CLIP, 400000, 8000, mode=NEAR_FIRST),
            choose(CLIP, 400000, 8000, mode=FAR_FIRST),
            choose(0, 400000, 8000),
        ]
        for got in cases:
            self.assertTrue(got.reason.strip(), msg=got.order)
            self.assertGreater(len(got.reason), 12, msg=got.reason)

    def test_the_description_names_the_leg(self):
        self.assertIn("far leg first", choose(CLIP, 400000, 8000).describe())
        self.assertIn("near leg first", choose(CLIP, 400000, 80000).describe())

    def test_the_two_orders_are_exclusive(self):
        for got in (choose(CLIP, 400000, 8000), choose(CLIP, 400000, 80000)):
            self.assertNotEqual(got.far_first, got.near_first)

    def test_a_sequence_is_immutable(self):
        got = Sequence(FAR_FIRST, "because")
        with self.assertRaises(Exception):
            got.order = NEAR_FIRST


class TestTheRealBooksObserved(unittest.TestCase):
    """The depths actually seen on these contracts, in units not lots.

    Far 8 to 80 lots at the touch, near 14 to 400. At a 25 lot clip the far
    book is usually the binding side, which is the whole reason for this.
    """

    def clip(self, lots):
        return lots * 1000

    def test_a_typical_book_at_25_lots_goes_far_first(self):
        got = choose(self.clip(25), near_bid_qty=self.clip(400),
                     far_ask_qty=self.clip(8))
        self.assertTrue(got.far_first)

    def test_a_good_far_book_at_25_lots_goes_near_first(self):
        got = choose(self.clip(25), near_bid_qty=self.clip(400),
                     far_ask_qty=self.clip(80))
        self.assertTrue(got.near_first)

    def test_a_thin_near_book_at_25_lots_refuses_far_first(self):
        """Near 14 lots against a 25 lot clip: cannot sell it second."""
        got = choose(self.clip(25), near_bid_qty=self.clip(14),
                     far_ask_qty=self.clip(8))
        self.assertTrue(got.near_first)

    def test_one_lot_is_comfortable_either_way(self):
        got = choose(self.clip(1), near_bid_qty=self.clip(14),
                     far_ask_qty=self.clip(8))
        self.assertTrue(got.near_first, "a 1 lot clip fits in both books")


class TestFarFirstCanActuallyReachAnOrder(unittest.TestCase):
    """The touch-size gate and far-first were exactly complementary.

    Far-first is chosen when the far ask is BELOW the clip. The far touch gate
    passed when the far ask was AT OR ABOVE it. So every market that selected
    far-first was a market the gate blocked, and no far-first order could ever
    have been sent. The feature was dead on arrival.

    The gate's reasoning - "sell the near leg in full and then find only a
    handful on the far offer" - is about the SECOND leg. When the far leg goes
    first, a thin far book is not the hazard, it is the premise.
    """

    def setUp(self):
        import time
        from datetime import date
        from rollover import gates, rule
        from rollover.broker import InstrumentInfo
        from rollover.config import RollConfig
        from rollover.money import D
        from rollover.quotes import Quote

        self.gates, self.rule, self.D, self.Quote = gates, rule, D, Quote
        self.now = time.monotonic()

        def contract(token, expiry):
            return InstrumentInfo(
                token=token, symbol="USDINR", sec_desc="USDINR" + token,
                segment="13", lot_size=1000, expiry=expiry,
                instrument="FUTCUR", price_divisor=D("10000000"),
                tick=D("0.0025"), tick_units=D("25000"),
                low_range=D("93"), high_range=D("99"))

        class Session:
            logged_in = True
            near = contract("1769", date(2026, 9, 28))
            far = contract("1584", date(2026, 11, 26))
            market_open = True
            near_position_qty = 500000
            margin = None
            clips_done_today = 0
            in_flight = False
            halted_reason = None
            scrip_file_date = date.today()

        self.session = Session()
        self.cfg = RollConfig(near_token="1769", far_token="1584",
                              near_expiry="2026-09-28", far_expiry="2026-11-26",
                              lots=25, dry_run=True, require_margin=False,
                              limit_mode="absolute")

    def report(self, far_depth, sequence=None, near_depth=400000):
        nq = self.Quote("1769", self.D("95.9400"), self.D("95.9450"), self.now,
                        self.D(1), bid_qty=near_depth, ask_qty=near_depth)
        fq = self.Quote("1584", self.D("96.2370"), self.D("96.2375"), self.now,
                        self.D(1), bid_qty=far_depth, ask_qty=far_depth)
        decision = self.rule.compute(nq, fq, self.cfg)
        return self.gates.evaluate(self.cfg, self.session,
                                   {"1769": nq, "1584": fq}, decision,
                                   sequence=sequence)

    def gate(self, report, name):
        return next(g for g in report.gates if g.name == name)

    def test_the_market_that_selects_far_first_used_to_be_blocked(self):
        """The defect, stated as the arithmetic that produced it."""
        clip = self.cfg.clip_qty
        thin = 8000
        self.assertLess(thin, clip, "this market selects far-first")
        self.assertTrue(choose(clip, 400000, thin).far_first)
        self.assertFalse(self.gate(self.report(thin), "far touch size").ok)

    def test_with_the_sequence_known_that_market_passes(self):
        clip = self.cfg.clip_qty
        seq = choose(clip, 400000, 8000)
        self.assertTrue(seq.far_first)
        self.assertTrue(self.gate(self.report(8000, seq), "far touch size").ok)

    def test_the_depth_is_still_reported_not_hidden(self):
        seq = choose(self.cfg.clip_qty, 400000, 8000)
        detail = self.gate(self.report(8000, seq), "far touch size").detail
        self.assertIn("8000", detail)
        self.assertIn("not required", detail)

    def test_the_near_leg_is_still_required_to_cover_the_clip(self):
        """Far-first commits you to selling near. That leg still has to fill."""
        seq = choose(self.cfg.clip_qty, 400000, 8000)
        report = self.report(8000, seq, near_depth=1000)
        self.assertFalse(self.gate(report, "near touch size").ok)

    def test_near_first_is_unchanged(self):
        seq = choose(self.cfg.clip_qty, 400000, 80000)
        self.assertTrue(seq.near_first)
        self.assertTrue(self.gate(self.report(80000, seq), "far touch size").ok)
        self.assertFalse(self.gate(self.report(8000, seq), "far touch size").ok)

    def test_no_sequence_given_behaves_as_before(self):
        """Every existing caller passes nothing, and must be unaffected."""
        self.assertFalse(self.gate(self.report(8000), "far touch size").ok)
        self.assertTrue(self.gate(self.report(80000), "far touch size").ok)

    def test_a_missing_far_size_still_blocks_even_under_far_first(self):
        """No size at all is not a thin book; it is an unreadable one."""
        seq = choose(self.cfg.clip_qty, 400000, 8000)
        report = self.report(None, seq)
        self.assertFalse(self.gate(report, "far touch size").ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
