"""Several sections in the window.

Stacking every section's cards would put the second one below the fold, which
is no way to compare them. A row each, with the full detail for whichever is
selected, keeps both the comparison and the detail available.

Skipped where there is no display.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
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

from rollover import editing, gates, rule
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


def contract(token, desc, expiry):
    return InstrumentInfo(token=token, symbol="USDINR", sec_desc=desc,
                          segment="13", lot_size=1000, expiry=expiry,
                          instrument="FUTCUR", price_divisor=D("10000000"),
                          tick=D("0.0025"), tick_units=D("25000"),
                          low_range=D("93"), high_range=D("99"))


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


@unittest.skipUnless(HAVE_TK, "no display")
class SectionsUICase(unittest.TestCase):
    CONTRACTS = {"1769": ("USDINR26SEPFUT", date(2026, 9, 28)),
                 "1500": ("USDINR26OCTFUT", date(2026, 10, 29)),
                 "1584": ("USDINR26NOVFUT", date(2026, 11, 26))}
    PRICES = {"1769": ("95.7800", "95.7825"),
              "1500": ("95.8600", "95.8625"),
              "1584": ("96.0500", "96.0525")}

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sectionsui_")
        self.path = os.path.join(self.dir, "config.json")
        self.log = StubLog()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def build(self, sections=None, held=100000, **kw):
        from rollover.engine import RollEngine
        from rollover.ui import RollWindow

        self.cfg = RollConfig(
            near_token="1769", near_expiry="2026-09-28",
            far_token="1584", far_expiry="2026-11-26",
            limit_ladder=[{"bps": "50", "qty": 20000}],
            use_live_feed=False, record_market=False, update_check=False,
            journal=False, dry_run=True, sections=sections or [], **kw)
        self.cfg.save(self.path)

        self.engine = RollEngine(self.cfg, self.log, self.dir,
                                 broker=StubBroker())
        for section in self.engine.sections:
            for role in ("near", "far"):
                token = getattr(section.cfg, f"{role}_token")
                desc, expiry = self.CONTRACTS[token]
                setattr(section, role, contract(token, desc, expiry))
        self.engine.near_positions = {"1769": held}
        self.engine._share_out()

        now = time.monotonic()
        self.quotes = {
            token: Quote(token, D(bid), D(ask), now, D(1),
                         bid_qty=400000, ask_qty=400000)
            for token, (bid, ask) in self.PRICES.items()}

        for section in self.engine.sections:
            nq = self.quotes[section.near.token]
            fq = self.quotes[section.far.token]
            section.decision = rule.compute(
                nq, fq, section.cfg, days=self.engine.tenor_days(section),
                ladder=section.ladder, progress=section.ladder_progress,
                watch=section.watch_limits)
            section.report = gates.evaluate(section.cfg, section, self.quotes,
                                            section.decision)
            section.quotes = (nq, fq)

        self.window = RollWindow(_root, self.engine, self.log,
                                 config_path=self.path)
        self.window.withdraw()
        self.addCleanup(self.window.destroy)
        return self.window

    def draw(self):
        first = self.engine.sections[0]
        self.engine._publish(first.quotes[0], first.quotes[1], first.decision,
                             "watching", first.report)
        self.window._draw(self.engine.snapshot())
        _root.update_idletasks()

    def rows(self):
        return [self.window.strip.item(i)["values"]
                for i in self.window.strip.get_children()]


class TestOneSectionLooksAsItAlwaysDid(SectionsUICase):
    def test_the_strip_is_hidden(self):
        self.build()
        self.draw()
        self.assertFalse(self.window._strip_shown)

    def test_the_cards_still_show_the_one_roll(self):
        self.build()
        self.draw()
        self.assertEqual(self.window._focused().key, "1769>1584")


class TestTheStrip(SectionsUICase):
    def setUp(self):
        super().setUp()
        self.build(sections=[SEP_OCT, SEP_NOV])
        self.engine.sections[1].ladder_progress = {"50": 6000}
        self.draw()

    def test_it_appears_with_a_row_per_section(self):
        self.assertTrue(self.window._strip_shown)
        self.assertEqual(len(self.rows()), 2)

    def test_each_row_names_its_two_contracts(self):
        self.assertIn("USDINR26OCTFUT", self.rows()[0][1])
        self.assertIn("USDINR26NOVFUT", self.rows()[1][1])

    def test_each_row_carries_its_own_cost_and_limit(self):
        self.assertIn("bps", str(self.rows()[0][2]))
        self.assertEqual(str(self.rows()[0][3]), "30 bps")
        self.assertEqual(str(self.rows()[1][3]), "50 bps")

    def test_the_costs_differ_because_the_far_legs_differ(self):
        self.assertNotEqual(self.rows()[0][2], self.rows()[1][2])

    def test_each_row_shows_how_far_inside_its_own_limit_it_is(self):
        self.assertIn("inside", str(self.rows()[0][4]))

    def test_each_row_shows_its_own_progress(self):
        self.assertEqual(str(self.rows()[0][5]), "0 / 10,000")
        self.assertEqual(str(self.rows()[1][5]), "6,000 / 20,000")

    def test_the_shared_position_is_shown_once(self):
        text = self.window.allocation_label.cget("text")
        self.assertIn("30,000", text)
        self.assertIn("100,000", text)
        self.assertIn("fits", text)

    def test_an_overdrawn_position_is_shown_as_a_refusal(self):
        self.engine.near_positions = {"1769": 20000}
        self.engine._share_out()
        self.draw()
        self.assertIn("sold twice", self.window.allocation_label.cget("text"))


class TestSelectingASection(SectionsUICase):
    def setUp(self):
        super().setUp()
        self.build(sections=[SEP_OCT, SEP_NOV])
        self.draw()

    def test_the_first_is_shown_to_begin_with(self):
        self.assertEqual(self.window._focused().name, "Sep into Oct")

    def test_the_cards_below_follow_the_selection(self):
        self.window.focus_key = "1769>1584"
        self.draw()
        self.assertEqual(self.window._focused().name, "Sep into Nov")

    def test_so_do_the_limit_cards(self):
        self.assertEqual([r["bps"].get() for r in self.window.rung_rows], ["30"])
        self.window.focus_key = "1769>1584"
        self.window._rebuild_rung_rows()
        self.assertEqual([r["bps"].get() for r in self.window.rung_rows], ["50"])

    def test_the_strip_says_which_one_is_below(self):
        self.assertIn("Sep into Oct", self.window.strip_note.cget("text"))


class TestEditingOneSectionsLimits(SectionsUICase):
    def setUp(self):
        super().setUp()
        self.build(sections=[SEP_OCT, SEP_NOV])
        self.draw()

    def test_it_writes_to_the_section_on_screen(self):
        self.window.rung_rows[0]["qty"].set("4000")
        self.window._apply_ladder()
        self.assertEqual(self.cfg.sections[0]["limit_ladder"],
                         [{"bps": "30", "qty": "4000"}])

    def test_the_other_section_is_untouched(self):
        self.window.rung_rows[0]["qty"].set("4000")
        self.window._apply_ladder()
        self.assertEqual(self.cfg.sections[1]["limit_ladder"],
                         [{"bps": "50", "qty": 20000}])

    def test_editing_the_second_writes_to_the_second(self):
        self.window.focus_key = "1769>1584"
        self.window._rebuild_rung_rows()
        self.window.rung_rows[0]["qty"].set("7000")
        self.window._apply_ladder()
        self.assertEqual(self.cfg.sections[1]["limit_ladder"],
                         [{"bps": "50", "qty": "7000"}])
        self.assertEqual(self.cfg.sections[0]["limit_ladder"],
                         [{"bps": "30", "qty": 10000}])

    def test_a_rung_above_that_sections_tenor_limit_is_refused(self):
        """Sep into Oct is a one month roll, so its ceiling is 30 bps."""
        self.window.rung_rows[0]["bps"].set("45")
        self.window._apply_ladder()
        self.assertIn("looser", self.window.ladder_note.cget("text"))

    def test_applying_disarms(self):
        self.engine.arm()
        self.window.rung_rows[0]["qty"].set("4000")
        self.window._apply_ladder()
        self.assertFalse(self.engine.armed)


class TestAddingAndRemovingFromTheWindow(SectionsUICase):
    def setUp(self):
        super().setUp()
        self.restarts = []
        self.build(sections=[SEP_OCT, SEP_NOV])
        self.window.on_change_contracts = lambda: self.restarts.append(1)
        self.draw()

    def test_removing_the_selected_section(self):
        import tkinter.messagebox as mb
        real = mb.askyesno
        mb.askyesno = lambda *a, **kw: True
        try:
            self.window._remove_section()
        finally:
            mb.askyesno = real
        self.assertEqual([s["name"] for s in self.cfg.sections],
                         ["Sep into Nov"])
        self.assertEqual(self.restarts, [1])

    def test_a_refused_removal_says_why(self):
        import tkinter.messagebox as mb
        self.cfg.sections = editing.remove(self.cfg, "1769>1500")
        self.window.focus_key = "1769>1584"
        self.draw()

        real = mb.askyesno
        mb.askyesno = lambda *a, **kw: True
        try:
            self.window._remove_section()
        finally:
            mb.askyesno = real
        self.assertIn("at least one section",
                      self.window.section_note.cget("text"))

    def test_disabling_a_section(self):
        self.window._toggle_section()
        self.assertFalse(self.cfg.sections[0]["enabled"])
        self.assertEqual(self.restarts, [1])

    def test_enabling_one_with_no_limits_is_refused(self):
        """That is the moment it could start selling."""
        case = TestAddingAndRemovingFromTheWindow("test_disabling_a_section")
        case.setUp()
        case.build(sections=[dict(SEP_OCT, limit_ladder=[], enabled=False),
                             SEP_NOV])
        case.draw()
        self.assertFalse(case.window._focused().enabled)

        case.window._toggle_section()
        self.assertIn("no limit_ladder", case.window.section_note.cget("text"))
        self.assertFalse(case.cfg.sections[0].get("enabled", True))

    def test_the_button_says_what_it_would_do(self):
        self.assertEqual(self.window.enable_button._label, "Disable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
