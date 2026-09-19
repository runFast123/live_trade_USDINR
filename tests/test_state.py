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


class TestPerSectionState(StateCase):
    """Each section keeps its own count, its own progress and its own halt."""

    def test_a_section_is_created_on_first_use(self):
        state = self.store.load(self.today)
        section = state.section("1769>1584")
        self.assertEqual(section.clips_done, 0)
        self.assertIs(state.section("1769>1584"), section)

    def test_sections_survive_a_restart(self):
        state = self.store.load(self.today)
        state.section("1769>1500").clips_done = 2
        state.section("1769>1584").ladder_done = {"50": 3000}
        self.store.save(state)

        again = StateStore(self.dir).load(self.today)
        self.assertEqual(again.section("1769>1500").clips_done, 2)
        self.assertEqual(again.section("1769>1584").ladder_done, {"50": 3000})

    def test_one_section_halting_does_not_halt_another(self):
        state = self.store.load(self.today)
        state.section("a").halted_reason = "HALF ROLLED"
        state.section("b").clips_done = 1
        self.store.save(state)

        again = StateStore(self.dir).load(self.today)
        self.assertTrue(again.section("a").halted)
        self.assertFalse(again.section("b").halted)

    def test_daily_counts_reset_per_section(self):
        state = self.store.load(self.today)
        state.section("a").clips_done = 2
        state.section("a").lots_rolled = 2000
        self.store.save(state)

        tomorrow = self.store.load(self.today + timedelta(days=1))
        self.assertEqual(tomorrow.section("a").clips_done, 0)
        self.assertEqual(tomorrow.section("a").lots_rolled, 0)

    def test_a_halt_and_ladder_progress_do_not_reset_overnight(self):
        state = self.store.load(self.today)
        state.section("a").halted_reason = "HALF ROLLED, short 600"
        state.section("a").ladder_done = {"30": 4000}
        self.store.save(state)

        tomorrow = self.store.load(self.today + timedelta(days=1))
        self.assertTrue(tomorrow.section("a").halted)
        self.assertEqual(tomorrow.section("a").ladder_done, {"30": 4000})

    def test_an_unrecognised_section_field_does_not_brick_it(self):
        self.store.save(DayState(trading_date="2026-09-18"))
        with open(self.store.path, encoding="utf-8") as fh:
            body = json.load(fh)
        body["sections"] = {"a": {"clips_done": 2, "something_new": 9}}
        with open(self.store.path, "w", encoding="utf-8") as fh:
            json.dump(body, fh)

        again = StateStore(self.dir).load(self.today)
        self.assertEqual(again.section("a").clips_done, 2)

    def test_a_section_entry_that_is_not_an_object_is_skipped(self):
        self.store.save(DayState(trading_date="2026-09-18"))
        with open(self.store.path, encoding="utf-8") as fh:
            body = json.load(fh)
        body["sections"] = {"a": "nonsense", "b": {"clips_done": 1}}
        with open(self.store.path, "w", encoding="utf-8") as fh:
            json.dump(body, fh)

        again = StateStore(self.dir).load(self.today)
        self.assertNotIn("a", again.sections)
        self.assertEqual(again.section("b").clips_done, 1)


class TestUpgradingFromAFileWithNoSections(StateCase):
    """A version 1 file records one roll in flat fields.

    Its ladder_campaign is already the pair of contracts, the same near>far a
    section key uses, so the migration is to move the flat figures under that
    key. Without it, upgrading would present a part-finished campaign as
    untouched and roll the whole thing again.
    """

    def write_v1(self, **overrides):
        body = {"version": 1, "trading_date": "2026-09-18", "clips_done": 3,
                "lots_rolled": 3000, "ladder_campaign": "1769>1584",
                "ladder_done": {"50": 3000}, "halted_reason": None}
        body.update(overrides)
        with open(self.store.path, "w", encoding="utf-8") as fh:
            json.dump(body, fh)

    def test_the_old_roll_becomes_a_section(self):
        self.write_v1()
        state = self.store.load(self.today)
        self.assertIn("1769>1584", state.sections)

    def test_its_progress_comes_with_it(self):
        self.write_v1()
        section = self.store.load(self.today).section("1769>1584")
        self.assertEqual(section.clips_done, 3)
        self.assertEqual(section.lots_rolled, 3000)
        self.assertEqual(section.ladder_done, {"50": 3000})

    def test_a_halt_comes_with_it(self):
        self.write_v1(halted_reason="HALF ROLLED")
        self.assertTrue(self.store.load(self.today).section("1769>1584").halted)

    def test_an_untouched_old_file_creates_nothing(self):
        self.write_v1(clips_done=0, lots_rolled=0, ladder_done={})
        self.assertEqual(self.store.load(self.today).sections, {})

    def test_an_old_file_with_no_campaign_creates_nothing(self):
        """Without a pair of contracts there is no key to file it under."""
        self.write_v1(ladder_campaign="")
        self.assertEqual(self.store.load(self.today).sections, {})

    def test_saving_upgrades_the_file(self):
        self.write_v1()
        self.store.save(self.store.load(self.today))
        with open(self.store.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["version"], 2)


class TestADowngradeCannotRollTwice(StateCase):
    """An older build reads only the flat fields.

    It must not conclude that nothing has been rolled and nothing is wrong, so
    the flat fields carry the account's totals, chosen conservatively.
    """

    def flat(self, state):
        self.store.save(state)
        with open(self.store.path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_any_section_halted_halts_the_file(self):
        state = self.store.load(self.today)
        state.section("a").name = "Sep into Oct"
        state.section("a").halted_reason = "HALF ROLLED, short 600"
        state.section("b").clips_done = 1

        body = self.flat(state)
        self.assertTrue(body["halted_reason"])
        self.assertIn("Sep into Oct", body["halted_reason"])
        self.assertIn("short 600", body["halted_reason"])

    def test_every_halted_section_is_named(self):
        state = self.store.load(self.today)
        state.section("a").name = "A"
        state.section("a").halted_reason = "one"
        state.section("b").name = "B"
        state.section("b").halted_reason = "two"

        body = self.flat(state)
        self.assertIn("A: one", body["halted_reason"])
        self.assertIn("B: two", body["halted_reason"])

    def test_the_flat_clip_count_is_the_largest_not_the_sum(self):
        """Under-trading on a downgrade is recoverable; over-trading is not."""
        state = self.store.load(self.today)
        state.section("a").clips_done = 3
        state.section("b").clips_done = 1
        self.assertEqual(self.flat(state)["clips_done"], 3)

    def test_the_flat_lots_are_the_sum(self):
        state = self.store.load(self.today)
        state.section("a").lots_rolled = 2000
        state.section("b").lots_rolled = 3000
        self.assertEqual(self.flat(state)["lots_rolled"], 5000)

    def test_no_sections_leaves_the_flat_fields_alone(self):
        state = self.store.load(self.today)
        state.clips_done = 2
        state.halted_reason = "something"
        body = self.flat(state)
        self.assertEqual(body["clips_done"], 2)
        self.assertEqual(body["halted_reason"], "something")


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
