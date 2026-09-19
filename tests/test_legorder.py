"""Sending the far leg first, and what it changes when it goes wrong.

Near first is right when both legs fill trivially: if the second fails the
account is left flat, which can be corrected at leisure. It inverts when the
far book is thin, because then the second leg is the one likely to come up
short and a half roll becomes the expected outcome rather than the exception.

The prize is in the partial fill. Far first, a far book that offers 8,000
against a clip of 25,000 produces a clean 8,000 roll -- the thin leg sets the
size after the fact. Near first, the same market produces a 25,000 sale against
an 8,000 purchase, which is a 17,000 naked short.

The price is that the failure mode inverts too, and the two are not undone the
same way.
"""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from datetime import date

from rollover import rule
from rollover.broker import BUY, InstrumentInfo, OrderOutcome
from rollover.config import RollConfig
from rollover.money import D
from rollover.quotes import Quote

DEEP, THIN = 400_000, 8_000


def contract(token, expiry):
    return InstrumentInfo(token=token, symbol="USDINR", sec_desc="USDINR" + token,
                          segment="13", lot_size=1000, expiry=expiry,
                          instrument="FUTCUR", price_divisor=D("10000000"),
                          tick=D("0.0025"), tick_units=D("25000"),
                          low_range=D("93"), high_range=D("99"))


class StubLog:
    def __init__(self):
        self.lines = []

    def info(self, m): self.lines.append(("INFO", m))
    def warn(self, m): self.lines.append(("WARN", m))
    def error(self, m): self.lines.append(("ERR", m))
    def alert(self, m): self.lines.append(("ALERT", m))

    def text(self):
        return "\n".join(f"{a} {b}" for a, b in self.lines)


class StubBroker:
    logged_in = True
    scrip_file_date = date.today()

    def __init__(self, fills):
        self.fills = list(fills)
        self.sent = []

    def place_leg(self, info, side, qty, price, label):
        self.sent.append({"label": label, "token": info.token, "qty": qty,
                          "side": "BUY" if side == BUY else "SELL"})
        wanted = self.fills.pop(0) if self.fills else None
        filled = qty if wanted is None else wanted
        return OrderOutcome(sent=True, filled_qty=filled, requested_qty=qty,
                            order_ref="X", certain=True,
                            detail=f"filled {filled} of {qty}")

    def net_qty(self, token): return None
    def _trade_snapshot(self): return None
    def long_qty(self, token): return 500_000
    def market_open(self): return True
    def refresh_scrip_master_if_stale(self): return False


class LegOrderCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="legorder_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def roll(self, far_depth, fills=(None, None), near_depth=DEEP, **kw):
        from rollover.engine import RollEngine

        kw.setdefault("lots", 25)
        kw.setdefault("dry_run", False)
        kw.setdefault("journal", False)
        cfg = RollConfig(near_token="1769", near_expiry="2026-09-28",
                         far_token="1584", far_expiry="2026-11-26",
                         use_live_feed=False, record_market=False,
                         update_check=False,
                         require_margin=False, limit_mode="absolute", **kw)
        self.log = StubLog()
        self.broker = StubBroker(fills)
        engine = RollEngine(cfg, self.log, self.dir, broker=self.broker)
        section = engine.sections[0]
        section.near = contract("1769", date(2026, 9, 28))
        section.far = contract("1584", date(2026, 11, 26))

        now = time.monotonic()
        near_q = Quote("1769", D("95.9400"), D("95.9450"), now, D(1),
                       bid_qty=near_depth, ask_qty=near_depth)
        far_q = Quote("1584", D("96.2370"), D("96.2375"), now, D(1),
                      bid_qty=far_depth, ask_qty=far_depth)
        decision = rule.compute(near_q, far_q, cfg)
        sequence = engine._choose_sequence(decision, near_q, far_q, section)
        engine._execute(decision, near_q, far_q, section=section,
                        sequence=sequence)
        self.engine = engine
        self.sequence = sequence
        return engine

    def order(self):
        return [(s["side"], s["token"], s["qty"]) for s in self.broker.sent]


class TestWhichLegGoesOut(LegOrderCase):
    def test_a_deep_far_book_sends_the_near_leg_first(self):
        self.roll(DEEP)
        self.assertTrue(self.sequence.near_first)
        self.assertEqual(self.order()[0][:2], ("SELL", "1769"))

    def test_a_thin_far_book_sends_the_far_leg_first(self):
        self.roll(THIN)
        self.assertTrue(self.sequence.far_first)
        self.assertEqual(self.order()[0][:2], ("BUY", "1584"))

    def test_a_shallow_near_book_keeps_the_near_leg_first(self):
        """Far first commits you to selling near afterwards."""
        self.roll(THIN, near_depth=25_000)
        self.assertTrue(self.sequence.near_first)

    def test_the_configured_mode_is_obeyed(self):
        self.roll(THIN, leg_order="near_first")
        self.assertTrue(self.sequence.near_first)
        self.assertEqual(self.order()[0][:2], ("SELL", "1769"))

    def test_the_order_is_logged(self):
        self.roll(THIN)
        self.assertIn("Order of legs: far leg first", self.log.text())


class TestTheSecondLegIsSizedFromTheFirstFill(LegOrderCase):
    """The prize. A partial first leg makes a smaller clean roll, not a gap."""

    def test_far_first_with_a_partial_fill_sizes_the_near_leg_down(self):
        self.roll(THIN, fills=(8000, None))
        self.assertEqual(self.order(), [("BUY", "1584", 25000),
                                        ("SELL", "1769", 8000)])

    def test_and_that_is_a_complete_roll_not_a_half_one(self):
        engine = self.roll(THIN, fills=(8000, None))
        self.assertIsNone(engine.halted_reason)
        self.assertIn("ROLL COMPLETE", self.log.text())
        self.assertIn("sold 8000 near, bought 8000 far", self.log.text())

    def test_near_first_in_the_same_market_leaves_a_naked_short(self):
        """The comparison that justifies far-first, as an outcome."""
        engine = self.roll(THIN, fills=(25000, 8000), leg_order="near_first")
        self.assertIsNotNone(engine.halted_reason)
        self.assertIn("short 17000 units", engine.halted_reason)

    def test_near_first_also_sizes_the_far_leg_from_the_fill(self):
        self.roll(DEEP, fills=(9000, None))
        self.assertEqual(self.order(), [("SELL", "1769", 25000),
                                        ("BUY", "1584", 9000)])


class TestTheFailureModeInverts(LegOrderCase):
    """Near first leaves you SHORT. Far first leaves you LONG BOTH."""

    def test_near_first_failure_is_a_short(self):
        engine = self.roll(DEEP, fills=(25000, 9000))
        self.assertIn("HALF ROLLED", engine.halted_reason)
        self.assertIn("short 16000", engine.halted_reason)
        self.assertNotIn("long both", engine.halted_reason.lower())

    def test_far_first_failure_is_long_both_months(self):
        engine = self.roll(THIN, fills=(8000, 3000))
        self.assertIn("HALF ROLLED THE OTHER WAY", engine.halted_reason)
        self.assertIn("long both months by 5000", engine.halted_reason)

    def test_the_far_first_message_names_both_quantities(self):
        engine = self.roll(THIN, fills=(8000, 3000))
        self.assertIn("bought 8000", engine.halted_reason)
        self.assertIn("only sold 3000", engine.halted_reason)

    def test_neither_credits_the_ladder(self):
        for depth, fills in ((DEEP, (25000, 9000)), (THIN, (8000, 3000))):
            engine = self.roll(depth, fills=fills,
                               limit_ladder=[{"bps": "50", "qty": 100000}])
            self.assertEqual(engine.sections[0].ladder_progress, {})


class TestUnwindingIsNotSymmetrical(LegOrderCase):
    """Undoing far-first means selling into the book that was too thin.

    Sweeping a thin book in a hurry is how a small legging cost becomes a
    large one, so it is never done automatically whatever auto unwind says.
    """

    def test_near_first_unwinds_by_buying_the_near_leg_back(self):
        engine = self.roll(DEEP, fills=(25000, 9000, None),
                           auto_unwind_on_leg2_failure=True)
        self.assertEqual(self.order()[-1][:2], ("BUY", "1769"))
        self.assertIn("bought back", engine.halted_reason)

    def test_far_first_refuses_to_unwind_even_when_asked_to(self):
        engine = self.roll(THIN, fills=(8000, 3000, None),
                           auto_unwind_on_leg2_failure=True)
        self.assertEqual(len(self.broker.sent), 2, "it tried to unwind")
        self.assertIn("not done automatically", engine.halted_reason)
        self.assertIn("thin book", engine.halted_reason)

    def test_with_auto_unwind_off_both_simply_halt(self):
        for depth, fills in ((DEEP, (25000, 9000)), (THIN, (8000, 3000))):
            engine = self.roll(depth, fills=fills)
            self.assertEqual(len(self.broker.sent), 2)
            self.assertIn("needs you at the terminal", engine.halted_reason)


class TestNothingElseChanged(LegOrderCase):
    def test_a_first_leg_that_does_not_fill_sends_no_second(self):
        engine = self.roll(THIN, fills=(0,))
        self.assertEqual(len(self.broker.sent), 1)
        self.assertIsNone(engine.halted_reason)
        self.assertIn("did not fill", self.log.text())

    def test_dry_run_never_reaches_the_second_leg(self):
        """The real broker refuses to send; the engine stops before leg two.

        place_leg is still called for the first leg, because that is where the
        dry-run guard lives -- deliberately, so any future call site is safe by
        default rather than relying on its caller to remember.
        """
        for depth in (DEEP, THIN):
            self.roll(depth, dry_run=True)
            self.assertEqual(len(self.broker.sent), 1)
            self.assertIn("leg 1", self.broker.sent[0]["label"])

    def test_a_clip_is_counted_once(self):
        engine = self.roll(THIN)
        self.assertEqual(engine.sections[0].clips_done_today, 1)

    def test_the_sequence_is_journalled(self):
        """The choice has to be readable afterwards, not inferred."""
        from rollover.journal import read
        engine = self.roll(THIN, journal=True)
        kinds = [e["kind"] for e in read(engine.journal.path_for())]
        self.assertIn("sequence", kinds)


if __name__ == "__main__":
    unittest.main(verbosity=2)
