"""A switched-off section is priced and compared, and never trades.

A section is added to find out what a roll would cost. One that was switched
off showed nothing at all -- every column a dash -- so the only way to see a
number was to Enable it, which is the one action that lets it sell. The
operator had to arm the thing to find out whether they wanted it.

So it is priced like any other. The safety is that it never becomes a
candidate, which is a different thing from never being priced, and this file
exists to hold those two apart: every test below either proves it shows a
number, or proves it cannot trade.
"""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from datetime import date

from rollover.broker import InstrumentInfo
from rollover.config import RollConfig
from rollover.money import D
from rollover.quotes import Quote

SEP_OCT = {"name": "Sep into Oct", "far_token": "1500",
           "far_expiry": "2026-10-29",
           "limit_ladder": [{"bps": "30", "qty": 10000}]}
SEP_NOV = {"name": "Sep into Nov", "far_token": "1584",
           "far_expiry": "2026-11-26",
           "limit_ladder": [{"bps": "50", "qty": 20000}]}

CONTRACTS = {"1769": ("USDINR26SEPFUT", date(2026, 9, 28)),
             "1500": ("USDINR26OCTFUT", date(2026, 10, 29)),
             "1584": ("USDINR26NOVFUT", date(2026, 11, 26))}
# Deliberately cheap, so a disabled section would qualify if anything let it.
PRICES = {"1769": ("95.7800", "95.7825"),
          "1500": ("95.7900", "95.7925"),
          "1584": ("95.8000", "95.8025")}


class Log:
    def __init__(self): self.lines = []
    def info(self, m): self.lines.append(m)
    def warn(self, m): self.lines.append(m)
    def error(self, m): self.lines.append(m)
    def alert(self, m): self.lines.append(m)


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


class DisabledCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="disabled_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.log = Log()
        self.sent = []

    def engine(self, sections):
        from rollover.engine import RollEngine

        cfg = RollConfig(
            near_token="1769", near_expiry="2026-09-28",
            far_token="1584", far_expiry="2026-11-26",
            limit_ladder=[{"bps": "50", "qty": 20000}],
            limit_bps_schedule={"1": "50", "2": "50"},
            sections=sections, use_live_feed=False, record_market=False,
            update_check=False, journal=False, dry_run=True)
        engine = RollEngine(cfg, self.log, self.dir, broker=Broker())
        for s in engine.sections:
            s.near = contract(s.cfg.near_token)
            s.far = contract(s.cfg.far_token)
        engine.near_positions = {"1769": 100000}
        engine.account.logged_in = True
        engine.account.market_open = True
        engine.account.scrip_file_date = date.today()

        # Record what would have been sent instead of sending it.
        engine._execute = lambda *a, **kw: self.sent.append(
            kw.get("section") or (a[3] if len(a) > 3 else None))
        self.engine = engine
        return engine

    def quotes(self):
        now = time.monotonic()
        return {t: Quote(t, D(b), D(a), now, D(1), bid_qty=400000,
                         ask_qty=400000)
                for t, (b, a) in PRICES.items()}

    def tick(self):
        self.engine._read_quotes = lambda tokens: self.quotes()
        self.engine._slow_refresh = lambda: None
        self.engine._tick(list(PRICES))

    def by_name(self, name):
        for s in self.engine.sections:
            if s.name == name:
                return s
        raise AssertionError(f"no section called {name}")


class TestADisabledSectionIsStillPriced(DisabledCase):
    def setUp(self):
        super().setUp()
        self.engine([SEP_OCT, dict(SEP_NOV, enabled=False)])
        self.tick()
        self.off = self.by_name("Sep into Nov")

    def test_it_has_a_decision(self):
        self.assertIsNotNone(self.off.decision)

    def test_with_a_roll_cost(self):
        self.assertIsNotNone(self.off.decision.cost_bps)

    def test_and_the_limit_it_would_be_judged_against(self):
        self.assertIsNotNone(self.off.decision.limit_bps)

    def test_so_it_can_be_compared_with_the_enabled_one(self):
        on = self.by_name("Sep into Oct")
        self.assertNotEqual(on.decision.cost_bps, self.off.decision.cost_bps)

    def test_it_is_marked_as_disabled(self):
        self.assertEqual(self.off.note, "disabled")

    def test_the_gates_are_not_run_on_it(self):
        """They answer "may this trade", and it may not. Running them would
        fill the row with reasons that are beside the point."""
        self.assertIsNone(self.off.report)


class TestButItCannotTrade(DisabledCase):
    """The prices above are chosen so a disabled section WOULD qualify."""

    def setUp(self):
        super().setUp()
        self.engine([SEP_OCT, dict(SEP_NOV, enabled=False)])
        self.engine.arm()
        self.tick()

    def test_it_qualifies_on_price(self):
        """Otherwise the tests below pass for the wrong reason."""
        self.assertTrue(self.by_name("Sep into Nov").decision.qualifies)

    def test_and_still_nothing_was_sent_for_it(self):
        self.assertNotIn(self.by_name("Sep into Nov"), self.sent)

    def test_it_never_reaches_the_candidates(self):
        import rollover.book as book

        seen = []
        real = book.pick
        book.pick = lambda candidates, expiry_day=False: (
            seen.extend(c.name for c in candidates),
            real(candidates, expiry_day=expiry_day))[1]
        try:
            self.tick()
        finally:
            book.pick = real
        self.assertNotIn(self.by_name("Sep into Nov").label(), seen)

    def test_it_claims_none_of_the_position(self):
        self.assertIsNone(self.by_name("Sep into Nov").near_position_qty)


class TestEveryoneDisabledTradesNothing(DisabledCase):
    def setUp(self):
        super().setUp()
        self.engine([dict(SEP_OCT, enabled=False),
                     dict(SEP_NOV, enabled=False)])
        self.engine.arm()
        self.tick()

    def test_both_are_still_priced(self):
        for name in ("Sep into Oct", "Sep into Nov"):
            self.assertIsNotNone(self.by_name(name).decision)

    def test_and_nothing_is_sent(self):
        self.assertEqual(self.sent, [])


class TestSwitchingOneOnChangesOnlyWhatItMayDo(DisabledCase):
    def setUp(self):
        super().setUp()
        self.engine([SEP_OCT, dict(SEP_NOV, enabled=False)])
        self.tick()
        self.before = self.by_name("Sep into Nov").decision.cost_bps

    def test_the_price_it_showed_was_the_real_one(self):
        """What it showed while off has to be what it works on once on, or
        the comparison it was added for was worthless."""
        self.engine.cfg.sections = [SEP_OCT, SEP_NOV]
        self.engine.reload_sections()
        for s in self.engine.sections:
            s.near = contract(s.cfg.near_token)
            s.far = contract(s.cfg.far_token)
        self.tick()
        self.assertEqual(self.by_name("Sep into Nov").decision.cost_bps,
                         self.before)

    def test_and_now_it_is_gated(self):
        self.engine.cfg.sections = [SEP_OCT, SEP_NOV]
        self.engine.reload_sections()
        for s in self.engine.sections:
            s.near = contract(s.cfg.near_token)
            s.far = contract(s.cfg.far_token)
        self.tick()
        self.assertIsNotNone(self.by_name("Sep into Nov").report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
