"""Tests for the state that has to survive the process.

Two failures motivate this file, and both are pinned here:

  * close the app after a completed roll, reopen it, and the clip counter was
    zero again, so one press of ARM rolled a second time
  * halt half way through a roll, close and reopen, and the halt was gone
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import date, timedelta

from rollover.state import DayState, StateStore


class StateCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="state_")
        self.store = StateStore(self.dir)
        self.today = date(2026, 9, 18)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class TestFirstRun(StateCase):
    def test_no_file_means_a_clean_start(self):
        state = self.store.load(self.today)
        self.assertEqual(state.clips_done, 0)
        self.assertIsNone(state.halted_reason)
        self.assertEqual(state.trading_date, "2026-09-18")

    def test_a_clean_start_is_not_a_halt(self):
        """Absence means first run. It must not be treated as suspicious."""
        self.assertFalse(self.store.load(self.today).halted)


class TestItSurvivesARestart(StateCase):
    def test_the_clip_count_comes_back(self):
        self.store.save(DayState(trading_date="2026-09-18", clips_done=1,
                                 lots_rolled=25))
        again = StateStore(self.dir).load(self.today)
        self.assertEqual(again.clips_done, 1)
        self.assertEqual(again.lots_rolled, 25)

    def test_a_halt_comes_back(self):
        self.store.save(DayState(
            trading_date="2026-09-18",
            halted_reason="HALF ROLLED. You are short 600 units."))
        again = StateStore(self.dir).load(self.today)
        self.assertTrue(again.halted)
        self.assertIn("short 600", again.halted_reason)


class TestTheDayRollingOver(StateCase):
    def test_the_budget_resets_on_a_new_day(self):
        self.store.save(DayState(trading_date="2026-09-17", clips_done=1,
                                 lots_rolled=25))
        state = self.store.load(self.today)
        self.assertEqual(state.clips_done, 0)
        self.assertEqual(state.lots_rolled, 0)
        self.assertEqual(state.trading_date, "2026-09-18")

    def test_a_halt_does_NOT_expire_overnight(self):
        """A half-rolled position does not repair itself while you sleep."""
        self.store.save(DayState(trading_date="2026-09-17",
                                 halted_reason="HALF ROLLED, short 600"))
        state = self.store.load(self.today)
        self.assertTrue(state.halted)
        self.assertIn("short 600", state.halted_reason)

    def test_a_halt_survives_several_days(self):
        self.store.save(DayState(trading_date="2026-09-10",
                                 halted_reason="unknown fill"))
        later = self.store.load(self.today + timedelta(days=30))
        self.assertTrue(later.halted)

    def test_working_orders_are_not_carried_into_a_new_day(self):
        """Day orders die at the close, so yesterday's are not still live."""
        self.store.save(DayState(trading_date="2026-09-17",
                                 working_orders=[{"ref": "A1"}]))
        self.assertEqual(self.store.load(self.today).working_orders, [])


class TestAnUnreadableFile(StateCase):
    def write(self, text):
        with open(self.store.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_corrupt_json_halts_rather_than_guessing(self):
        """Present but unreadable is not the same as absent.

        We cannot tell whether today's roll already happened, so the honest
        answer is to stop and have somebody look.
        """
        self.write("{ this is not json")
        state = self.store.load(self.today)
        self.assertTrue(state.halted)
        self.assertIn("could not be read", state.halted_reason)

    def test_the_wrong_shape_halts(self):
        self.write('["not", "an", "object"]')
        self.assertTrue(self.store.load(self.today).halted)

    def test_an_empty_file_halts(self):
        self.write("")
        self.assertTrue(self.store.load(self.today).halted)

    def test_unknown_keys_are_ignored_rather_than_fatal(self):
        """A newer build's extra fields must not brick an older one."""
        self.write(json.dumps({"trading_date": "2026-09-18", "clips_done": 2,
                               "something_new": 42}))
        state = self.store.load(self.today)
        self.assertFalse(state.halted)
        self.assertEqual(state.clips_done, 2)


class TestWriting(StateCase):
    def test_a_save_can_be_read_back(self):
        self.assertTrue(self.store.save(DayState(trading_date="2026-09-18",
                                                 clips_done=3)))
        self.assertEqual(self.store.load(self.today).clips_done, 3)

    def test_the_write_is_atomic(self):
        """A crash mid-write must not leave a half-written file behind."""
        self.store.save(DayState(trading_date="2026-09-18", clips_done=1))
        leftovers = [n for n in os.listdir(self.dir) if n.startswith(".state-")]
        self.assertEqual(leftovers, [])

    def test_saving_over_an_existing_file_replaces_it(self):
        self.store.save(DayState(trading_date="2026-09-18", clips_done=1))
        self.store.save(DayState(trading_date="2026-09-18", clips_done=2))
        self.assertEqual(self.store.load(self.today).clips_done, 2)

    def test_a_failed_save_reports_rather_than_raises(self):
        store = StateStore(os.path.join(self.dir, "nested"))
        shutil.rmtree(self.dir, ignore_errors=True)
        # The parent is gone; makedirs may succeed or not, but it must not raise.
        result = store.save(DayState(trading_date="2026-09-18"))
        self.assertIn(result, (True, False))


class TestThroughTheEngine(unittest.TestCase):
    """The engine must actually use it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="engstate_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def engine(self):
        from rollover.config import RollConfig
        from rollover.engine import RollEngine

        class Log:
            def info(self, m): pass
            def warn(self, m): pass
            def error(self, m): pass
            def alert(self, m): pass

        cfg = RollConfig(near_token="1769", far_token="1584",
                         use_live_feed=False, record_market=False,
                         update_check=False)

        class Broker:
            logged_in = True
            scrip_file_date = date.today()

        return RollEngine(cfg, Log(), self.dir, broker=Broker())

    def test_a_halt_is_written_and_reloaded(self):
        first = self.engine()
        first.halt("HALF ROLLED, short 600 units")

        second = self.engine()
        self.assertEqual(second.session.halted_reason,
                         "HALF ROLLED, short 600 units")

    def test_clearing_a_halt_is_written(self):
        first = self.engine()
        first.halt("something")
        first.start = lambda: None          # do not spin up the watch loop
        first.clear_halt()

        second = self.engine()
        self.assertIsNone(second.session.halted_reason)

    def test_a_completed_clip_is_written_and_reloaded(self):
        first = self.engine()
        first._count_clip(25)
        self.assertEqual(first.session.clips_done_today, 1)
        self.assertEqual(first.session.lots_rolled, 25)

        second = self.engine()
        self.assertEqual(second.session.clips_done_today, 1)
        self.assertEqual(second.session.lots_rolled, 25)

    def test_changing_contracts_cannot_wipe_a_halt(self):
        engine = self.engine()
        engine.halt("HALF ROLLED")
        started = []
        engine.start = lambda: started.append(1)

        engine.restart()
        self.assertEqual(started, [], "restart ran despite the halt")
        self.assertEqual(engine.session.halted_reason, "HALF ROLLED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
