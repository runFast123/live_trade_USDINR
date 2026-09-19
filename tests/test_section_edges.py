"""The places outside the trading loop that had to learn about sections.

The engine, the gates and the window were taught about several rolls. Four
things around the edges were not, and each of them is a way to be told
something untrue:

  * ``--check`` described one pair, so a second section was invisible in the
    one command whose job is to say what the app will do;
  * the recorder pooled every section into one evidence file, producing a
    "closest approach" belonging to no roll in particular;
  * the journal wrote a near-leg SELL with no way to say which roll asked for
    it -- and two sections selling the same September differ only in the far
    leg, which that line does not carry;
  * the Go Live dialog counted the first section's ladder only, understating
    the money it was asking permission to commit.

None of these can lose money by itself. All of them can make a person believe
the wrong thing about a system that does.
"""
from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date

import roll_app
from rollover import journal as journallib
from rollover import livemode
from rollover.config import RollConfig
from rollover.money import D
from rollover.recorder import Recorder

SEP_OCT = {"name": "Sep into Oct", "far_token": "1500",
           "far_expiry": "2026-10-29",
           "limit_ladder": [{"bps": "30", "qty": 10000}]}
SEP_NOV = {"name": "Sep into Nov", "far_token": "1584",
           "far_expiry": "2026-11-26",
           "limit_ladder": [{"bps": "50", "qty": 20000}]}


def config(**kw):
    base = dict(near_token="1769", near_expiry="2026-09-28",
                far_token="1584", far_expiry="2026-11-26",
                limit_ladder=[{"bps": "50", "qty": 20000}],
                quantity_unit_confirmed=True)
    base.update(kw)
    return RollConfig(**base)


# --------------------------------------------------------------- --check
class CheckCase(unittest.TestCase):
    def check(self, cfg):
        out = io.StringIO()
        with redirect_stdout(out):
            code = roll_app.cmd_check(cfg)
        return code, out.getvalue()


class TestOneSectionReadsAsItAlwaysDid(CheckCase):
    def test_it_is_not_called_a_section(self):
        _, text = self.check(config())
        self.assertNotIn("sections ", text)
        self.assertNotIn("[1]", text)

    def test_the_pair_and_the_ladder_are_still_there(self):
        _, text = self.check(config())
        self.assertIn("near token   1769", text)
        self.assertIn("far token    1584", text)
        self.assertIn("20,000 in total", text)

    def test_it_passes(self):
        code, _ = self.check(config())
        self.assertEqual(code, 0)


class TestEverySectionIsDescribed(CheckCase):
    def setUp(self):
        self.code, self.text = self.check(
            config(sections=[SEP_OCT, dict(SEP_NOV, lots=5)]))

    def test_both_appear(self):
        self.assertIn("Sep into Oct", self.text)
        self.assertIn("Sep into Nov", self.text)

    def test_with_their_own_far_legs(self):
        self.assertIn("far token    1500", self.text)
        self.assertIn("far token    1584", self.text)

    def test_and_their_own_ladders(self):
        self.assertIn("10,000 in total", self.text)
        self.assertIn("20,000 in total", self.text)

    def test_and_their_own_clip_sizes(self):
        """A section may size its own clip, and the report must say so."""
        self.assertIn("1 lot(s) = 1000 units", self.text)
        self.assertIn("5 lot(s) = 5000 units", self.text)

    def test_the_count_is_stated(self):
        self.assertIn("2 configured, 2 enabled", self.text)

    def test_it_passes(self):
        self.assertEqual(self.code, 0)


class TestWhatTheyShareIsPrintedOnce(CheckCase):
    def test_the_schedule_is_not_repeated_per_section(self):
        _, text = self.check(config(sections=[SEP_OCT, SEP_NOV]))
        self.assertEqual(text.count("limit mode"), 1)
        self.assertEqual(text.count("qty unit"), 1)


class TestASectionThatWillNotTradeSaysSo(CheckCase):
    def test_a_disabled_one_is_marked(self):
        _, text = self.check(config(sections=[SEP_OCT,
                                              dict(SEP_NOV, enabled=False)]))
        self.assertIn("DISABLED", text)
        self.assertIn("2 configured, 1 enabled", text)


class TestTheSharedPositionIsCalledOut(CheckCase):
    """Two sections selling one September are spending one position."""

    def test_the_total_claimed_is_added_up(self):
        _, text = self.check(config(sections=[SEP_OCT, SEP_NOV]))
        self.assertIn("2 sections sell token 1769", text)
        self.assertIn("30,000 claimed between them", text)

    def test_a_ladderless_section_among_others_is_refused(self):
        """It claims the whole position, leaving the others nothing."""
        code, text = self.check(
            config(sections=[dict(SEP_OCT, limit_ladder=[]), SEP_NOV]))
        self.assertIn("claims the whole position", text)
        self.assertEqual(code, 1)

    def test_different_near_months_are_not_pooled(self):
        code, text = self.check(config(sections=[
            SEP_OCT,
            {"name": "Oct into Dec", "near_token": "1500",
             "near_expiry": "2026-10-29", "far_token": "1600",
             "far_expiry": "2026-12-29",
             "limit_ladder": [{"bps": "30", "qty": 5000}]}]))
        self.assertNotIn("sections sell token", text)
        self.assertEqual(code, 0)


class TestAMissingTokenStillFails(CheckCase):
    def test_it_says_which_section(self):
        code, text = self.check(config(sections=[
            SEP_OCT, dict(SEP_NOV, far_token="")]))
        self.assertEqual(code, 1)
        self.assertIn("(not set)", text)
        self.assertIn("--find", text)


# ------------------------------------------------------------- recorder
class TestEachRollGetsItsOwnEvidenceFile(unittest.TestCase):
    """Sep into Oct and Sep into Nov are different costs against different
    limits. Pooled, the day's closest approach belongs to neither."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="recname_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_an_unnamed_recorder_keeps_the_plain_filename(self):
        got = Recorder(self.dir)
        self.assertEqual(os.path.basename(got.path),
                         f"market-{date.today():%Y-%m-%d}.csv")

    def test_a_named_one_says_which_roll(self):
        got = Recorder(self.dir, name="Sep into Oct")
        self.assertIn("Sep_into_Oct", os.path.basename(got.path))

    def test_a_section_key_is_made_safe_for_a_filename(self):
        """"1769>1584" is not a filename on Windows."""
        got = Recorder(self.dir, name="1769>1584")
        self.assertNotIn(">", got.path)
        self.assertIn("1769_1584", got.path)

    def test_two_sections_do_not_share_a_file(self):
        a = Recorder(self.dir, name="Sep into Oct")
        b = Recorder(self.dir, name="Sep into Nov")
        self.assertNotEqual(a.path, b.path)

    def test_the_summary_names_the_roll(self):
        got = Recorder(self.dir, name="Sep into Oct")
        self.assertIn("Sep into Oct", got.summary())


# -------------------------------------------------------------- journal
class TestTheJournalSaysWhichRoll(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="jrnsec_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.journal = journallib.Journal(self.dir, enabled=True)

    def entries(self):
        return journallib.read(self.journal.path_for())

    def test_an_untagged_journal_is_unchanged(self):
        self.journal.note("x", "hello")
        self.assertNotIn("section", self.entries()[0])

    def test_a_section_view_stamps_every_line(self):
        view = self.journal.for_section("1769>1500", "Sep into Oct")
        view.note("x", "hello")
        entry = self.entries()[0]
        self.assertEqual(entry["section"], "1769>1500")
        self.assertEqual(entry["section_name"], "Sep into Oct")

    def test_the_near_leg_is_attributable(self):
        """The whole point: a SELL 1769 line carries no far leg, so without
        the stamp it could have come from either roll."""
        oct_ = self.journal.for_section("1769>1500", "Sep into Oct")
        nov = self.journal.for_section("1769>1584", "Sep into Nov")
        oct_.order("near", 2, "1769", 1000, D("95.78"), None)
        nov.order("near", 2, "1769", 5000, D("95.78"), None)

        got = [(e["section_name"], e["requested_qty"]) for e in self.entries()]
        self.assertEqual(got, [("Sep into Oct", 1000), ("Sep into Nov", 5000)])

    def test_they_share_one_file_so_the_order_of_events_survives(self):
        a = self.journal.for_section("1769>1500", "Sep into Oct")
        b = self.journal.for_section("1769>1584", "Sep into Nov")
        a.note("x", "first")
        b.note("x", "second")
        a.note("x", "third")
        self.assertEqual([e["message"] for e in self.entries()],
                         ["first", "second", "third"])
        self.assertEqual(len(os.listdir(self.dir)), 1)

    def test_a_disabled_journal_stays_disabled_through_a_view(self):
        off = journallib.Journal(self.dir, enabled=False)
        self.assertFalse(off.for_section("k", "n").note("x", "hello"))
        self.assertEqual(os.listdir(self.dir), [])

    def test_an_explicit_section_field_is_not_overwritten(self):
        view = self.journal.for_section("1769>1500", "Sep into Oct")
        view.write("x", section="deliberate")
        self.assertEqual(self.entries()[0]["section"], "deliberate")


class TestTheEngineTagsWhatItWrites(unittest.TestCase):
    """One roll writes what it always wrote; several say which is which."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="engsec_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def engine(self, **kw):
        from rollover.engine import RollEngine

        class Log:
            def info(self, m): pass
            warn = error = alert = info

        class Broker:
            logged_in = False
            scrip_file_date = None

        kw.setdefault("record_market", True)
        kw.setdefault("journal", True)
        cfg = config(use_live_feed=False, update_check=False, **kw)
        return RollEngine(cfg, Log(), self.dir, broker=Broker())

    def test_one_roll_writes_the_plain_file_and_untagged_lines(self):
        engine = self.engine()
        self.assertEqual(os.path.basename(engine.sections[0].recorder.path),
                         f"market-{date.today():%Y-%m-%d}.csv")
        self.assertNotIsInstance(engine.sections[0].journal,
                                 journallib.SectionJournal)

    def test_several_rolls_get_a_file_and_a_stamp_each(self):
        engine = self.engine(sections=[SEP_OCT, SEP_NOV])
        paths = [s.recorder.path for s in engine.sections]
        self.assertEqual(len(set(paths)), 2)
        for section in engine.sections:
            self.assertIsInstance(section.journal, journallib.SectionJournal)
            self.assertEqual(section.journal.key, section.key)

    def test_recording_off_means_no_recorder_anywhere(self):
        engine = self.engine(record_market=False, sections=[SEP_OCT, SEP_NOV])
        self.assertTrue(all(s.recorder is None for s in engine.sections))


# ------------------------------------------------------------- Go Live
class TestGoLiveCountsEverySection(unittest.TestCase):
    """Going live switches on every enabled section at once. A dialog that
    counted the one on screen would understate the money by the rest."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="golive_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def engine(self, sections, **kw):
        from rollover.engine import RollEngine

        class Log:
            def info(self, m): pass
            warn = error = alert = info

        class Broker:
            logged_in = False
            scrip_file_date = None

        cfg = config(sections=sections, use_live_feed=False,
                     record_market=False, journal=False, update_check=False,
                     **kw)
        return RollEngine(cfg, Log(), self.dir, broker=Broker())

    def exposure(self, sections, **kw):
        engine = self.engine(sections, **kw)
        got = livemode.exposure(engine.cfg, D("95.78"), lot_size=1000,
                                engine=engine)
        return got, got.lines(unit_confirmed=True)

    def test_the_outstanding_quantity_is_the_sum(self):
        got, _ = self.exposure([SEP_OCT, SEP_NOV])
        self.assertEqual(got.campaign_left, 30000)

    def test_and_so_is_the_money(self):
        got, lines = self.exposure([SEP_OCT, SEP_NOV])
        # 30,000 x 95.78 = 28.73 lakh
        self.assertTrue(any("28.73 lakh" in line for line in lines), lines)

    def test_each_one_is_named(self):
        _, lines = self.exposure([SEP_OCT, SEP_NOV])
        text = "\n".join(lines)
        self.assertIn("Sep into Oct: 10,000 left of 10,000", text)
        self.assertIn("Sep into Nov: 20,000 left of 20,000", text)

    def test_a_disabled_section_is_not_authorised_so_is_not_counted(self):
        got, lines = self.exposure([SEP_OCT, dict(SEP_NOV, enabled=False)])
        self.assertEqual(got.campaign_left, 10000)
        self.assertNotIn("Sep into Nov", "\n".join(lines))

    def test_progress_already_made_is_taken_off(self):
        engine = self.engine([SEP_OCT, SEP_NOV])
        engine.sections[1].ladder_progress = {"50": 6000}
        got = livemode.exposure(engine.cfg, D("95.78"), lot_size=1000,
                                engine=engine)
        self.assertEqual(got.campaign_left, 24000)

    def test_the_clip_shown_is_the_largest_one_a_section_could_send(self):
        """It answers "what could one order commit", and they differ."""
        got, _ = self.exposure([SEP_OCT, dict(SEP_NOV, lots=5)])
        self.assertEqual(got.lots, 5)
        self.assertEqual(got.qty_sent, 5000)

    def test_one_section_reads_exactly_as_it_did_before(self):
        engine = self.engine([])
        got = livemode.exposure(engine.cfg, D("95.78"), lot_size=1000,
                                engine=engine)
        self.assertEqual(got.campaign_left, 20000)
        self.assertEqual(got.rolls, [])
        self.assertFalse(any("authorises all" in line
                             for line in got.lines(unit_confirmed=True)))

    def test_a_broken_section_does_not_stop_the_dialog_opening(self):
        engine = self.engine([SEP_OCT, SEP_NOV])

        class Awkward:
            enabled = True
            name = "broken"

            @property
            def cfg(self):
                raise RuntimeError("no")

        engine.sections.append(Awkward())
        got = livemode.exposure(engine.cfg, D("95.78"), lot_size=1000,
                                engine=engine)
        self.assertEqual(got.campaign_left, 30000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
