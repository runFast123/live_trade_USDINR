"""A Section at runtime, and the engine holding exactly one of them.

This is the checkpoint of the multi-section work: the engine splits into
account-level facts and per-roll facts before any new behaviour exists to
debug. Nothing here should change what the app does. If these pass and the
other eight hundred pass, the shape is right.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import date

from rollover.config import RollConfig
from rollover.sections import AccountState, Section

SEP_OCT = {"name": "Sep into Oct", "far_token": "1500",
           "far_expiry": "2026-10-29",
           "limit_ladder": [{"bps": "30", "qty": 10000}]}
SEP_NOV = {"name": "Sep into Nov", "far_token": "1584",
           "far_expiry": "2026-11-26",
           "limit_ladder": [{"bps": "50", "qty": 20000}]}


def config(**kw):
    base = dict(near_token="1769", near_expiry="2026-09-28",
                far_token="1584", far_expiry="2026-11-26",
                limit_ladder=[{"bps": "50", "qty": 20000}])
    base.update(kw)
    return RollConfig(**base)


def section(cfg=None, account=None, index=0):
    cfg = cfg or config()
    account = account or AccountState()
    return Section(cfg.section_specs()[index], account, cfg), account, cfg


class TestItReadsLikeTheOldSession(unittest.TestCase):
    """The gates duck-type this object. Every name they use must be here."""

    GATE_READS = ("logged_in", "market_open", "near", "far", "scrip_file_date",
                  "near_position_qty", "clips_done_today", "in_flight",
                  "halted_reason", "margin")

    def test_every_attribute_the_gates_read_exists(self):
        got, _, _ = section()
        for name in self.GATE_READS:
            getattr(got, name)          # raises if missing

    def test_the_gates_run_against_it_unchanged(self):
        import time

        from rollover import gates, rule
        from rollover.broker import InstrumentInfo
        from rollover.money import D
        from rollover.quotes import Quote

        got, account, cfg = section()
        account.logged_in = True
        account.market_open = True
        account.scrip_file_date = date.today()
        got.near_position_qty = 500000

        def contract(token, expiry):
            return InstrumentInfo(
                token=token, symbol="USDINR", sec_desc="USDINR" + token,
                segment="13", lot_size=1000, expiry=expiry, instrument="FUTCUR",
                price_divisor=D("10000000"), tick=D("0.0025"),
                tick_units=D("25000"), low_range=D("93"), high_range=D("99"))

        got.near = contract("1769", date(2026, 9, 28))
        got.far = contract("1584", date(2026, 11, 26))

        now = time.monotonic()
        nq = Quote("1769", D("95.9400"), D("95.9450"), now, D(1),
                   bid_qty=500000, ask_qty=500000)
        fq = Quote("1584", D("96.2370"), D("96.2375"), now, D(1),
                   bid_qty=500000, ask_qty=500000)
        decision = rule.compute(nq, fq, got.cfg, days=59)
        report = gates.evaluate(got.cfg, got, {"1769": nq, "1584": fq}, decision)
        self.assertTrue(report.gates, "no gates ran at all")


class TestWhatIsSharedAndWhatIsNot(unittest.TestCase):
    def setUp(self):
        self.cfg = config(sections=[SEP_OCT, SEP_NOV])
        self.account = AccountState()
        self.a = Section(self.cfg.section_specs()[0], self.account, self.cfg)
        self.b = Section(self.cfg.section_specs()[1], self.account, self.cfg)

    def test_an_order_in_flight_is_shared(self):
        """One order at a time across every section, always."""
        self.a.in_flight = True
        self.assertTrue(self.b.in_flight)

    def test_signing_in_is_shared(self):
        self.a.logged_in = True
        self.assertTrue(self.b.logged_in)

    def test_the_margin_is_shared(self):
        self.a.margin = "an estimate"
        self.assertEqual(self.b.margin, "an estimate")

    def test_the_contracts_are_not_shared(self):
        self.a.near = "a contract"
        self.assertIsNone(self.b.near)

    def test_the_clip_count_is_not_shared(self):
        self.a.clips_done_today = 3
        self.assertEqual(self.b.clips_done_today, 0)

    def test_ladder_progress_is_not_shared(self):
        self.a.ladder_progress = {"30": 4000}
        self.assertEqual(self.b.ladder_progress, {})


class TestHalting(unittest.TestCase):
    def setUp(self):
        self.cfg = config(sections=[SEP_OCT, SEP_NOV])
        self.account = AccountState()
        self.a = Section(self.cfg.section_specs()[0], self.account, self.cfg)
        self.b = Section(self.cfg.section_specs()[1], self.account, self.cfg)

    def test_a_section_halt_stops_only_that_section(self):
        self.a.own_halt = "this campaign is stuck"
        self.assertTrue(self.a.halted)
        self.assertFalse(self.b.halted)

    def test_an_account_halt_stops_every_section(self):
        """A half rolled position is a fact about the exposure, not one roll."""
        self.account.halted_reason = "HALF ROLLED, short 600"
        self.assertTrue(self.a.halted)
        self.assertTrue(self.b.halted)

    def test_the_account_halt_is_what_is_reported(self):
        self.a.own_halt = "narrower"
        self.account.halted_reason = "HALF ROLLED"
        self.assertEqual(self.a.halted_reason, "HALF ROLLED")

    def test_clearing_the_account_halt_leaves_a_section_halt(self):
        self.a.own_halt = "narrower"
        self.account.halted_reason = "HALF ROLLED"
        self.account.halted_reason = None
        self.assertEqual(self.a.halted_reason, "narrower")
        self.assertFalse(self.b.halted)

    def test_assigning_halted_reason_halts_this_section_alone(self):
        self.a.halted_reason = "mine"
        self.assertEqual(self.a.own_halt, "mine")
        self.assertFalse(self.b.halted)


class TestTheConfigIsDerived(unittest.TestCase):
    def setUp(self):
        self.cfg = config(dry_run=True, sections=[SEP_OCT, SEP_NOV])
        self.account = AccountState()
        self.a = Section(self.cfg.section_specs()[0], self.account, self.cfg)

    def test_it_carries_the_sections_own_settings(self):
        self.assertEqual(self.a.cfg.far_token, "1500")
        self.assertEqual(self.a.cfg.limit_ladder[0]["bps"], "30")

    def test_going_live_reaches_it(self):
        """A stored copy would report the margin gate as passing."""
        self.assertTrue(self.a.cfg.dry_run)
        self.cfg.dry_run = False
        self.assertFalse(self.a.cfg.dry_run)

    def test_a_section_edited_away_says_so(self):
        self.assertTrue(self.a.still_configured)
        self.cfg.sections = [SEP_NOV]
        self.assertFalse(self.a.still_configured)

    def test_and_still_answers_rather_than_raising(self):
        self.cfg.sections = [SEP_NOV]
        self.assertIsNotNone(self.a.cfg)


class TestWhatASectionClaims(unittest.TestCase):
    def test_a_laddered_section_claims_its_ladder(self):
        from rollover.ladder import parse
        got, _, _ = section()
        got.ladder = parse(got.cfg.limit_ladder, lot_size=1000)
        self.assertEqual(got.allocated(), 20000)
        self.assertEqual(got.claim().outstanding, 20000)

    def test_progress_reduces_the_claim(self):
        from rollover.ladder import parse
        got, _, _ = section()
        got.ladder = parse(got.cfg.limit_ladder, lot_size=1000)
        got.ladder_progress = {"50": 5000}
        self.assertEqual(got.claim().outstanding, 15000)

    def test_a_section_with_no_ladder_claims_everything(self):
        """None, not zero. It has no cap, so it could take all of it."""
        got, _, _ = section(config(limit_ladder=[]))
        self.assertIsNone(got.allocated())
        self.assertTrue(got.claim().uncapped)

    def test_the_claim_carries_both_contracts(self):
        got, _, _ = section()
        claim = got.claim()
        self.assertEqual(claim.near_token, "1769")
        self.assertEqual(claim.far_token, "1584")

    def test_a_halted_section_says_so_in_its_claim(self):
        got, _, _ = section()
        got.own_halt = "stuck"
        self.assertTrue(got.claim().halted)


class TestTheEngineHoldsExactlyOne(unittest.TestCase):
    """Nothing about today's behaviour may change at this step."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="checkpoint_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def engine(self, **kw):
        from rollover.engine import RollEngine

        class Log:
            def __init__(self): self.lines = []
            def info(self, m): self.lines.append(m)
            def warn(self, m): self.lines.append(m)
            def error(self, m): self.lines.append(m)
            def alert(self, m): self.lines.append(m)

        class Broker:
            logged_in = True
            scrip_file_date = date.today()

        cfg = config(use_live_feed=False, record_market=False,
                     update_check=False, journal=False, **kw)
        self.log = Log()
        return RollEngine(cfg, self.log, self.dir, broker=Broker())

    def test_one_section_by_default(self):
        self.assertEqual(len(self.engine().sections), 1)

    def test_session_is_that_section(self):
        engine = self.engine()
        self.assertIs(engine.session, engine.sections[0])

    def test_ladder_and_progress_read_through_to_it(self):
        engine = self.engine()
        engine.ladder_progress = {"50": 3000}
        self.assertEqual(engine.sections[0].ladder_progress, {"50": 3000})
        self.assertEqual(engine.ladder.total_qty, 20000)

    def test_state_round_trips_through_the_section(self):
        engine = self.engine()
        engine.session.clips_done_today = 2
        engine.ladder_progress = {"50": 3000}
        engine._persist()

        again = self.engine()
        self.assertEqual(again.session.clips_done_today, 2)
        self.assertEqual(again.ladder_progress, {"50": 3000})

    def test_halting_halts_the_account_not_one_campaign(self):
        engine = self.engine()
        engine.halt("HALF ROLLED, short 600")
        self.assertEqual(engine.account.halted_reason, "HALF ROLLED, short 600")
        self.assertTrue(engine.sections[0].halted)

    def test_an_account_halt_survives_a_restart(self):
        engine = self.engine()
        engine.halt("HALF ROLLED, short 600")
        self.assertIn("short 600", self.engine().halted_reason)

    def test_two_sections_are_refused_rather_than_half_run(self):
        """Running the first and ignoring the rest would look like it worked."""
        engine = self.engine(sections=[SEP_OCT, SEP_NOV])
        self.assertEqual(len(engine.sections), 2)
        self.assertIsNotNone(engine.halted_reason)
        self.assertIn("only trades one", engine.halted_reason)

    def test_each_section_still_builds_its_own_ladder(self):
        engine = self.engine(sections=[SEP_OCT, SEP_NOV])
        self.assertEqual([s.ladder.total_qty for s in engine.sections],
                         [10000, 20000])

    def test_each_section_claims_its_own_far_leg(self):
        engine = self.engine(sections=[SEP_OCT, SEP_NOV])
        claims = [s.claim() for s in engine.sections]
        self.assertEqual({c.near_token for c in claims}, {"1769"})
        self.assertEqual([c.far_token for c in claims], ["1500", "1584"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
