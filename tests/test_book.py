"""Several rolls competing for one September.

Two sections that both sell September are two claims on one inventory. The
arithmetic here is what stops the same position being sold twice, which would
leave a short in a month about to expire and cannot be undone by noticing it
afterwards.
"""
from __future__ import annotations

import unittest

from rollover.book import (Candidate, Claim, allocation,
                           claims_from, explain, pick, pools,
                           sellable)
from rollover.money import D


def claim(name, total, done=0):
    return Claim(name=name, near_token="1769", far_token="X",
                 total=total, done=done)


class TestTheAllocationFits(unittest.TestCase):
    def test_two_ladders_inside_the_position(self):
        got = allocation([claim("Sep->Oct", 30000), claim("Sep->Nov", 40000)],
                         held=100000)
        self.assertTrue(got.fits)
        self.assertEqual(got.outstanding, 70000)
        self.assertEqual(got.spare, 30000)
        self.assertIsNone(got.refusal())

    def test_exactly_the_position_fits(self):
        got = allocation([claim("a", 60000), claim("b", 40000)], held=100000)
        self.assertTrue(got.fits)
        self.assertEqual(got.spare, 0)

    def test_one_unit_over_does_not(self):
        got = allocation([claim("a", 60000), claim("b", 40001)], held=100000)
        self.assertFalse(got.fits)
        self.assertIsNotNone(got.refusal())

    def test_a_single_section_still_has_to_fit(self):
        got = allocation([claim("only", 150000)], held=100000)
        self.assertFalse(got.fits)

    def test_no_sections_always_fit(self):
        self.assertTrue(allocation([], held=100000).fits)
        self.assertEqual(allocation([], held=100000).outstanding, 0)


class TestTheRefusalSaysWhatToDo(unittest.TestCase):
    def setUp(self):
        self.why = allocation([claim("Sep->Oct", 60000),
                               claim("Sep->Nov", 60000)], held=100000).refusal()

    def test_it_names_the_total_and_the_position(self):
        self.assertIn("120,000", self.why)
        self.assertIn("100,000", self.why)

    def test_it_names_each_section(self):
        self.assertIn("Sep->Oct 60,000", self.why)
        self.assertIn("Sep->Nov 60,000", self.why)

    def test_it_says_what_the_consequence_would_be(self):
        self.assertIn("sold twice", self.why)


class TestTheInvariantHoldsAsItRuns(unittest.TestCase):
    """Rolling reduces both sides, so the check stays true without redoing it.

    Thirty thousand rolled out of a hundred thousand leaves seventy thousand
    held and thirty thousand less to roll.
    """

    def test_after_a_partial_roll_it_still_fits(self):
        got = allocation([claim("Sep->Oct", 30000, done=30000),
                          claim("Sep->Nov", 40000)], held=70000)
        self.assertTrue(got.fits)
        self.assertEqual(got.outstanding, 40000)

    def test_a_finished_section_claims_nothing(self):
        got = allocation([claim("done", 30000, done=30000)], held=0)
        self.assertEqual(got.outstanding, 0)
        self.assertTrue(got.fits)

    def test_over_credited_progress_cannot_make_a_claim_negative(self):
        """A hand-edited state file must not free up phantom inventory."""
        got = allocation([claim("a", 30000, done=999999),
                          claim("b", 40000)], held=40000)
        self.assertEqual(got.outstanding, 40000)
        self.assertTrue(got.fits)


class TestASectionWithNoLadder(unittest.TestCase):
    """No ladder is not a claim of nothing. It is a claim of everything.

    A section without a ladder has no campaign cap: it rolls a clip at a time
    for as long as the market allows. Summing it as zero made the whole check
    vacuous -- two sections would total less than the position while one of
    them quietly consumed all of it, which is the exact double sell this
    module exists to prevent.
    """

    def uncapped(self, name="Sep->Nov"):
        return Claim(name=name, near_token="1769", far_token="X",
                     total=None, done=0)

    def test_it_is_uncapped_not_empty(self):
        got = self.uncapped()
        self.assertTrue(got.uncapped)
        self.assertIsNone(got.outstanding)

    def test_it_makes_the_total_unknowable(self):
        got = allocation([claim("Sep->Oct", 100000), self.uncapped()],
                         held=100000)
        self.assertIsNone(got.outstanding)
        self.assertFalse(got.known)

    def test_two_sections_with_one_uncapped_do_not_fit(self):
        got = allocation([claim("Sep->Oct", 100000), self.uncapped()],
                         held=100000)
        self.assertFalse(got.fits)

    def test_it_is_refused_and_says_what_to_do(self):
        why = allocation([claim("Sep->Oct", 100000), self.uncapped()],
                         held=100000).refusal()
        self.assertIn("Sep->Nov", why)
        self.assertIn("no ladder", why)
        self.assertIn("every section needs a ladder", why)

    def test_a_generous_position_does_not_rescue_it(self):
        """There is no position large enough to bound something unlimited."""
        got = allocation([claim("Sep->Oct", 1000), self.uncapped()],
                         held=99_000_000)
        self.assertFalse(got.fits)

    def test_one_uncapped_section_alone_is_the_original_arrangement(self):
        """Single-pair rolling has always been bounded by the position gate."""
        got = allocation([self.uncapped()], held=100000)
        self.assertTrue(got.fits)
        self.assertIsNone(got.refusal())

    def test_the_description_names_the_uncapped_section(self):
        text = allocation([claim("a", 1000), self.uncapped()], held=100000).describe()
        self.assertIn("Sep->Nov", text)
        self.assertIn("no cap", text)

    def test_a_section_that_could_not_be_parsed_is_uncapped_not_empty(self):
        """Its size could not be established, so it must not be assumed small."""
        got = claims_from([{"name": "good", "total": 1000},
                           {"name": "broken", "total": "not a number"}])
        self.assertTrue(got[1].uncapped)
        self.assertFalse(allocation(got, held=999999).fits)

    def test_a_section_with_an_explicit_zero_is_still_zero(self):
        """Nothing to roll is a real, bounded answer. It is not "no ladder"."""
        got = claims_from([{"name": "empty", "total": 0}])
        self.assertFalse(got[0].uncapped)
        self.assertEqual(got[0].outstanding, 0)


class TestAnUnreadablePosition(unittest.TestCase):
    """The check cannot be made against a number nobody has."""

    def test_it_is_not_a_pass(self):
        got = allocation([claim("a", 30000)], held=None)
        self.assertIsNone(got.fits)
        self.assertFalse(got.known)

    def test_it_is_a_refusal(self):
        why = allocation([claim("a", 30000)], held=None).refusal()
        self.assertIsNotNone(why)
        self.assertIn("could not be read", why)

    def test_the_description_says_so_rather_than_showing_a_verdict(self):
        text = allocation([claim("a", 30000)], held=None).describe()
        self.assertIn("not known", text)
        self.assertNotIn("fits", text.replace("not known whether that fits", ""))


class TestSectionsOnlyCompeteOverTheSameContract(unittest.TestCase):
    """Rolling September and rolling October are independent campaigns."""

    def setUp(self):
        self.sep_oct = Claim("Sep->Oct", "1769", "OCT", total=30000, done=0)
        self.sep_nov = Claim("Sep->Nov", "1769", "NOV", total=40000, done=0)
        self.oct_dec = Claim("Oct->Dec", "1800", "DEC", total=50000, done=0)
        self.all = [self.sep_oct, self.sep_nov, self.oct_dec]
        self.held = {"1769": 100000, "1800": 60000}

    def test_one_pool_per_near_contract(self):
        got = pools(self.all, self.held)
        self.assertEqual(sorted(got), ["1769", "1800"])

    def test_only_the_same_near_leg_is_summed(self):
        got = pools(self.all, self.held)
        self.assertEqual(got["1769"].outstanding, 70000)
        self.assertEqual(got["1800"].outstanding, 50000)

    def test_an_overdrawn_pool_does_not_condemn_a_healthy_one(self):
        got = pools(self.all, {"1769": 10000, "1800": 60000})
        self.assertFalse(got["1769"].fits)
        self.assertTrue(got["1800"].fits)

    def test_a_contract_with_no_position_reading_is_unknown(self):
        got = pools(self.all, {"1769": 100000})
        self.assertIsNone(got["1800"].fits)

    def test_nothing_in_gives_nothing_out(self):
        self.assertEqual(pools([], {}), {})
        self.assertEqual(pools(None, None), {})


class TestWhatThisSectionMaySell(unittest.TestCase):
    """The number that makes the existing position gate do the right thing.

    The gate already refuses to sell a clip larger than the position it is
    given. Hand it what is left after the other sections' claims and it starts
    refusing to sell into their allocation too, without the gate knowing
    sections exist at all.
    """

    def setUp(self):
        self.sep_oct = Claim("Sep->Oct", "1769", "OCT", total=30000, done=0)
        self.sep_nov = Claim("Sep->Nov", "1769", "NOV", total=40000, done=0)
        self.all = [self.sep_oct, self.sep_nov]

    def test_a_section_may_not_sell_its_siblings_allocation(self):
        self.assertEqual(sellable(self.all, self.sep_oct, 100000), 60000)
        self.assertEqual(sellable(self.all, self.sep_nov, 100000), 70000)

    def test_the_two_together_exceed_the_position_which_is_the_point(self):
        """Neither may sell it all, and no order can consume both shares."""
        a = sellable(self.all, self.sep_oct, 100000)
        b = sellable(self.all, self.sep_nov, 100000)
        self.assertGreater(a + b, 100000)
        self.assertLess(a, 100000)
        self.assertLess(b, 100000)

    def test_a_sibling_that_has_finished_reserves_nothing(self):
        done = Claim("Sep->Nov", "1769", "NOV", total=40000, done=40000)
        self.assertEqual(sellable([self.sep_oct, done], self.sep_oct, 100000),
                         100000)

    def test_a_lone_section_may_sell_the_whole_position(self):
        self.assertEqual(sellable([self.sep_oct], self.sep_oct, 100000), 100000)

    def test_a_section_selling_a_different_contract_reserves_nothing(self):
        other = Claim("Oct->Dec", "1800", "DEC", total=90000, done=0)
        self.assertEqual(sellable([self.sep_oct, other], self.sep_oct, 100000),
                         100000)

    def test_an_uncapped_sibling_leaves_nothing_guaranteed(self):
        """It could take all of it, so none of it is spoken for by this one."""
        greedy = Claim("Sep->Nov", "1769", "NOV", total=None, done=0)
        self.assertEqual(sellable([self.sep_oct, greedy], self.sep_oct, 100000), 0)

    def test_an_unreadable_position_stays_unreadable(self):
        """The gate already treats unknown as a failure, not as permission."""
        self.assertIsNone(sellable(self.all, self.sep_oct, None))

    def test_siblings_claiming_more_than_is_held_leave_nothing(self):
        big = Claim("Sep->Nov", "1769", "NOV", total=200000, done=0)
        self.assertEqual(sellable([self.sep_oct, big], self.sep_oct, 100000), 0)

    def test_it_never_returns_a_negative(self):
        big = Claim("Sep->Nov", "1769", "NOV", total=999999, done=0)
        self.assertGreaterEqual(sellable([self.sep_oct, big], self.sep_oct, 1), 0)


class TestWhichSectionTrades(unittest.TestCase):
    """Furthest inside its OWN limit, not cheapest in absolute terms."""

    def candidate(self, name, cost, limit, qualifies=True):
        return Candidate(name=name, cost_bps=D(cost), limit_bps=D(limit),
                         qualifies=qualifies, payload=name)

    def test_the_one_doing_better_against_its_own_instruction_wins(self):
        oct_ = self.candidate("Sep->Oct", "26", "30")     # 4 bps inside
        nov = self.candidate("Sep->Nov", "44", "50")      # 6 bps inside
        self.assertIs(pick([oct_, nov]), nov)

    def test_the_absolute_cheapest_does_not_automatically_win(self):
        """26 bps is the smaller number and still loses. That is the point."""
        oct_ = self.candidate("Sep->Oct", "26", "30")
        nov = self.candidate("Sep->Nov", "44", "50")
        self.assertLess(oct_.cost_bps, nov.cost_bps)
        self.assertIs(pick([oct_, nov]), nov)

    def test_a_section_that_does_not_qualify_is_not_picked(self):
        good = self.candidate("good", "26", "30")
        better_but_blocked = self.candidate("blocked", "10", "50",
                                            qualifies=False)
        self.assertIs(pick([better_but_blocked, good]), good)

    def test_nothing_qualifying_picks_nothing(self):
        self.assertIsNone(pick([self.candidate("a", "60", "30", qualifies=False)]))

    def test_an_empty_book_picks_nothing(self):
        self.assertIsNone(pick([]))
        self.assertIsNone(pick(None))

    def test_a_section_that_cannot_be_priced_is_not_picked(self):
        unpriced = Candidate(name="unpriced", cost_bps=None, limit_bps=D("30"),
                             qualifies=True)
        self.assertIsNone(pick([unpriced]))

    def test_a_tie_keeps_the_order_it_was_given(self):
        """Reproducible from the log, rather than depending on dict order."""
        first = self.candidate("first", "26", "30")
        second = self.candidate("second", "46", "50")
        self.assertEqual(first.inside_bps, second.inside_bps)
        self.assertIs(pick([first, second]), first)
        self.assertIs(pick([second, first]), second)

    def test_the_payload_comes_back_with_it(self):
        chosen = pick([self.candidate("Sep->Nov", "44", "50")])
        self.assertEqual(chosen.payload, "Sep->Nov")

    def test_three_sections_choose_the_best_of_them(self):
        a = self.candidate("a", "29", "30")      # 1 inside
        b = self.candidate("b", "44", "50")      # 6 inside
        c = self.candidate("c", "38", "40")      # 2 inside
        self.assertIs(pick([a, b, c]), b)


class TestExplaining(unittest.TestCase):
    """The operator has to be able to see why the other section did not go."""

    def setUp(self):
        self.a = Candidate("Sep->Oct", D("26"), D("30"), True)
        self.b = Candidate("Sep->Nov", D("44"), D("50"), True)
        self.c = Candidate("Sep->Dec", D("62"), D("50"), False)
        self.chosen = pick([self.a, self.b, self.c])

    def test_every_section_gets_a_line(self):
        text = explain([self.a, self.b, self.c], self.chosen)
        self.assertEqual(len(text.splitlines()), 3)

    def test_the_chosen_one_is_marked_once(self):
        text = explain([self.a, self.b, self.c], self.chosen)
        self.assertEqual(text.count("<- trading"), 1)
        self.assertIn("Sep->Nov", text.splitlines()[1])

    def test_a_section_above_its_limit_says_so(self):
        text = explain([self.c], None)
        self.assertIn("12 bps above its limit", text)

    def test_exactly_at_the_limit_is_distinguished(self):
        at = Candidate("at", D("30"), D("30"), False)
        self.assertIn("exactly at its limit", explain([at], None))

    def test_an_unpriced_section_says_so(self):
        blank = Candidate("blank", None, None, False)
        self.assertIn("not priced", explain([blank], None))

    def test_nothing_chosen_marks_nothing(self):
        self.assertNotIn("<- trading", explain([self.a, self.b], None))


class TestBuildingClaims(unittest.TestCase):
    def test_it_reads_a_plain_mapping(self):
        got = claims_from([{"name": "Sep->Oct", "near_token": "1769",
                            "far_token": "1500", "total": 30000, "done": 4000}])
        self.assertEqual(got[0].name, "Sep->Oct")
        self.assertEqual(got[0].outstanding, 26000)

    def test_a_missing_name_gets_a_positional_one(self):
        self.assertEqual(claims_from([{}])[0].name, "section 1")

    def test_an_unreadable_section_still_appears(self):
        """A claim missing from the sum is how the position gets over-committed."""
        got = claims_from([{"total": "not a number"}])
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].halted)

    def test_nothing_in_gives_nothing_out(self):
        self.assertEqual(claims_from([]), [])
        self.assertEqual(claims_from(None), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
