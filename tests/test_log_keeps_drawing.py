"""The log pane and the alarm stopped working 300 lines into a session.

_draw_log returned early when len(recent(300)) matched the last count. The
buffer keeps 500 and recent() is capped at 300, so from the 300th line of a
run that length never changed again: the pane stopped redrawing, the alert
counter stopped advancing, and notify.alarm() never fired for the rest of the
session.

Sound is the only channel this app has to somebody who looks at the screen
every 15-30 minutes, and it died on exactly the days that produce the most
log lines. Reproduced before the fix: a second halt past line 300 redrew
nothing and made no sound.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest

from rollover.logbook import Logbook


class Screen:
    """The decision _draw_log makes, with the Tk stripped out."""

    def __init__(self, log):
        self.log = log
        self.seq = -1
        self.alerts_seen = 0
        self.redraws = 0
        self.sounds = []

    def draw(self):
        seq = self.log.sequence
        if seq == self.seq:
            return False
        self.seq = seq
        alerts = self.log.alert_sequence
        if alerts > self.alerts_seen:
            newest = self.log.last_alert
            self.sounds.append("chime" if "come below the limit" in newest
                               else "alarm")
        self.alerts_seen = alerts
        self.redraws += 1
        return True


class LogCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="logdraw_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.log = Logbook(self.dir)
        self.screen = Screen(self.log)

    def fill(self, count):
        for i in range(count):
            self.log.info(f"line {i}")
            self.screen.draw()


class TestTheCountersOnlyGoUp(LogCase):
    def test_the_sequence_counts_everything_ever_written(self):
        self.fill(700)
        self.assertEqual(self.log.sequence, 700)

    def test_even_though_the_buffer_holds_fewer(self):
        self.fill(700)
        self.assertLess(len(self.log.recent(300)), self.log.sequence)

    def test_alerts_are_counted_separately_and_also_only_go_up(self):
        self.log.alert("one")
        self.fill(400)
        self.log.alert("two")
        self.assertEqual(self.log.alert_sequence, 2)

    def test_the_last_alert_survives_scrolling_out_of_the_buffer(self):
        """Which is exactly when the sound matters most."""
        self.log.alert("HALTED: HALF ROLLED, short 4000")
        self.fill(700)
        self.assertNotIn("HALTED: HALF ROLLED, short 4000",
                         [m for _, _, m in self.log.recent(300)])
        self.assertIn("short 4000", self.log.last_alert)


class TestItKeepsDrawing(LogCase):
    def test_past_three_hundred_lines(self):
        self.fill(400)
        before = self.screen.redraws
        self.log.info("one more")
        self.assertTrue(self.screen.draw())
        self.assertEqual(self.screen.redraws, before + 1)

    def test_it_still_does_not_redraw_when_nothing_happened(self):
        """The early return exists for a reason; only its test was wrong."""
        self.fill(50)
        self.assertFalse(self.screen.draw())

    def test_a_halt_past_three_hundred_lines_still_sounds(self):
        self.fill(400)
        self.log.alert("HALTED: HALF ROLLED, short 4000 units")
        self.screen.draw()
        self.assertEqual(self.screen.sounds, ["alarm"])

    def test_a_second_halt_much_later_sounds_again(self):
        self.log.alert("HALTED: first")
        self.screen.draw()
        self.fill(900)
        self.log.alert("HALTED: second")
        self.screen.draw()
        self.assertEqual(self.screen.sounds, ["alarm", "alarm"])

    def test_the_cost_clearing_chimes_rather_than_alarms(self):
        self.fill(400)
        self.log.alert("the roll cost has come below the limit")
        self.screen.draw()
        self.assertEqual(self.screen.sounds, ["chime"])

    def test_a_thousand_lines_never_stop_it(self):
        for i in range(1000):
            self.log.info(f"line {i}")
            self.assertTrue(self.screen.draw(), f"stopped drawing at {i}")


class TestTheWindowUsesTheCounters(unittest.TestCase):
    def test_draw_log_compares_the_sequence_not_the_length(self):
        import inspect

        from rollover.ui import RollWindow
        source = inspect.getsource(RollWindow._draw_log)
        self.assertIn("self.log.sequence", source)
        self.assertIn("self.log.alert_sequence", source)
        self.assertNotIn("len(entries) ==", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
