"""Tests for the switch that lets real orders out.

The asymmetry is the design: going live is slow and conditional, going back to
dry run is instant and unconditional. A safety control that is awkward to
release gets switched off and left off.
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from rollover import livemode
from rollover.config import RollConfig
from rollover.livemode import (CONFIRM, Check, blockers, exposure, go_dry,
                               go_live, may_go_live, preflight, rupees,
                               summary)
from rollover.money import D


class FakeBroker:
    def __init__(self, logged_in=True, scrip=None):
        self.logged_in = logged_in
        self.scrip_file_date = date.today() if scrip is None else scrip


class FakeSession:
    def __init__(self, halted=None):
        self.halted_reason = halted
        self.near = None
        self.far = None


class FakeFeed:
    def __init__(self, ok=True):
        self.ok = ok

    def healthy(self, tokens):
        return self.ok


class FakeEngine:
    def __init__(self, logged_in=True, halted=None, scrip=None, feed=True):
        self.broker = FakeBroker(logged_in, scrip)
        self.session = FakeSession(halted)
        self.feed = FakeFeed(feed) if feed is not None else None
        self.disarmed = []

    def disarm(self, reason="operator"):
        self.disarmed.append(reason)


def good_config(**kw):
    kw.setdefault("near_token", "1769")
    kw.setdefault("far_token", "1584")
    kw.setdefault("quantity_unit_confirmed", True)
    kw.setdefault("dry_run", True)
    return RollConfig(**kw)


def passing(cfg=None, engine=None):
    cfg = cfg or good_config()
    engine = engine or FakeEngine()
    return cfg, engine, preflight(cfg, engine, feed_healthy=True)


class TestTheQuantityGate(unittest.TestCase):
    """The unresolved 1000x question, made into a precondition."""

    def test_an_unconfirmed_quantity_unit_blocks_live(self):
        cfg = good_config(quantity_unit_confirmed=False)
        checks = preflight(cfg, FakeEngine(), feed_healthy=True)
        self.assertFalse(may_go_live(checks))
        self.assertIn("quantity unit confirmed",
                      [c.name for c in blockers(checks)])

    def test_confirming_it_clears_that_blocker(self):
        _, _, checks = passing()
        self.assertTrue(may_go_live(checks), blockers(checks))

    def test_the_default_config_is_not_confirmed(self):
        self.assertFalse(RollConfig().quantity_unit_confirmed)

    def test_the_blocker_says_what_to_do_about_it(self):
        cfg = good_config(quantity_unit_confirmed=False)
        checks = preflight(cfg, FakeEngine(), feed_healthy=True)
        detail = next(c.detail for c in checks if c.name == "quantity unit confirmed")
        self.assertIn("Choice", detail)
        self.assertIn("quantity_unit_confirmed", detail)


class TestTheOtherPreconditions(unittest.TestCase):
    def test_not_signed_in_blocks(self):
        checks = preflight(good_config(), FakeEngine(logged_in=False),
                           feed_healthy=True)
        self.assertIn("signed in", [c.name for c in blockers(checks)])

    def test_a_halt_blocks(self):
        checks = preflight(good_config(),
                           FakeEngine(halted="HALF ROLLED, short 600"),
                           feed_healthy=True)
        stopped = blockers(checks)
        self.assertIn("not halted", [c.name for c in stopped])
        self.assertIn("short 600",
                      next(c.detail for c in stopped if c.name == "not halted"))

    def test_a_stale_scrip_master_blocks(self):
        yesterday = date.today() - timedelta(days=1)
        checks = preflight(good_config(), FakeEngine(scrip=yesterday),
                           feed_healthy=True)
        self.assertIn("scrip master is today's", [c.name for c in blockers(checks)])

    def test_an_unhealthy_feed_blocks(self):
        """REST has been seen 13 minutes stale. That is not a basis to trade."""
        checks = preflight(good_config(), FakeEngine(), feed_healthy=False)
        self.assertIn("live price feed", [c.name for c in blockers(checks)])

    def test_no_engine_blocks(self):
        checks = preflight(good_config(), None)
        self.assertIn("engine running", [c.name for c in blockers(checks)])

    def test_an_invalid_configuration_blocks(self):
        cfg = good_config()
        cfg.near_token = cfg.far_token = "1769"     # same contract both legs
        checks = preflight(cfg, FakeEngine(), feed_healthy=True)
        self.assertIn("configuration valid", [c.name for c in blockers(checks)])

    def test_the_feed_is_read_from_the_engine_when_not_given(self):
        checks = preflight(good_config(), FakeEngine(feed=False))
        self.assertIn("live price feed", [c.name for c in blockers(checks)])


class TestGoingLive(unittest.TestCase):
    def test_the_exact_phrase_switches_it(self):
        cfg, engine, checks = passing()
        self.assertIsNone(go_live(cfg, engine, CONFIRM, checks))
        self.assertFalse(cfg.dry_run)

    def test_anything_else_changes_nothing(self):
        for answer in ("", "go live", "yes", "GO  LIVE", "GOLIVE", "Go Live"):
            cfg, engine, checks = passing()
            refusal = go_live(cfg, engine, answer, checks)
            self.assertIsNotNone(refusal, msg=answer)
            self.assertTrue(cfg.dry_run, msg=answer)

    def test_surrounding_whitespace_is_forgiven(self):
        cfg, engine, checks = passing()
        self.assertIsNone(go_live(cfg, engine, "  GO LIVE  ", checks))
        self.assertFalse(cfg.dry_run)

    def test_a_blocker_beats_the_confirmation(self):
        cfg = good_config(quantity_unit_confirmed=False)
        engine = FakeEngine()
        refusal = go_live(cfg, engine, CONFIRM,
                          preflight(cfg, engine, feed_healthy=True))
        self.assertIn("quantity unit confirmed", refusal)
        self.assertTrue(cfg.dry_run)

    def test_an_arm_made_in_dry_run_does_not_carry_into_live(self):
        """Whoever armed it was authorising a simulation, not an order."""
        cfg, engine, checks = passing()
        go_live(cfg, engine, CONFIRM, checks)
        self.assertEqual(engine.disarmed, ["switched to live"])

    def test_it_rechecks_when_not_given_checks(self):
        cfg = good_config(quantity_unit_confirmed=False)
        self.assertIsNotNone(go_live(cfg, FakeEngine(), CONFIRM))
        self.assertTrue(cfg.dry_run)


class TestGoingBack(unittest.TestCase):
    def test_dry_run_is_always_available(self):
        cfg, engine, checks = passing()
        go_live(cfg, engine, CONFIRM, checks)
        go_dry(cfg, engine)
        self.assertTrue(cfg.dry_run)

    def test_it_asks_nothing(self):
        cfg = good_config(dry_run=False)
        go_dry(cfg, FakeEngine())
        self.assertTrue(cfg.dry_run)

    def test_it_disarms_too(self):
        cfg, engine, _ = passing()
        go_dry(cfg, engine)
        self.assertEqual(engine.disarmed, ["switched to dry run"])

    def test_it_works_even_while_halted(self):
        cfg = good_config(dry_run=False)
        engine = FakeEngine(halted="something bad")
        go_dry(cfg, engine)
        self.assertTrue(cfg.dry_run)


class TestWhatItCommits(unittest.TestCase):
    def test_the_notional_is_lots_times_lot_size_times_price(self):
        got = exposure(good_config(lots=25), D("95.78"))
        self.assertEqual(got.qty_sent, 25000)
        self.assertEqual(got.as_units, D("2394500"))

    def test_the_other_reading_is_shown_while_the_unit_is_unconfirmed(self):
        cfg = good_config(lots=1, quantity_unit_confirmed=False)
        lines = exposure(cfg, D("95.78")).lines(unit_confirmed=False)
        self.assertTrue(any("CONTRACTS" in line for line in lines))
        self.assertTrue(any("crore" in line for line in lines))

    def test_it_is_not_shown_once_the_unit_is_confirmed(self):
        lines = exposure(good_config(lots=1), D("95.78")).lines(unit_confirmed=True)
        self.assertFalse(any("CONTRACTS" in line for line in lines))

    def test_a_missing_price_says_so_rather_than_showing_zero(self):
        lines = exposure(good_config(), None).lines(unit_confirmed=True)
        self.assertTrue(any("cannot be shown" in line for line in lines))
        self.assertFalse(any("Rs 0" in line for line in lines))

    def test_rupees_uses_lakh_and_crore(self):
        self.assertEqual(rupees(D("95780")), "Rs 95,780")
        self.assertEqual(rupees(D("2394500")), "Rs 23.95 lakh")
        self.assertEqual(rupees(D("95780000")), "Rs 9.58 crore")
        self.assertEqual(rupees(None), "unknown")

    def test_rupees_handles_a_negative(self):
        self.assertTrue(rupees(D("-95780")).startswith("-"))


class TestTheCampaignIsShownNotJustTheClip(unittest.TestCase):
    """Engaging live authorises the ladder, not one clip.

    A dialog showing only "about Rs 95,780" understates what is being agreed to
    by the number of clips in the ladder -- twenty six of them, in the case
    below.
    """

    def ladder_engine(self, progress=None):
        from rollover.ladder import parse

        class Engine:
            def __init__(self, ladder, progress):
                self.ladder = ladder
                self.ladder_progress = progress
                self.broker = self.session = self.feed = None

        return Engine(parse([{"bps": "30", "qty": 10000},
                             {"bps": "50", "qty": 20000}], lot_size=1000),
                      progress or {})

    def lines(self, progress=None, price=D("95.78")):
        cfg = good_config(lots=1)
        got = exposure(cfg, price, engine=self.ladder_engine(progress))
        return got, got.lines(unit_confirmed=True)

    def test_the_outstanding_quantity_is_shown(self):
        _, lines = self.lines({"30": 4000})
        self.assertTrue(any("26,000 to roll" in line for line in lines), lines)

    def test_so_is_what_it_is_worth(self):
        _, lines = self.lines({"30": 4000})
        self.assertTrue(any("24.90 lakh" in line for line in lines), lines)

    def test_and_how_many_clips_that_is(self):
        _, lines = self.lines({"30": 4000})
        self.assertTrue(any("26 clip(s)" in line for line in lines), lines)

    def test_a_finished_ladder_adds_nothing(self):
        _, lines = self.lines({"30": 10000, "50": 20000})
        self.assertFalse(any("to roll" in line for line in lines))

    def test_no_ladder_adds_nothing(self):
        got = exposure(good_config(), D("95.78"))
        self.assertEqual(got.campaign_left, 0)
        self.assertFalse(any("to roll" in line
                             for line in got.lines(unit_confirmed=True)))

    def test_without_a_price_the_quantity_still_shows(self):
        _, lines = self.lines({"30": 4000}, price=None)
        self.assertTrue(any("26,000 to roll" in line for line in lines))
        self.assertFalse(any("Rs" in line for line in lines[2:]))

    def test_a_broken_engine_does_not_stop_the_dialog(self):
        class Awkward:
            @property
            def ladder(self):
                raise RuntimeError("no")

        got = exposure(good_config(), D("95.78"), engine=Awkward())
        self.assertEqual(got.campaign_left, 0)


class TestWhatTheOperatorReads(unittest.TestCase):
    def test_it_says_orders_are_real_and_the_session_stays_live(self):
        cfg, engine, checks = passing()
        text = summary(cfg, checks, exposure(cfg, D("95.78")))
        self.assertIn("real orders", text)
        self.assertIn("session", text)

    def test_every_check_is_listed_with_its_verdict(self):
        cfg, engine, checks = passing()
        text = summary(cfg, checks, exposure(cfg, D("95.78")))
        for check in checks:
            self.assertIn(check.name, text)
        self.assertIn("[OK]", text)

    def test_a_blocker_is_explained_not_merely_marked(self):
        cfg = good_config(quantity_unit_confirmed=False)
        engine = FakeEngine()
        checks = preflight(cfg, engine, feed_healthy=True)
        text = summary(cfg, checks, exposure(cfg, D("95.78")))
        self.assertIn("[NO]", text)
        self.assertIn("Choice", text)

    def test_no_line_is_too_wide_for_the_dialog(self):
        cfg = good_config(quantity_unit_confirmed=False)
        engine = FakeEngine(halted="x" * 300, logged_in=False)
        checks = preflight(cfg, engine, feed_healthy=False)
        text = summary(cfg, checks, exposure(cfg, D("95.78")))
        for line in text.splitlines():
            self.assertLessEqual(len(line), 78, msg=line)


class TestTheCheckObject(unittest.TestCase):
    def test_the_mark_is_readable(self):
        self.assertEqual(Check("x", True, "").mark, "OK")
        self.assertEqual(Check("x", False, "").mark, "NO")


class TestTheConfirmationPhrase(unittest.TestCase):
    def test_it_is_not_something_typed_by_accident(self):
        self.assertGreaterEqual(len(CONFIRM), 7)
        self.assertEqual(CONFIRM, CONFIRM.upper())

    def test_it_is_not_the_probe_phrase(self):
        """Two different irreversible actions must not share a phrase."""
        from rollover.probe import CONFIRM as PROBE_CONFIRM
        self.assertNotEqual(CONFIRM, PROBE_CONFIRM)


class TestItDoesNotImportTk(unittest.TestCase):
    def test_the_gate_is_testable_without_a_display(self):
        import inspect
        source = inspect.getsource(livemode)
        self.assertNotIn("import tkinter", source)
        self.assertNotIn("from tkinter", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
