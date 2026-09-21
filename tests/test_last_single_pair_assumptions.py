"""The last three places that still assumed one pair, found by sweeping.

Every one of the section bugs reported this week was the same fault wearing a
different coat: something written for a single roll, left alone when
sections arrived, acting on sections[0] whatever was on screen. Rather than
wait for each to surface, ui.py and livemode.py were swept for every read of
engine.session, engine.ladder and engine.ladder_progress. These three were
real.

  * Reset ladder cleared sections[0]'s progress whichever section the
    operator was looking at -- forgetting rolled quantity on the wrong roll,
    which the next clip then rolls again.
  * The Go Live feed-health check looked at the first section's two legs
    only. A stale feed on a second section's far month would have passed
    preflight, which is exactly what lets a live clip be sized against a
    price nobody has seen for minutes.
  * The live dialog read the lot size off sections[0]'s near contract, and
    fell to None if that one section had failed to resolve.
"""
from __future__ import annotations

import inspect
import shutil
import tempfile
import unittest
from datetime import date

from rollover.config import RollConfig

SEP_OCT = {"name": "Sep into Oct", "far_token": "1500",
           "far_expiry": "2026-10-29",
           "limit_ladder": [{"bps": "30", "qty": 10000}]}
SEP_NOV = {"name": "Sep into Nov", "far_token": "1584",
           "far_expiry": "2026-11-26",
           "limit_ladder": [{"bps": "50", "qty": 20000}]}


class Log:
    def __init__(self):
        self.lines = []

    def info(self, m): self.lines.append(m)
    warn = error = alert = info

    def text(self):
        return " ".join(self.lines)


class Broker:
    logged_in = True
    scrip_file_date = date.today()


def engine(directory, sections=(SEP_OCT, SEP_NOV)):
    from rollover.engine import RollEngine

    cfg = RollConfig(near_token="1769", near_expiry="2026-09-28",
                     far_token="1584", far_expiry="2026-11-26",
                     limit_ladder=[{"bps": "50", "qty": 20000}],
                     sections=list(sections), use_live_feed=False,
                     record_market=False, update_check=False, journal=False,
                     dry_run=True)
    return RollEngine(cfg, Log(), directory, broker=Broker())


class TestResetLadderResetsTheSectionOnScreen(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="reset_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.engine = engine(self.dir)
        self.first, self.second = self.engine.sections
        self.first.ladder_progress = {"30": 4000}
        self.second.ladder_progress = {"50": 6000}

    def test_resetting_the_second_leaves_the_first_alone(self):
        self.engine.reset_ladder(self.second)
        self.assertEqual(self.second.ladder_progress, {})
        self.assertEqual(self.first.ladder_progress, {"30": 4000})

    def test_resetting_the_first_leaves_the_second_alone(self):
        self.engine.reset_ladder(self.first)
        self.assertEqual(self.first.ladder_progress, {})
        self.assertEqual(self.second.ladder_progress, {"50": 6000})

    def test_no_section_given_still_means_the_first(self):
        """The single-roll callers never pass one."""
        self.engine.reset_ladder()
        self.assertEqual(self.first.ladder_progress, {})
        self.assertEqual(self.second.ladder_progress, {"50": 6000})

    def test_it_says_which_section_it_reset(self):
        self.engine.reset_ladder(self.second)
        self.assertIn("Sep into Nov", self.engine.log.text())

    def test_the_reset_reaches_the_state_file(self):
        """A reset that did not persist would come back on restart."""
        self.engine.reset_ladder(self.second)
        again = engine(self.dir)
        by_name = {s.name: s for s in again.sections}
        self.assertEqual(by_name["Sep into Nov"].ladder_progress, {})
        self.assertEqual(by_name["Sep into Oct"].ladder_progress, {"30": 4000})

    def test_the_window_passes_the_focused_section(self):
        from rollover.ui import RollWindow
        source = inspect.getsource(RollWindow._reset_ladder)
        self.assertIn("self._focused_section()", source)
        self.assertIn("self.engine.reset_ladder(section)", source)
        self.assertNotIn("self.engine.ladder_progress", source)


class TestFeedHealthCoversEverySection(unittest.TestCase):
    """The Go Live preflight asks whether the feed is healthy. It has to ask
    about every leg that could be traded, not the first section's two."""

    class Feed:
        def __init__(self):
            self.asked = None

        def healthy(self, tokens):
            self.asked = list(tokens)
            return True

    def test_it_asks_about_every_sections_legs(self):
        from rollover import livemode

        d = tempfile.mkdtemp(prefix="feed_")
        self.addCleanup(shutil.rmtree, d, True)
        e = engine(d)
        from rollover.broker import InstrumentInfo
        from rollover.money import D

        def contract(token, expiry):
            return InstrumentInfo(token=token, symbol="USDINR",
                                  sec_desc=token, segment="13",
                                  lot_size=1000, expiry=expiry,
                                  instrument="FUTCUR",
                                  price_divisor=D("10000000"),
                                  tick=D("0.0025"), tick_units=D("25000"),
                                  low_range=D("93"), high_range=D("99"))
        for s in e.sections:
            s.near = contract(s.cfg.near_token, date(2026, 9, 28))
            s.far = contract(s.cfg.far_token, date(2026, 11, 26))
        e.feed = self.Feed()
        livemode._feed_is_healthy(e)
        # Two sections sharing September: three tokens, not two.
        self.assertEqual(sorted(e.feed.asked), ["1500", "1584", "1769"])

    def test_an_engine_without_quote_tokens_still_works(self):
        """Older engine shapes, and the fakes the livemode tests use."""
        from rollover import livemode

        class Leg:
            def __init__(self, t): self.token = t

        class Session:
            near, far = Leg("1769"), Leg("1584")

        class Old:
            session = Session()
            feed = self.Feed()
        e = Old()
        self.assertTrue(livemode._feed_is_healthy(e))
        self.assertEqual(e.feed.asked, ["1769", "1584"])


class TestTheLiveDialogFindsALotSize(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lot_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def dialog_lot(self, e):
        from rollover.ui import LiveModeDialog
        # Only the method under test; no window is built.
        return LiveModeDialog._lot_size(type("D", (), {"engine": e})())

    def contract(self, token, lot):
        from rollover.broker import InstrumentInfo
        from rollover.money import D
        return InstrumentInfo(token=token, symbol="USDINR", sec_desc=token,
                              segment="13", lot_size=lot,
                              expiry=date(2026, 9, 28), instrument="FUTCUR",
                              price_divisor=D("10000000"), tick=D("0.0025"),
                              tick_units=D("25000"), low_range=D("93"),
                              high_range=D("99"))

    def test_it_reads_the_first_resolved_contract(self):
        e = engine(self.dir)
        e.sections[0].near = self.contract("1769", 1000)
        self.assertEqual(self.dialog_lot(e), 1000)

    def test_a_first_section_that_failed_to_resolve_is_skipped(self):
        """It used to return None here, and the dialog then could not show
        what going live would commit."""
        e = engine(self.dir)
        e.sections[0].near = None
        e.sections[1].near = self.contract("1769", 1000)
        self.assertEqual(self.dialog_lot(e), 1000)

    def test_nothing_resolved_is_none_not_a_crash(self):
        e = engine(self.dir)
        for s in e.sections:
            s.near = None
        self.assertIsNone(self.dialog_lot(e))


if __name__ == "__main__":
    unittest.main(verbosity=2)
