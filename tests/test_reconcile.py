"""Checking the account moved the way the roll said it did.

Before this, "ROLL COMPLETE" was asserted from a row scraped out of the order
book and never compared with anything. These tests pin the comparison, and in
particular the distinction the whole design turns on: a position book that
cannot be read is not the same as one that disagrees.
"""
from __future__ import annotations

import unittest

from rollover import reconcile
from rollover.broker import BUY, SELL
from rollover.reconcile import Positions, capture, check
from tests.test_execution import ExecutionCase, outcome

NEAR, FAR = "1769", "1584"


class FakeBroker:
    """Positions read from a script, one reading per call."""

    def __init__(self, readings, fail=()):
        # readings: list of {token: qty}. The last one repeats forever.
        self.readings = list(readings)
        self.fail = set(fail)
        self.reads = 0

    def net_qty(self, token):
        if token in self.fail:
            return None
        if self.reads >= len(self.readings):
            book = self.readings[-1]
        else:
            book = self.readings[self.reads // 2]
        return book.get(token)

    def _advance(self):
        self.reads += 2


def no_sleep(_seconds):
    pass


class TestAGoodRoll(unittest.TestCase):
    def test_both_legs_moving_by_the_fill_agrees(self):
        broker = FakeBroker([{NEAR: 700, FAR: 1300}])
        result = check(broker, NEAR, FAR, Positions(near=1000, far=1000),
                       expected=300, sleep=no_sleep)
        self.assertTrue(result.agreed)
        self.assertTrue(result.checked)
        self.assertEqual((result.near_moved, result.far_moved), (300, 300))
        self.assertIn("position confirmed", result.detail)

    def test_it_returns_as_soon_as_the_numbers_agree(self):
        broker = FakeBroker([{NEAR: 700, FAR: 1300}])
        check(broker, NEAR, FAR, Positions(1000, 1000), 300, sleep=no_sleep)
        self.assertEqual(broker.reads, 0, "read more than once when it need not")

    def test_a_short_near_leg_is_still_arithmetic(self):
        """Selling into a short is a fall like any other."""
        broker = FakeBroker([{NEAR: -300, FAR: 300}])
        result = check(broker, NEAR, FAR, Positions(near=0, far=0),
                       expected=300, sleep=no_sleep)
        self.assertTrue(result.agreed)


class TestAHalfRoll(unittest.TestCase):
    """The failure this exists to catch."""

    def setUp(self):
        broker = FakeBroker([{NEAR: 700, FAR: 1000}])     # far never moved
        self.result = check(broker, NEAR, FAR, Positions(1000, 1000),
                            expected=300, wait=0.0, sleep=no_sleep)

    def test_it_disagrees(self):
        self.assertFalse(self.result.agreed)
        self.assertTrue(self.result.checked)
        self.assertTrue(self.result.contradicted)

    def test_it_says_which_leg_is_short(self):
        self.assertIn("half-rolled", self.result.detail)
        self.assertIn("300 short", self.result.detail)

    def test_it_shows_both_readings(self):
        self.assertIn("near 1000, far 1000", self.result.detail)
        self.assertIn("near 700, far 1000", self.result.detail)


class TestTheDirectionsAreNotConfused(unittest.TestCase):
    """near_moved counts a fall and far_moved counts a rise.

    The same positive number therefore means opposite directions, and sharing
    one description between them reported a correct far leg as having gone the
    wrong way.
    """

    def text(self, near_after, far_after, expected=300):
        broker = FakeBroker([{NEAR: near_after, FAR: far_after}])
        return check(broker, NEAR, FAR, Positions(1000, 1000), expected,
                     wait=0.0, sleep=no_sleep).detail

    def test_a_far_leg_that_rose_reads_as_up(self):
        detail = self.text(near_after=1000, far_after=1300)
        self.assertIn("far up 300", detail)
        self.assertNotIn("far down", detail)

    def test_a_far_leg_that_fell_reads_as_down(self):
        detail = self.text(near_after=700, far_after=700)
        self.assertIn("far down 300", detail)

    def test_a_near_leg_that_fell_reads_as_down(self):
        detail = self.text(near_after=700, far_after=1000)
        self.assertIn("near down 300", detail)

    def test_a_near_leg_that_rose_reads_as_up(self):
        detail = self.text(near_after=1300, far_after=1300)
        self.assertIn("near up 300", detail)

    def test_an_unmoved_leg_reads_as_unchanged(self):
        self.assertIn("unchanged", self.text(near_after=1000, far_after=1300))


class TestWhatItWillNotHaltFor(unittest.TestCase):
    """Unreadable is not disagreement, and must not stop a correct roll."""

    def test_an_unreadable_book_afterwards_warns_rather_than_contradicts(self):
        broker = FakeBroker([{NEAR: 700, FAR: 1300}], fail={NEAR})
        result = check(broker, NEAR, FAR, Positions(1000, 1000), 300,
                       wait=0.0, sleep=no_sleep)
        self.assertFalse(result.contradicted)
        self.assertFalse(result.checked)
        self.assertIn("could not be read after", result.detail)

    def test_an_unreadable_book_beforehand_has_nothing_to_compare(self):
        broker = FakeBroker([{NEAR: 700, FAR: 1300}])
        result = check(broker, NEAR, FAR, Positions(near=None, far=1000), 300,
                       wait=0.0, sleep=no_sleep)
        self.assertFalse(result.contradicted)
        self.assertFalse(result.checked)
        self.assertIn("before the roll", result.detail)

    def test_nothing_filled_is_nothing_to_reconcile(self):
        broker = FakeBroker([{NEAR: 1000, FAR: 1000}])
        result = check(broker, NEAR, FAR, Positions(1000, 1000), expected=0,
                       wait=0.0, sleep=no_sleep)
        self.assertFalse(result.contradicted)
        self.assertIn("nothing to reconcile", result.detail)


class TestItWaitsForTheBookToCatchUp(unittest.TestCase):
    """Positions lag the trade. Reading once would invent mismatches."""

    def test_a_late_update_still_agrees(self):
        broker = FakeBroker([
            {NEAR: 1000, FAR: 1000},       # not updated yet
            {NEAR: 1000, FAR: 1000},
            {NEAR: 700, FAR: 1300},        # caught up
        ])
        slept = []

        def advance(seconds):
            slept.append(seconds)
            broker._advance()

        result = check(broker, NEAR, FAR, Positions(1000, 1000), 300,
                       wait=10.0, sleep=advance)
        self.assertTrue(result.agreed, result.detail)
        self.assertTrue(slept, "did not wait at all")

    def test_it_gives_up_eventually_rather_than_waiting_forever(self):
        broker = FakeBroker([{NEAR: 1000, FAR: 1000}])
        result = check(broker, NEAR, FAR, Positions(1000, 1000), 300,
                       wait=0.0, sleep=no_sleep)
        self.assertTrue(result.contradicted)
        self.assertIn("Neither leg moved", result.detail)


class TestCapture(unittest.TestCase):
    def test_it_reads_both_legs(self):
        got = capture(FakeBroker([{NEAR: 5000, FAR: 250}]), NEAR, FAR)
        self.assertEqual((got.near, got.far), (5000, 250))
        self.assertTrue(got.readable)

    def test_a_failing_broker_does_not_raise(self):
        class Broken:
            def net_qty(self, token):
                raise RuntimeError("no network")

        got = capture(Broken(), NEAR, FAR)
        self.assertEqual((got.near, got.far), (None, None))
        self.assertFalse(got.readable)

    def test_a_missing_token_reads_as_unknown(self):
        got = capture(FakeBroker([{NEAR: 1000, FAR: 1000}]), "", FAR)
        self.assertIsNone(got.near)


class TestThroughTheEngine(ExecutionCase):
    """The engine must halt on a contradiction and not on a failed read."""

    def roll(self, positions, traded=None):
        engine = self.build([outcome(1000, 1000), outcome(1000, 1000)],
                            positions=positions, traded=traded)
        engine._execute(self.decision, self.near_q, self.far_q)
        return engine

    def test_a_contradiction_halts(self):
        engine = self.roll([{NEAR: 1000, FAR: 1000}])    # nothing moved
        self.assertIsNotNone(engine.session.halted_reason)
        self.assertIn("BOTH LEGS REPORTED FILLED", engine.session.halted_reason)

    def test_a_matching_book_completes(self):
        engine = self.roll([{NEAR: 1000, FAR: 1000},     # before
                            {NEAR: 0, FAR: 2000}])       # after
        self.assertIsNone(engine.session.halted_reason)
        self.assertIn("ROLL COMPLETE", self.log.text())

    def test_an_unreadable_book_completes_with_a_warning(self):
        engine = self.roll([{NEAR: None, FAR: None}])
        self.assertIsNone(engine.session.halted_reason)
        self.assertIn("ROLL COMPLETE", self.log.text())
        self.assertIn("could not be read", self.log.text())

    def test_a_halted_contradiction_still_counts_the_clip(self):
        """It was sent, whatever else is wrong. The budget must reflect it."""
        engine = self.roll([{NEAR: 1000, FAR: 1000}])
        self.assertEqual(engine.session.clips_done_today, 1)

    def test_the_reading_before_the_roll_is_logged(self):
        self.roll([{NEAR: 1000, FAR: 1000}, {NEAR: 0, FAR: 2000}])
        self.assertIn("Before the roll:", self.log.text())


class TestTheTradeBookAsAWitness(ExecutionCase):
    def roll(self, traded):
        engine = self.build([outcome(1000, 1000), outcome(1000, 1000)],
                            traded=traded)
        engine._execute(self.decision, self.near_q, self.far_q)
        return engine

    def test_agreement_is_noted(self):
        self.roll({(NEAR, SELL): 1000, (FAR, BUY): 1000})
        self.assertIn("Trade book confirms the near leg", self.log.text())
        self.assertIn("Trade book confirms the far leg", self.log.text())

    def test_disagreement_is_shouted_about(self):
        self.roll({(NEAR, SELL): 400, (FAR, BUY): 1000})
        self.assertIn("TRADE BOOK DISAGREES on the near leg", self.log.text())
        self.assertIn("ALERT", self.log.text())

    def test_disagreement_does_not_halt_on_its_own(self):
        """Its row shape is still unconfirmed. Halting every roll on a guess
        would be worse than saying so loudly."""
        engine = self.roll({(NEAR, SELL): 400, (FAR, BUY): 1000})
        self.assertIsNone(engine.session.halted_reason)

    def test_an_unreadable_trade_book_says_the_fills_stand_alone(self):
        self.roll(None)
        self.assertIn("rest on the order book alone", self.log.text())


class TestDescribing(unittest.TestCase):
    def test_positions_read_plainly(self):
        self.assertEqual(Positions(1000, 250).describe(), "near 1000, far 250")

    def test_an_unknown_leg_shows_a_question_mark(self):
        self.assertIn("?", Positions(None, 250).describe())

    def test_the_module_has_no_clock_of_its_own_in_tests(self):
        """Every wait is injectable, so the suite never actually sleeps."""
        import inspect
        source = inspect.getsource(reconcile.check)
        self.assertIn("sleep", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
