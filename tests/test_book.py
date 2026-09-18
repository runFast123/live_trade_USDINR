"""Several rolls competing for one September.

Two sections that both sell September are two claims on one inventory. The
arithmetic here is what stops the same position being sold twice, which would
leave a short in a month about to expire and cannot be undone by noticing it
afterwards.
"""
from __future__ import annotations

import unittest

from rollover.book import (Candidate, Claim, allocation,
                           claims_from, explain, pick)
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
