"""Every section's legs get quoted, and no section borrows another's numbers.

Reported from a running build: a second section showed a dash in every
column, and the ROLL COST card below it showed the FIRST section's cost,
limit and tenor under the second section's name. The operator could not
change the limit, because the tenor being edited belonged to the other roll.

Two faults, one report.

  * the watch loop asked for [session.near.token, session.far.token] -- the
    first section's two legs -- worked out ONCE before the loop started. A
    second section on a different far month was never quoted, and adding one
    while the app ran changed nothing. The websocket subscribed to the same
    two.
  * _draw_cost fell back to the snapshot's own decision, which is the first
    section's, whenever the section on screen had none.

Every previous test of several sections stubbed _read_quotes with a dict
holding all the tokens, so the first fault could not show. These tests ask
the engine what it wants quoted instead of telling it.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import date

from rollover.broker import InstrumentInfo
from rollover.config import RollConfig
from rollover.money import D

CONTRACTS = {"1769": ("USDINR26SEPFUT", date(2026, 9, 28)),
             "1284": ("USDINR26OCTFUT", date(2026, 10, 28)),
             "1584": ("USDINR26NOVFUT", date(2026, 11, 26))}

NOV = {"name": "Sep into Nov", "far_token": "1584",
       "far_expiry": "2026-11-26",
       "limit_ladder": [{"bps": "50", "qty": 10000}]}
OCT = {"name": "Sep into Oct", "far_token": "1284",
       "far_expiry": "2026-10-28",
       "limit_ladder": [{"bps": "30", "qty": 10000}]}


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


def contract(token):
    desc, expiry = CONTRACTS[token]
    return InstrumentInfo(token=token, symbol="USDINR", sec_desc=desc,
                          segment="13", lot_size=1000, expiry=expiry,
                          instrument="FUTCUR", price_divisor=D("10000000"),
                          tick=D("0.0025"), tick_units=D("25000"),
                          low_range=D("93"), high_range=D("99"))


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="quoted_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.log = Log()

    def engine(self, sections, **kw):
        from rollover.engine import RollEngine

        cfg = RollConfig(
            near_token="1769", near_expiry="2026-09-28",
            far_token="1584", far_expiry="2026-11-26",
            limit_bps_schedule={"1": "30", "2": "50"},
            limit_ladder=[{"bps": "50", "qty": 10000}],
            sections=sections, use_live_feed=False, record_market=False,
            update_check=False, journal=False, dry_run=True, **kw)
        engine = RollEngine(cfg, self.log, self.dir, broker=Broker())
        for section in engine.sections:
            section.near = contract(section.cfg.near_token)
            section.far = contract(section.cfg.far_token)
        return engine


class TestWhichTokensAreAskedFor(EngineCase):
    def test_one_section_asks_for_its_two_legs(self):
        self.assertEqual(self.engine([]).quote_tokens(), ["1769", "1584"])

    def test_two_sections_ask_for_all_three(self):
        """The far month of the second section is the one that was missing."""
        self.assertEqual(self.engine([NOV, OCT]).quote_tokens(),
                         ["1769", "1584", "1284"])

    def test_a_shared_leg_is_asked_for_once(self):
        got = self.engine([NOV, OCT]).quote_tokens()
        self.assertEqual(got.count("1769"), 1)

    def test_a_disabled_section_is_still_quoted(self):
        """It is priced so it can be compared, which needs a quote."""
        got = self.engine([dict(NOV, enabled=False), OCT]).quote_tokens()
        self.assertIn("1584", got)

    def test_a_section_whose_contracts_are_not_resolved_is_skipped(self):
        engine = self.engine([NOV, OCT])
        engine.sections[1].far = None
        self.assertEqual(engine.quote_tokens(), ["1769", "1584"])

    def test_adding_a_section_changes_the_answer(self):
        """It used to be worked out once, before the watch loop started."""
        engine = self.engine([NOV])
        before = engine.quote_tokens()
        engine.cfg.sections = [NOV, OCT]
        engine.reload_sections()
        for section in engine.sections:
            section.near = contract(section.cfg.near_token)
            section.far = contract(section.cfg.far_token)
        self.assertNotIn("1284", before)
        self.assertIn("1284", engine.quote_tokens())


class TestTheWatchLoopAsksEveryTick(EngineCase):
    def test_the_tick_is_given_every_section_s_tokens(self):
        import inspect

        from rollover.engine import RollEngine
        source = inspect.getsource(RollEngine._run)
        self.assertIn("self._tick(self.quote_tokens())", source)
        self.assertNotIn("self.session.near.token", source)

    def test_the_feed_is_started_on_all_of_them(self):
        import inspect

        from rollover.engine import RollEngine
        source = inspect.getsource(RollEngine._connect)
        self.assertIn("self.feed.start(self.broker.client, self.quote_tokens())",
                      source)


class TestTheFeedFollowsANewSection(unittest.TestCase):
    def feed(self):
        from rollover.feed import LiveFeed

        got = LiveFeed(RollConfig(vendor_id="V", api_key="K"), Log())
        got.tokens = ["1769", "1584"]
        return got

    def test_a_new_token_is_added(self):
        feed = self.feed()
        self.assertTrue(feed.watch(["1769", "1584", "1284"]))
        self.assertEqual(feed.tokens, ["1769", "1584", "1284"])

    def test_nothing_new_is_not_a_change(self):
        feed = self.feed()
        self.assertFalse(feed.watch(["1769", "1584"]))

    def test_it_says_which_ones_it_added(self):
        feed = self.feed()
        feed.watch(["1284"])
        self.assertIn("1284", feed.log.text())

    def test_an_empty_token_is_ignored(self):
        feed = self.feed()
        feed.watch(["", None, "1284"])
        self.assertEqual(feed.tokens, ["1769", "1584", "1284"])

    def test_it_does_not_subscribe_when_the_socket_is_not_up(self):
        """There is nothing to subscribe on; start() will use the list."""
        feed = self.feed()
        feed._started = False
        feed.watch(["1284"])           # must not raise
        self.assertIn("1284", feed.tokens)


class TestNoSectionBorrowsAnothersNumbers(unittest.TestCase):
    """The ROLL COST card, when the section on screen has no quote yet."""

    def test_the_fallback_to_the_snapshot_is_gone(self):
        import inspect

        from rollover.ui import RollWindow
        source = inspect.getsource(RollWindow._draw_cost)
        self.assertNotIn("else snap.decision", source)

    def test_the_tenor_comes_from_the_focused_section(self):
        """Editing a limit against the wrong tenor writes the wrong entry in
        the schedule, changing a roll the operator is not looking at."""
        import inspect

        from rollover.ui import RollWindow
        source = inspect.getsource(RollWindow._current_tenor)
        self.assertIn("self._focused()", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
