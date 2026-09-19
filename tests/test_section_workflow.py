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

from rollover import gates
from rollover.broker import InstrumentInfo
from rollover.config import RollConfig
from rollover.money import D
from rollover.quotes import Quote

CONTRACTS = {
    "1769": ("USDINR26SEPFUT", date(2026, 9, 28)),
    "1284": ("USDINR26OCTFUT", date(2026, 10, 28)),
    "1584": ("USDINR26NOVFUT", date(2026, 11, 26)),
}
# Deep enough that the touch-size gates pass, so a section can actually reach
# READY and the "would roll next" mark has something to point at.
PRICES = {"1769": ("95.9525", "95.9600", 400000, 400000),
          "1284": ("96.1000", "96.1200", 400000, 400000),
          "1584": ("96.4050", "96.5300", 400000, 400000)}


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
        """The engine's REAL tick, with the network stubbed out.

        Deliberately not a hand-built imitation of it. Everything these tests
        are about -- what is priced, what may trade, which section would roll
        next -- is decided in _tick, and a harness that reproduced that logic
        would be testing the harness.
        """
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

        self.engine._read_quotes = lambda tokens: quotes
        self.engine._slow_refresh = lambda: None
        self.engine._tick(list(PRICES))

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


class TestWorkingTheStrip(WorkflowCase):
    """Clicking, sorting, and the mark for which section rolls next."""

    def setUp(self):
        super().setUp()
        self.set_limit("30", "10000")
        self.add_section("1769", "1284")
        self.set_limit("25", "5000")
        self.window._toggle_section()
        self.tick()

    def rows(self):
        return [self.window.strip.item(i)["values"]
                for i in self.window.strip.get_children()]

    def names(self):
        return [str(r[0]).strip().lstrip("> ").strip() for r in self.rows()]

    def click(self, index):
        item = self.window.strip.get_children()[index]
        self.window.strip.selection_set(item)
        self.window._focus_selected()
        _root.update_idletasks()

    def test_selecting_a_row_changes_the_section_below(self):
        """It matched the displayed name, so the marker for which section
        rolls next silently stopped selection working at all."""
        self.click(0)
        first = self.window._focused().key
        self.click(1)
        self.assertNotEqual(self.window._focused().key, first)

    def test_the_marker_does_not_break_the_match(self):
        marked = [r for r in self.rows() if str(r[0]).startswith(">")]
        self.assertTrue(marked, "nothing is marked as next")
        index = self.rows().index(marked[0])
        self.click(index)
        self.assertEqual(self.window._focused().key,
                         self.window._strip_keys[
                             self.window.strip.get_children()[index]])

    def test_the_section_that_would_roll_next_is_marked(self):
        snap = self.engine.snapshot()
        self.assertIsNotNone(snap.next_key)
        marked = [str(r[0]) for r in self.rows() if str(r[0]).startswith(">")]
        self.assertEqual(len(marked), 1)

    def test_the_mark_is_the_engines_choice_not_the_windows(self):
        """Or the row marked as next and the section actually chosen drift."""
        import rollover.book as book
        snap = self.engine.snapshot()
        rows = self.rows()
        marked = next(r for r in rows if str(r[0]).startswith(">"))
        index = rows.index(marked)
        key = self.window._strip_keys[
            self.window.strip.get_children()[index]]
        self.assertEqual(key, snap.next_key)
        self.assertTrue(hasattr(book, "pick"))

    def test_sorting_by_cost_orders_the_rows(self):
        self.window._sort_strip("cost")
        self.tick()
        costs = [str(r[2]) for r in self.rows()]
        self.assertEqual(costs, sorted(costs, key=lambda t: float(t.split()[0])))

    def test_clicking_the_same_heading_reverses_it(self):
        self.window._sort_strip("cost")
        self.tick()
        up = self.names()
        self.window._sort_strip("cost")
        self.tick()
        self.assertEqual(self.names(), list(reversed(up)))

    def test_a_third_click_goes_back_to_the_configured_order(self):
        for _ in range(3):
            self.window._sort_strip("cost")
        self.tick()
        self.assertIsNone(self.window._sort_by)
        self.assertEqual(self.names(), ["Sep into Nov", "Sep into Oct"])

    def test_sorting_cannot_change_what_trades(self):
        """It is a listing order. The choice is made in the engine."""
        before = self.engine.snapshot().next_key
        self.window._sort_strip("cost")
        self.tick()
        self.assertEqual(self.engine.snapshot().next_key, before)

    def test_a_missing_figure_sorts_last_rather_than_cheapest(self):
        for s in self.engine.sections:
            if s.name == "Sep into Oct":
                s.decision = None
        self.engine._republish()
        self.window._sort_strip("cost")
        self.window._draw(self.engine.snapshot())
        _root.update_idletasks()
        self.assertEqual(self.names()[-1], "Sep into Oct")

    def test_double_clicking_a_row_switches_it(self):
        class Event:
            x = 30
        item = self.window.strip.get_children()[1]
        bbox = self.window.strip.bbox(item)
        if not bbox:
            self.skipTest("the row is not laid out")
        event = Event()
        event.y = bbox[1] + bbox[3] // 2
        was = self.cfg.sections[1].get("enabled", True)
        self.window._strip_double_click(event)
        self.assertNotEqual(self.cfg.sections[1].get("enabled", True), was)

    def test_a_double_click_on_the_heading_does_nothing(self):
        class Event:
            x = 30
            y = 2
        before = [s.get("enabled", True) for s in self.cfg.sections]
        self.window._strip_double_click(Event())
        self.assertEqual([s.get("enabled", True) for s in self.cfg.sections],
                         before)

    def test_the_note_says_both_gestures(self):
        note = self.window.strip_note.cget("text")
        self.assertIn("click a row", note)
        self.assertIn("double-click", note)


class TestADisabledSectionIsStillPricedOnScreen(WorkflowCase):
    """The row you add to compare a roll cost showed every column as a dash.

    So the only way to see the number was to Enable it, which is the one
    action that lets it sell.
    """

    def setUp(self):
        super().setUp()
        self.set_limit("30", "10000")
        self.add_section("1769", "1284")     # arrives switched off
        self.set_limit("25", "5000")
        self.tick()

    def row(self):
        for item in self.window.strip.get_children():
            if self.window._strip_keys[item] == "1769>1284":
                return self.window.strip.item(item)["values"]
        raise AssertionError("the added section is not on the strip")

    def test_it_is_still_switched_off(self):
        self.assertFalse(self.window._focused().enabled)

    def test_but_its_roll_cost_is_shown(self):
        self.assertIn("bps", str(self.row()[2]))

    def test_and_its_limit(self):
        self.assertEqual(str(self.row()[3]), "25 bps")

    def test_and_how_far_from_it_the_market_is(self):
        self.assertIn("bps", str(self.row()[4]))

    def test_it_still_says_it_will_not_trade(self):
        self.assertEqual(str(self.row()[6]), "disabled")

    def test_it_is_never_the_section_that_would_roll_next(self):
        self.assertNotEqual(self.engine.snapshot().next_key, "1769>1284")


class TestTheRefusalToEnableIsReadable(WorkflowCase):
    """Reported as: "why am I getting this, or is this an issue?"

    It is not an issue: a section with no limits has no cap, and two sections
    sell the same September out of one position, so the one without limits
    could roll all of it. The refusal is right. How it was shown was not.
    """

    def setUp(self):
        super().setUp()
        self.set_limit("30", "10000")
        self.add_section("1769", "1284")        # no limits, switched off
        self.window._toggle_section()           # try to enable it
        self.tick()

    def note(self):
        return self.window.section_note.cget("text")

    def test_it_is_still_refused(self):
        self.assertFalse(self.cfg.sections[1].get("enabled", True))

    def test_it_does_not_name_a_config_key(self):
        self.assertNotIn("limit_ladder", self.note())

    def test_it_names_the_section(self):
        self.assertIn("Sep into Oct", self.note())

    def test_it_says_why(self):
        self.assertIn("same near month", self.note())

    def test_and_what_to_do_next(self):
        self.assertIn("Set limits", self.note())

    def test_giving_it_limits_clears_the_refusal(self):
        self.set_limit("25", "5000")
        self.window._toggle_section()
        self.tick()
        self.assertTrue(self.cfg.sections[1].get("enabled"))

    def test_the_refusal_does_not_follow_you_to_another_section(self):
        """It stayed up next to a different section's name, reading as a
        complaint about that one."""
        self.assertTrue(self.note())
        for item in self.window.strip.get_children():
            if self.window._strip_keys[item] == "1769>1584":
                self.window.strip.selection_set(item)
                self.window._focus_selected()
                break
        self.assertEqual(self.note(), "")

    def test_the_whole_sentence_fits(self):
        """It was cut off at "there is no cap on what it woul"."""
        self.assertGreater(self.window.section_note.cget("wraplength"), 1000)
        self.assertTrue(self.note().rstrip().endswith("."))


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

    def test_a_measurement_says_what_was_measured(self):
        """"0.1250 (max 0.0500)" alone names nothing. It is a leg spread."""
        from rollover.ui import _why_waiting

        class Gate:
            name = "far leg spread"
            detail = "0.1250 (max 0.0500)"
        self.assertEqual(_why_waiting(Gate()),
                         "far leg spread: 0.1250 (max 0.0500)")

    def test_a_sentence_is_left_to_stand_on_its_own(self):
        from rollover.ui import _why_waiting

        class Gate:
            name = "market open"
            detail = "closed for this segment"
        self.assertEqual(_why_waiting(Gate()), "closed for this segment")

    def test_a_gate_with_no_detail_falls_back_to_its_name(self):
        from rollover.ui import _why_waiting

        class Gate:
            name = "position"
            detail = ""
        self.assertEqual(_why_waiting(Gate()), "position")

    def test_the_column_is_labelled_as_a_reason(self):
        headings = [self.window.strip.heading(c)["text"]
                    for c in self.window.strip.cget("columns")]
        self.assertIn("Waiting for", headings)


if __name__ == "__main__":
    unittest.main(verbosity=2)
