"""The operator's actual sequence, pressed through the real window.

Every test in here comes from a fault reported from a running build. They had
one cause between them: config.json changed and the engine went on running the
list it was constructed with, so the window drew a stale set of sections and
every button acted on one that was no longer there.

What that cost, in the order it was hit:

  * limits were typed, saved to config.json, and vanished off the screen --
    rebuild_ladder read the TOP-LEVEL limit_ladder and wrote the result to
    sections[0], so editing any section rebuilt a different one from a
    different source;
  * Add section opened a SECOND contract picker, because putting a change
    into force called on_change_contracts, which is the contract chooser;
  * the strip kept drawing the section that had just been deleted, so an
    operator who added a section and then pressed Remove deleted the roll
    they already had;
  * and the limit cards, rebuilt against the previous snapshot, were filled
    from the previous section -- so Set limits wrote one section's ladder
    into another's.

Skipped where there is no display.
"""
from __future__ import annotations

import json
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

from rollover import gates, rule
from rollover.broker import InstrumentInfo
from rollover.config import RollConfig
from rollover.money import D
from rollover.quotes import Quote

CONTRACTS = {
    "1769": ("USDINR26SEPFUT", date(2026, 9, 28)),
    "1284": ("USDINR26OCTFUT", date(2026, 10, 28)),
    "1584": ("USDINR26NOVFUT", date(2026, 11, 26)),
}
PRICES = {"1769": ("95.9525", "95.9600", 50, 302),
          "1284": ("96.1000", "96.1200", 30, 90),
          "1584": ("96.4050", "96.5300", 10, 106)}


class StubLog:
    def __init__(self):
        self.lines = []

    def info(self, m): self.lines.append(m)
    def warn(self, m): self.lines.append(m)
    def error(self, m): self.lines.append(m)
    def alert(self, m): self.lines.append(m)
    def tail(self, n=200): return []
    def recent(self, n=300): return []

    def text(self):
        return "\n".join(self.lines)


class StubBroker:
    logged_in = True
    scrip_file_date = date.today()


def contract(token):
    desc, expiry = CONTRACTS[token]
    return InstrumentInfo(token=token, symbol="USDINR", sec_desc=desc,
                          segment="13", lot_size=1000, expiry=expiry,
                          instrument="FUTCUR", price_divisor=D("10000000"),
                          tick=D("0.0025"), tick_units=D("25000"),
                          low_range=D("93"), high_range=D("99"))


def row(token):
    desc, expiry = CONTRACTS[token]
    return {"Token": token, "SecDesc": desc, "Expiry": expiry.isoformat()}


@unittest.skipUnless(HAVE_TK, "no display")
class WorkflowCase(unittest.TestCase):
    """Starts from exactly what the operator had: one pair, and no ladder."""

    def setUp(self):
        from rollover.engine import RollEngine
        from rollover.ui import RollWindow

        self.dir = tempfile.mkdtemp(prefix="workflow_")
        self.path = os.path.join(self.dir, "config.json")
        self.log = StubLog()
        self.addCleanup(shutil.rmtree, self.dir, True)

        self.cfg = RollConfig(
            near_token="1769", near_expiry="2026-09-28",
            far_token="1584", far_expiry="2026-11-26",
            limit_ladder=[], watch_limits=[], sections=[],
            limit_bps_schedule={"1": "30", "2": "30"},
            use_live_feed=False, record_market=False, update_check=False,
            journal=False, dry_run=True, quantity_unit_confirmed=True)
        self.cfg.save(self.path)

        self.engine = RollEngine(self.cfg, self.log, self.dir,
                                 broker=StubBroker())
        self.window = RollWindow(_root, self.engine, self.log,
                                 config_path=self.path)
        self.window.withdraw()
        self.addCleanup(self.window.destroy)
        self.tick()

    # ---- driving it -------------------------------------------------------
    def tick(self):
        """A tick's worth of pricing, without a network."""
        now = time.monotonic()
        quotes = {t: Quote(t, D(b), D(a), now, D(1), bid_qty=bq, ask_qty=aq)
                  for t, (b, a, bq, aq) in PRICES.items()}
        self.engine.account.logged_in = True
        self.engine.account.market_open = True
        self.engine.account.scrip_file_date = date.today()
        self.engine.near_positions = {"1769": 100000}
        for s in self.engine.sections:
            for role in ("near", "far"):
                token = getattr(s.cfg, f"{role}_token")
                if token in CONTRACTS:
                    setattr(s, role, contract(token))
        self.engine._share_out()
        for s in self.engine.sections:
            if s.near is None or s.far is None:
                continue
            nq, fq = quotes[s.near.token], quotes[s.far.token]
            s.decision = rule.compute(nq, fq, s.cfg,
                                      days=self.engine.tenor_days(s),
                                      ladder=s.ladder,
                                      progress=s.ladder_progress,
                                      watch=s.watch_limits)
            s.report = gates.evaluate(s.cfg, s, quotes, s.decision)
            s.quotes = (nq, fq)
        first = self.engine.sections[0]
        self.engine._publish(first.quotes[0] if first.quotes else None,
                             first.quotes[1] if first.quotes else None,
                             first.decision, "watching", first.report)
        self.window._draw(self.engine.snapshot())
        _root.update_idletasks()

    def set_limit(self, bps, qty=""):
        self.window._add_rung()
        self.window.rung_rows[-1]["bps"].set(bps)
        self.window.rung_rows[-1]["qty"].set(qty)
        self.window._apply_ladder()
        self.tick()

    def add_section(self, near, far):
        def picker(title, apply):
            apply(row(near), row(far))
            return True
        self.window._pick_contracts = picker
        self.window._add_section()
        self.tick()

    def remove_section(self):
        import tkinter.messagebox as mb
        real = mb.askyesno
        mb.askyesno = lambda *a, **kw: True
        try:
            self.window._remove_section()
        finally:
            mb.askyesno = real
        self.tick()

    # ---- reading it back --------------------------------------------------
    def cards(self):
        return [(r["bps"].get(), r["qty"].get())
                for r in self.window.rung_rows]

    def engine_keys(self):
        return [s.key for s in self.engine.sections]

    def saved(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def ladders(self):
        return {s.name: s.ladder.total_qty for s in self.engine.sections}


class TestSettingALimitFromNothing(WorkflowCase):
    """Reported as: "I set the limit but it is not showing"."""

    def setUp(self):
        super().setUp()
        self.set_limit("30", "10000")

    def test_one_roll_does_not_have_a_name_shouted_at_it(self):
        """With one section there is nothing to distinguish."""
        self.assertEqual(self.window.ladder_heading.cget("text"),
                         "ROLL COST AT EACH LIMIT")
        self.assertEqual(self.window.remove_button._label, "Remove")

    def test_the_card_stays_on_screen(self):
        self.assertEqual(self.cards(), [("30", "10000")])

    def test_the_engine_is_actually_using_it(self):
        self.assertEqual(self.engine.sections[0].ladder.total_qty, 10000)

    def test_it_reaches_config_json(self):
        self.assertEqual(self.saved()["limit_ladder"],
                         [{"bps": "30", "qty": "10000"}])

    def test_the_header_counts_it(self):
        self.assertIn("10,000", self.window.ladder_total.cget("text"))

    def test_the_invitation_is_gone(self):
        self.assertFalse(self.window._ladder_empty_shown)


class TestSettingALimitOnASection(WorkflowCase):
    """The same thing once sections exist, which is where it broke.

    rebuild_ladder took no argument: it read the top-level limit_ladder and
    wrote the result to sections[0]. So editing section two saved the limits
    to config.json and then rebuilt section one from a config that had not
    changed -- the limits were written, and vanished.
    """

    def setUp(self):
        super().setUp()
        self.set_limit("30", "10000")
        self.add_section("1769", "1284")
        self.set_limit("25", "5000")

    def test_the_new_sections_limits_stay_on_screen(self):
        self.assertEqual(self.cards(), [("25", "5000")])

    def test_the_engine_uses_them(self):
        self.assertEqual(self.ladders(),
                         {"Sep into Nov": 10000, "Sep into Oct": 5000})

    def test_the_other_section_is_untouched(self):
        self.assertEqual(self.cfg.sections[0]["limit_ladder"],
                         [{"bps": "30", "qty": "10000"}])

    def test_no_limits_leak_between_sections(self):
        """The cards were rebuilt against the previous snapshot, so they were
        filled from the previous section -- and Set limits wrote that
        section's rungs into this one's ladder."""
        self.assertEqual(self.cfg.sections[1]["limit_ladder"],
                         [{"bps": "25", "qty": "5000"}])


class TestAddingASection(WorkflowCase):
    """Reported as: "trying to make new section but unable to"."""

    def setUp(self):
        super().setUp()
        self.set_limit("30", "10000")
        self.pickers = []
        self.window.on_change_contracts = lambda: self.pickers.append(1)
        self.add_section("1769", "1284")

    def test_both_sections_are_in_the_configuration(self):
        self.assertEqual([s["name"] for s in self.cfg.sections],
                         ["Sep into Nov", "Sep into Oct"])

    def test_the_original_pair_is_not_lost(self):
        """It was: the implicit pair has to be written down first, and then
        the engine has to be told, or Remove deletes the wrong one."""
        self.assertEqual(self.cfg.sections[0]["far_token"], "1584")

    def test_the_engine_runs_both(self):
        self.assertEqual(self.engine_keys(), ["1769>1584", "1769>1284"])

    def test_the_strip_shows_both(self):
        self.assertEqual(len(self.window.strip.get_children()), 2)

    def test_a_second_contract_picker_is_not_opened(self):
        self.assertEqual(self.pickers, [])

    def test_the_new_one_arrives_switched_off(self):
        self.assertFalse(self.cfg.sections[1].get("enabled", True))

    def test_the_operator_is_now_looking_at_the_new_one(self):
        """Or the limits they set next go to the section they were on."""
        self.assertEqual(self.window._focused().name, "Sep into Oct")

    def test_so_the_limit_cards_are_empty(self):
        self.assertEqual(self.cards(), [])

    def test_the_buttons_name_the_section_they_would_act_on(self):
        self.assertIn("Sep into Oct", self.window.remove_button._label)
        self.assertIn("Sep into Oct", self.window.enable_button._label)

    def test_the_limits_card_says_whose_limits_it_shows(self):
        self.assertIn("SEP INTO OCT",
                      self.window.ladder_heading.cget("text"))

    def test_the_note_names_it_and_says_what_to_do(self):
        note = self.window.section_note.cget("text")
        self.assertIn("Sep into Oct", note)
        self.assertIn("Enable", note)


class TestRemovingTheRightSection(WorkflowCase):
    """The expensive one.

    The strip drew the stale list, so the row on screen was the section the
    operator had before. Pressing Remove deleted that -- the roll they already
    had -- and left the disabled newcomer behind. That is what their
    config.json contained when they reported it.
    """

    def setUp(self):
        super().setUp()
        self.set_limit("30", "10000")
        self.add_section("1769", "1284")

    def test_removing_takes_out_the_one_on_screen(self):
        self.assertEqual(self.window._focused().name, "Sep into Oct")
        self.remove_section()
        self.assertEqual([s["name"] for s in self.cfg.sections],
                         ["Sep into Nov"])

    def test_and_the_engine_stops_running_it(self):
        self.remove_section()
        self.assertEqual(self.engine_keys(), ["1769>1584"])

    def test_the_survivor_keeps_its_ladder(self):
        self.remove_section()
        self.assertEqual(self.engine.sections[0].ladder.total_qty, 10000)

    def test_the_strip_follows(self):
        self.remove_section()
        self.assertEqual(len(self.window.strip.get_children()), 1)

    def test_a_second_removal_is_refused_rather_than_confusing(self):
        self.remove_section()
        self.remove_section()
        self.assertIn("at least one section",
                      self.window.section_note.cget("text"))


class TestEnablingASection(WorkflowCase):
    def setUp(self):
        super().setUp()
        self.set_limit("30", "10000")
        self.add_section("1769", "1284")
        self.set_limit("25", "5000")
        self.window._toggle_section()
        self.tick()

    def test_it_is_enabled_in_the_configuration(self):
        self.assertTrue(self.cfg.sections[1].get("enabled"))

    def test_the_engine_sees_it_enabled(self):
        self.assertTrue(self.engine.sections[1].enabled)

    def test_both_now_claim_a_share_of_the_position(self):
        self.assertEqual(self.engine.sections[0].near_position_qty, 95000)
        self.assertEqual(self.engine.sections[1].near_position_qty, 90000)

    def test_the_allocation_line_adds_them_up(self):
        text = self.window.allocation_label.cget("text")
        self.assertIn("15,000", text)
        self.assertIn("fits", text)

    def test_the_operator_stays_on_the_section_they_enabled(self):
        self.assertEqual(self.window._focused().name, "Sep into Oct")


class TestTheTwoLimitsAreToldApart(WorkflowCase):
    """The ROLL LIMIT box and a ladder rung are both called a limit.

    The box holds the ceiling for the tenor; the rule may be working a
    tighter rung from the limits below. A box reading 30 above a line reading
    "25 bps" is a contradiction until it says which is which.
    """

    def setUp(self):
        super().setUp()
        self.set_limit("30", "10000")
        self.add_section("1769", "1284")
        self.set_limit("25", "5000")
        self.window._describe_limit()

    def test_it_names_the_rung_it_is_working(self):
        note = self.window.limit_note.cget("text")
        self.assertIn("25 bps rung", note)

    def test_and_says_the_box_is_the_ceiling(self):
        self.assertIn("ceiling", self.window.limit_note.cget("text"))

    def test_nothing_is_said_when_they_agree(self):
        """No rung, so the box IS the limit and there is nothing to reconcile."""
        for row in list(self.window.rung_rows):
            self.window._remove_rung(row)
        self.window._apply_ladder()
        self.tick()
        self.window._describe_limit()
        self.assertNotIn("ceiling", self.window.limit_note.cget("text"))


class TestProgressSurvivesAReload(WorkflowCase):
    """Adding a section must not reset what the others have already rolled."""

    def setUp(self):
        super().setUp()
        self.set_limit("30", "10000")
        self.engine.sections[0].ladder_progress = {"30": 4000}
        self.engine.sections[0].clips_done_today = 3
        self.add_section("1769", "1284")

    def test_the_rungs_already_rolled_are_still_rolled(self):
        self.assertEqual(self.engine.sections[0].ladder_progress, {"30": 4000})

    def test_and_the_days_clip_count(self):
        """Or adding a section would be a way around the daily budget."""
        self.assertEqual(self.engine.sections[0].clips_done_today, 3)

    def test_the_strip_still_shows_the_progress(self):
        values = self.window.strip.item(
            self.window.strip.get_children()[0])["values"]
        self.assertEqual(str(values[5]), "4,000 / 10,000")


class TestEverySectionSwitchedOffIsNotSilent(WorkflowCase):
    """A config where nothing is enabled watches, passes gates, and can
    never fire. That has to be visible where the reason is read."""

    def setUp(self):
        super().setUp()
        self.set_limit("30", "10000")
        self.add_section("1769", "1284")
        # Switch off the only enabled one, leaving nothing that can trade.
        self.window.focus_key = "1769>1584"
        self.window._draw(self.engine.snapshot())
        self.window._toggle_section()
        self.tick()

    def test_it_says_nothing_will_trade(self):
        text = self.window.allocation_label.cget("text")
        self.assertIn("Nothing will trade", text)

    def test_and_says_what_to_do_about_it(self):
        self.assertIn("Enable", self.window.allocation_label.cget("text"))

    def test_enabling_one_clears_it(self):
        self.window._toggle_section()
        self.tick()
        self.assertNotIn("Nothing will trade",
                         self.window.allocation_label.cget("text"))


class TestTheWaitingColumnSaysSomethingTrue(WorkflowCase):
    """Reported as: "it say market is open but is close".

    The column printed the failing gate's NAME. Gate names are row labels, and
    read as statements the first one says the opposite of the truth.
    """

    def test_a_closed_market_does_not_read_as_market_open(self):
        self.engine.account.market_open = False
        for s in self.engine.sections:
            s.report = gates.evaluate(s.cfg, s, {}, s.decision)
        self.engine._republish()
        self.window._draw(self.engine.snapshot())
        _root.update_idletasks()

        state = str(self.window.strip.item(
            self.window.strip.get_children()[0])["values"][6])
        self.assertNotEqual(state, "market open")
        self.assertIn("closed", state)

    def test_the_column_is_labelled_as_a_reason(self):
        headings = [self.window.strip.heading(c)["text"]
                    for c in self.window.strip.cget("columns")]
        self.assertIn("Waiting for", headings)


if __name__ == "__main__":
    unittest.main(verbosity=2)
