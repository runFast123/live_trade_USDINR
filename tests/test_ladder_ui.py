"""The ladder as the operator actually works with it.

The single ROLL LIMIT box could be typed into. The ladder was first built as a
read-only table, which is no use to someone who wants to decide there and then
that they will do ten thousand at thirty basis points and twenty at fifty. So
each rung is now the same kind of control: a figure you type, the rupee amount
it comes to, the far ask that would satisfy it, and how far away the market is.

Skipped where there is no display.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import tempfile
import unittest
from datetime import date

try:
    import tkinter as tk
    _root = tk.Tk()
    _root.withdraw()
    HAVE_TK = True
except Exception:                       # pragma: no cover - headless
    _root = None
    HAVE_TK = False

from rollover import rule
from rollover.config import RollConfig
from rollover.money import D
from rollover.quotes import Quote

NOW = dt.datetime(2026, 9, 18, 11, 0, 0)
TWO_RUNGS = [{"bps": "30", "qty": 10000}, {"bps": "50", "qty": 20000}]


class StubLog:
    def __init__(self):
        self.lines = []

    def info(self, m): self.lines.append(m)
    def warn(self, m): self.lines.append(m)
    def error(self, m): self.lines.append(m)
    def alert(self, m): self.lines.append(m)
    def tail(self, n=200): return []

    def text(self):
        return "\n".join(self.lines)


class StubBroker:
    logged_in = True
    scrip_file_date = date.today()


def quote(token, bid, ask):
    return Quote(token, D(bid), D(ask), NOW, D(1), bid_qty=500, ask_qty=500)


@unittest.skipUnless(HAVE_TK, "no display")
class LadderUICase(unittest.TestCase):
    def setUp(self):
        from rollover.engine import RollEngine
        from rollover.ui import RollWindow

        self.dir = tempfile.mkdtemp(prefix="ladderui_")
        self.path = os.path.join(self.dir, "config.json")
        self.log = StubLog()

        self.cfg = RollConfig(
            near_token="1769", far_token="1584",
            near_expiry="2026-09-28", far_expiry="2026-11-26",
            use_live_feed=False, record_market=False, update_check=False,
            journal=False, dry_run=True, limit_ladder=list(TWO_RUNGS))
        self.cfg.save(self.path)

        self.engine = RollEngine(self.cfg, self.log, self.dir,
                                 broker=StubBroker())
        self.window = RollWindow(_root, self.engine, self.log,
                                 config_path=self.path)
        self.window.withdraw()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(self.window.destroy)

    def rows(self):
        return self.window.rung_rows

    def note(self):
        return self.window.ladder_note.cget("text")

    def add(self, bps, qty):
        self.window._add_rung()
        self.rows()[-1]["bps"].set(bps)
        self.rows()[-1]["qty"].set(qty)

    def applied(self):
        return [(r["bps"], r["qty"]) for r in (self.cfg.limit_ladder or [])]

    def saved(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)["limit_ladder"]

    def draw(self, far_ask="96.0500"):
        decision = rule.compute(
            quote("1769", "95.7800", "95.7825"),
            quote("1584", D(far_ask) - D("0.0025"), far_ask),
            self.cfg, days=59, ladder=self.engine.ladder,
            progress=self.engine.ladder_progress)
        self.window._draw_ladder(decision)
        _root.update_idletasks()
        return decision

    def cell(self, index, name):
        return self.rows()[index]["cells"][name].cget("text")


class TestItStartsFromTheConfiguredLadder(LadderUICase):
    def test_one_row_per_rung(self):
        self.assertEqual(len(self.rows()), 2)

    def test_the_rows_carry_the_configured_values(self):
        self.assertEqual([r["bps"].get() for r in self.rows()], ["30", "50"])
        self.assertEqual([r["qty"].get() for r in self.rows()], ["10000", "20000"])


class TestTheLiveNumbers(LadderUICase):
    def test_each_rung_shows_its_own_rupee_limit(self):
        self.draw()
        self.assertEqual(self.cell(0, "rupees"), "0.2873")
        self.assertEqual(self.cell(1, "rupees"), "0.4789")

    def test_each_rung_shows_the_far_ask_that_would_satisfy_it(self):
        """The number to watch the market against, per rung."""
        self.draw()
        self.assertEqual(self.cell(0, "target"), "96.0673")
        self.assertEqual(self.cell(1, "target"), "96.2589")

    def test_a_rung_the_market_has_passed_reads_negative(self):
        self.draw(far_ask="96.0500")          # 28 bps, inside both
        self.assertTrue(self.cell(0, "gap").startswith("-"), self.cell(0, "gap"))

    def test_a_rung_out_of_reach_reads_positive(self):
        self.draw(far_ask="96.4000")          # 65 bps, outside both
        self.assertTrue(self.cell(0, "gap").startswith("+"), self.cell(0, "gap"))
        self.assertIn("bps away", self.cell(0, "status"))

    def test_the_rung_being_worked_says_so(self):
        self.draw(far_ask="96.0500")
        self.assertEqual(self.cell(0, "status"), "WORKING")

    def test_a_cheaper_rung_that_is_spent_shows_done(self):
        self.engine.ladder_progress = {"30": 10000}
        self.draw(far_ask="96.0500")
        self.assertEqual(self.cell(0, "status"), "done")
        self.assertEqual(self.cell(1, "status"), "WORKING")

    def test_progress_is_shown_per_rung(self):
        self.engine.ladder_progress = {"30": 4000}
        self.draw()
        self.assertEqual(self.cell(0, "rolled"), "4,000 / 10,000")

    def test_the_total_shows_the_clip_size_too(self):
        """A 10,000 rung at a 1,000 clip is ten orders, and that should show."""
        self.draw()
        self.assertIn("30,000", self.window.ladder_total.cget("text"))
        self.assertIn("clip 1,000", self.window.ladder_total.cget("text"))

    def test_a_row_not_yet_applied_shows_nothing_invented(self):
        self.add("40", "5000")
        self.draw()
        self.assertEqual(self.cell(2, "status"), "not set")
        self.assertEqual(self.cell(2, "rupees"), "--")


class TestAddingAndRemoving(LadderUICase):
    def test_adding_a_rung_applies_it(self):
        self.add("40", "5000")
        self.window._apply_ladder()
        self.assertEqual(len(self.rows()), 3)
        self.assertIn(("40", "5000"), self.applied())

    def test_the_engine_uses_it_immediately(self):
        self.add("40", "5000")
        self.window._apply_ladder()
        self.assertIn(D("40"), [r.bps for r in self.engine.ladder.rungs])

    def test_it_is_written_to_config_json(self):
        self.add("40", "5000")
        self.window._apply_ladder()
        self.assertEqual(self.note(), "saved")
        self.assertIn({"bps": "40", "qty": "5000"}, self.saved())

    def test_removing_a_rung_applies_too(self):
        self.window._remove_rung(self.rows()[0])
        self.window._apply_ladder()
        self.assertEqual([r["bps"] for r in self.cfg.limit_ladder], ["50"])

    def test_removing_every_rung_leaves_the_single_limit(self):
        for row in list(self.rows()):
            self.window._remove_rung(row)
        self.window._apply_ladder()
        self.assertEqual(self.cfg.limit_ladder, [])
        self.assertFalse(self.engine.ladder)

    def test_a_blank_row_is_not_a_rung(self):
        self.window._add_rung()               # left empty
        self.window._apply_ladder()
        self.assertEqual(len(self.cfg.limit_ladder), 2)

    def test_half_a_row_is_refused(self):
        self.add("40", "")
        self.window._apply_ladder()
        self.assertIn("both a limit and a quantity", self.note())
        self.assertEqual(len(self.cfg.limit_ladder), 2)

    def test_escape_puts_the_rows_back(self):
        self.rows()[0]["bps"].set("999")
        self.window._rebuild_rung_rows()
        self.assertEqual(self.rows()[0]["bps"].get(), "30")


class TestWhatItRefuses(LadderUICase):
    def test_a_rung_looser_than_the_tenor_limit(self):
        """A ladder spends within the client's limit. It may not raise it."""
        self.add("70", "5000")
        self.window._apply_ladder()
        self.assertIn("looser", self.note())
        self.assertEqual(len(self.cfg.limit_ladder), 2)

    def test_a_rung_exactly_at_the_ceiling_is_allowed(self):
        self.window._remove_rung(self.rows()[1])      # free up 50
        self.add("50", "5000")
        self.window._apply_ladder()
        self.assertEqual(self.note(), "saved")

    def test_a_quantity_off_the_lot_grid(self):
        self.add("40", "1500")
        self.window._apply_ladder()
        self.assertIn("lot size", self.note())

    def test_a_duplicate_limit(self):
        self.add("30", "1000")
        self.window._apply_ladder()
        self.assertIn("twice", self.note())

    def test_something_that_is_not_a_number(self):
        self.add("soon", "lots")
        self.window._apply_ladder()
        self.assertIn("not a number", self.note())

    def test_a_refused_row_stays_on_screen_to_be_fixed(self):
        self.add("40", "1500")
        self.window._apply_ladder()
        self.assertEqual(len(self.rows()), 3, "the bad row was thrown away")
        self.assertEqual(self.rows()[2]["qty"].get(), "1500")

    def test_nothing_is_saved_when_it_is_refused(self):
        self.add("40", "1500")
        self.window._apply_ladder()
        self.assertEqual(self.saved(), TWO_RUNGS)

    def test_no_tenor_means_no_ceiling_and_no_ladder(self):
        """Without a ceiling a rung could quietly exceed the client's limit."""
        self.cfg.near_expiry = self.cfg.far_expiry = ""
        self.engine.session.near = self.engine.session.far = None
        self.add("40", "5000")
        self.window._apply_ladder()
        self.assertIn("tenor is not known", self.note())


class TestApplyingIsDeliberate(LadderUICase):
    def test_it_disarms(self):
        """A typed digit must not fire a roll on the next tick."""
        self.engine.arm()
        self.add("40", "5000")
        self.window._apply_ladder()
        self.assertFalse(self.engine.armed)

    def test_an_unchanged_ladder_says_so_and_does_not_disarm(self):
        self.engine.arm()
        self.window._apply_ladder()
        self.assertEqual(self.note(), "unchanged")
        self.assertTrue(self.engine.armed)

    def test_the_change_is_logged(self):
        self.add("40", "5000")
        self.window._apply_ladder()
        self.assertIn("Ladder set to 3 rung(s)", self.log.text())


class TestProgressAcrossAnEdit(LadderUICase):
    def test_progress_survives_a_rung_that_is_unchanged(self):
        self.engine.ladder_progress = {"30": 4000}
        self.add("40", "5000")
        self.window._apply_ladder()
        self.assertEqual(self.engine.ladder_progress.get("30"), 4000)

    def test_progress_is_dropped_for_a_rung_that_is_removed(self):
        """A rung that is gone is a different commitment, not a part-done one."""
        self.engine.ladder_progress = {"30": 4000, "50": 1000}
        self.window._remove_rung(self.rows()[0])
        self.window._apply_ladder()
        self.assertNotIn("30", self.engine.ladder_progress)
        self.assertEqual(self.engine.ladder_progress.get("50"), 1000)

    def test_repricing_a_rung_does_not_carry_its_history_over(self):
        self.engine.ladder_progress = {"30": 4000}
        self.rows()[0]["bps"].set("35")
        self.window._apply_ladder()
        self.assertEqual(self.engine.ladder_progress, {})
        self.assertIn("progress dropped", self.log.text())

    def test_the_drop_is_written_to_disk(self):
        self.engine.ladder_progress = {"30": 4000}
        self.window._remove_rung(self.rows()[0])
        self.window._apply_ladder()

        from rollover.state import StateStore
        self.assertEqual(StateStore(self.dir).load().ladder_done, {})


class TestTheTenorFallback(unittest.TestCase):
    """Before the contracts load there is no tenor, so config supplies it."""

    def setUp(self):
        from rollover.engine import RollEngine
        self.dir = tempfile.mkdtemp(prefix="tenor_")
        self.addCleanup(shutil.rmtree, self.dir, True)

        cfg = RollConfig(near_token="1769", far_token="1584",
                         near_expiry="2026-09-28", far_expiry="2026-11-26",
                         use_live_feed=False, record_market=False,
                         update_check=False, journal=False)
        self.engine = RollEngine(cfg, StubLog(), self.dir, broker=StubBroker())

    def test_the_tenor_comes_from_config_before_the_contracts_load(self):
        self.assertIsNone(self.engine.session.near)
        self.assertEqual(self.engine.tenor_days(), 59)

    def test_and_so_does_the_ceiling(self):
        self.assertEqual(self.engine._tenor_ceiling_bps(), D("50"))

    def test_without_either_there_is_no_ceiling(self):
        self.engine.cfg.near_expiry = self.engine.cfg.far_expiry = ""
        self.assertIsNone(self.engine.tenor_days())
        self.assertIsNone(self.engine._tenor_ceiling_bps())

    def test_a_malformed_expiry_is_not_a_crash(self):
        self.engine.cfg.near_expiry = "not a date"
        self.assertIsNone(self.engine.tenor_days())


if __name__ == "__main__":
    unittest.main(verbosity=2)
