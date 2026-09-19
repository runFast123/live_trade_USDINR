"""The guide on screen, and whether what it claims is still true.

A help text is a second description of the system, and a second description
drifts. These tests pin the claims that would actually mislead someone if the
code moved underneath them -- the one-order-at-a-time rule, the expiry-day
reversal, what a blank quantity means -- against the code that implements
them, so changing the behaviour without changing the words turns something
red.
"""
from __future__ import annotations

import unittest

from rollover import help as helptext

try:
    import tkinter as tk
    _root = tk.Tk()
    _root.withdraw()
    HAVE_TK = True
except Exception:                       # pragma: no cover - headless
    _root = None
    HAVE_TK = False


class TestTheGuideItself(unittest.TestCase):
    def test_it_has_topics(self):
        self.assertGreaterEqual(len(helptext.TOPICS), 8)

    def test_every_topic_has_a_heading_and_something_under_it(self):
        for heading, paragraphs in helptext.TOPICS:
            self.assertTrue(heading.strip(), "a topic with no heading")
            self.assertTrue(paragraphs, f"{heading} has nothing under it")
            for paragraph in paragraphs:
                self.assertTrue(paragraph.strip())

    def test_headings_are_not_repeated(self):
        headings = [h for h, _ in helptext.TOPICS]
        self.assertEqual(len(headings), len(set(headings)))

    def test_it_answers_the_questions_that_were_actually_asked(self):
        """Each of these came from someone using it, not from a guess."""
        whole = helptext.as_text().lower()
        for asked in ("can two sections trade at the same time",
                      "two numbers both called a limit",
                      "waiting for",
                      "add section",
                      "watch line"):
            self.assertIn(asked, whole, f"nothing about {asked!r}")

    def test_as_text_is_plain_and_complete(self):
        text = helptext.as_text()
        for heading, paragraphs in helptext.TOPICS:
            self.assertIn(heading, text)
            for paragraph in paragraphs:
                self.assertIn(paragraph, text)

    def test_it_does_not_speak_in_config_keys(self):
        """One exception: the account cap has no other name on screen."""
        whole = helptext.as_text()
        for key in ("limit_ladder", "watch_limits", "dry_run", "near_token",
                    "max_clips_per_day\n", "section_specs"):
            self.assertNotIn(key, whole, f"{key} is a file word")


class TestItStillDescribesTheRealSystem(unittest.TestCase):
    """The claims that would mislead someone if the code moved."""

    def text(self):
        return helptext.as_text().lower()

    def test_one_order_at_a_time_is_still_account_wide(self):
        """The guide says other sections stand down while one works."""
        from rollover.sections import AccountState, Section

        self.assertIn("only one order is ever live on the account",
                      self.text())
        self.assertIn("in_flight", AccountState.__dataclass_fields__)
        self.assertIsInstance(Section.in_flight, property)

    def test_furthest_inside_its_own_limit_still_wins(self):
        from rollover import book
        from rollover.money import D

        self.assertIn("furthest inside its own limit", self.text())

        def cand(name, cost, limit):
            return book.Candidate(name=name, cost_bps=D(cost),
                                  limit_bps=D(limit), qualifies=True,
                                  payload=name, outstanding=1000)
        # b is cheaper; a is further inside its own limit.
        self.assertEqual(
            book.pick([cand("a", "20", "30"), cand("b", "5", "10")]).name, "a")

    def test_expiry_day_still_reverses_it(self):
        from rollover import book
        from rollover.money import D

        self.assertIn("most left to roll goes first", self.text())

        def cand(name, cost, limit, outstanding):
            return book.Candidate(name=name, cost_bps=D(cost),
                                  limit_bps=D(limit), qualifies=True,
                                  payload=name, outstanding=outstanding)
        cheap = cand("cheap", "5", "10", 1000)
        big = cand("big", "20", "30", 9000)
        self.assertEqual(book.pick([cheap, big], expiry_day=True).name, "big")

    def test_a_new_section_still_arrives_switched_off(self):
        from rollover import editing
        from rollover.config import RollConfig

        self.assertIn("arrives switched off", self.text())
        cfg = RollConfig(near_token="1769", near_expiry="2026-09-28",
                         far_token="1584", far_expiry="2026-11-26",
                         limit_ladder=[{"bps": "50", "qty": 20000}])
        got = editing.add(cfg, {"Token": "1769", "SecDesc": "SEP",
                                "Expiry": "2026-09-28"},
                          {"Token": "1500", "SecDesc": "OCT",
                           "Expiry": "2026-10-29"})
        self.assertFalse(got[1]["enabled"])

    def test_a_blank_quantity_is_still_a_watch_line(self):
        from rollover.ladder import parse_watch
        from rollover.money import D

        self.assertIn("never traded", self.text())
        self.assertEqual(parse_watch(["25", "40"]), [D("25"), D("40")])

    def test_arming_still_lasts_one_clip(self):
        import inspect

        from rollover.engine import RollEngine

        self.assertIn("after a roll fires it disarms", self.text())
        self.assertIn('self.disarm("condition met, firing")',
                      inspect.getsource(RollEngine._tick))

    def test_a_halt_is_still_account_wide(self):
        import inspect

        from rollover.engine import RollEngine

        self.assertIn("halted stops everything, not one section", self.text())
        self.assertIn("self.account.halted_reason",
                      inspect.getsource(RollEngine.halt))


@unittest.skipUnless(HAVE_TK, "no display")
class TestTheHelpWindow(unittest.TestCase):
    def setUp(self):
        import os
        import shutil
        import tempfile
        from datetime import date

        from rollover.config import RollConfig
        from rollover.engine import RollEngine
        from rollover.ui import RollWindow

        class Log:
            def info(self, m): pass
            warn = error = alert = info
            def tail(self, n=200): return []
            def recent(self, n=300): return []

        class Broker:
            logged_in = True
            scrip_file_date = date.today()

        self.dir = tempfile.mkdtemp(prefix="help_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        path = os.path.join(self.dir, "config.json")
        cfg = RollConfig(near_token="1769", near_expiry="2026-09-28",
                         far_token="1584", far_expiry="2026-11-26",
                         use_live_feed=False, record_market=False,
                         update_check=False, journal=False, dry_run=True)
        cfg.save(path)
        engine = RollEngine(cfg, Log(), self.dir, broker=Broker())
        self.window = RollWindow(_root, engine, Log(), config_path=path)
        self.window.withdraw()
        self.addCleanup(self.window.destroy)

    def body(self, widget):
        """Every Text widget's contents, as a list of strings.

        A list, and the caller joins it. Returning a string and writing
        `out += self.body(child)` extends the list one character at a
        time -- which still prints correctly and matches nothing at all.
        """
        out = []
        for child in widget.winfo_children():
            if isinstance(child, tk.Text):
                out.append(child.get("1.0", "end"))
            out.extend(self.body(child))
        return out

    def test_it_opens(self):
        self.window._show_help()
        _root.update_idletasks()
        self.assertTrue(self.window._help_window.winfo_exists())

    def test_it_carries_the_whole_guide(self):
        self.window._show_help()
        _root.update_idletasks()
        text = chr(10).join(self.body(self.window._help_window))
        for heading, _ in helptext.TOPICS:
            self.assertIn(heading, text)

    def test_pressing_it_twice_does_not_open_a_second(self):
        self.window._show_help()
        first = self.window._help_window
        self.window._show_help()
        self.assertIs(self.window._help_window, first)

    def test_it_can_be_closed_and_opened_again(self):
        self.window._show_help()
        self.window._help_window.destroy()
        self.window._help_window = None
        self.window._show_help()
        _root.update_idletasks()
        self.assertTrue(self.window._help_window.winfo_exists())

    def test_it_is_read_only(self):
        self.window._show_help()
        _root.update_idletasks()

        def texts(widget):
            found = []
            for child in widget.winfo_children():
                if isinstance(child, tk.Text):
                    found.append(child)
                found += texts(child)
            return found
        for box in texts(self.window._help_window):
            self.assertEqual(str(box.cget("state")), "disabled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
