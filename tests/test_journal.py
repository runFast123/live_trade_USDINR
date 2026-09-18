"""The record that stands up afterwards.

The log is prose for someone watching the screen. It cannot answer "what limit
was in force when that order went out?" or "what did the broker actually say?"
Both answers existed and neither was ever written down.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal

from rollover.journal import Journal, _plain, read
from rollover.money import D
from tests.test_execution import ExecutionCase, outcome


class JournalCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="journal_")
        self.journal = Journal(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def entries(self):
        return read(self.journal.path_for())


class TestWriting(JournalCase):
    def test_an_entry_can_be_read_back(self):
        self.assertTrue(self.journal.write("test", who="me", n=3))
        entries = self.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "test")
        self.assertEqual(entries[0]["who"], "me")
        self.assertEqual(entries[0]["n"], 3)

    def test_every_entry_is_stamped(self):
        self.journal.write("test")
        stamp = self.entries()[0]["at"]
        parsed = datetime.fromisoformat(stamp)
        self.assertIsNotNone(parsed.tzinfo, "no timezone, so the time is ambiguous")
        self.assertIn(".", stamp, "no milliseconds")

    def test_entries_append_rather_than_replace(self):
        for i in range(5):
            self.journal.write("test", i=i)
        self.assertEqual([e["i"] for e in self.entries()], [0, 1, 2, 3, 4])

    def test_one_object_per_line(self):
        self.journal.write("a")
        self.journal.write("b")
        with open(self.journal.path_for(), encoding="utf-8") as fh:
            lines = [l for l in fh.read().splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line)          # each line stands alone

    def test_the_file_is_named_for_the_day(self):
        self.assertIn(date.today().isoformat(), self.journal.path_for())

    def test_disabled_writes_nothing(self):
        quiet = Journal(self.dir, enabled=False)
        self.assertFalse(quiet.write("test"))
        self.assertFalse(os.path.exists(quiet.path_for()))


class TestItNeverInterferes(JournalCase):
    """A journal that could stop a roll would be a liability, not a record."""

    def test_an_unwritable_directory_does_not_raise(self):
        broken = Journal(os.path.join(self.dir, "a\x00b"))
        self.assertFalse(broken.write("test"))
        self.assertTrue(broken.failed)

    def test_an_unserialisable_value_does_not_raise(self):
        class Awkward:
            def __repr__(self):
                raise RuntimeError("not even repr works")

        self.assertIn(self.journal.write("test", thing=Awkward()), (True, False))

    def test_a_partial_final_line_does_not_spoil_the_rest(self):
        """A crash mid-write must cost one line, not the whole record."""
        self.journal.write("good", n=1)
        self.journal.write("good", n=2)
        with open(self.journal.path_for(), "a", encoding="utf-8") as fh:
            fh.write('{"kind": "truncated", "at":')

        entries = self.entries()
        self.assertEqual([e["n"] for e in entries], [1, 2])

    def test_reading_a_missing_file_is_empty_not_an_error(self):
        self.assertEqual(read(os.path.join(self.dir, "nothing.jsonl")), [])


class TestExactNumbers(unittest.TestCase):
    """Prices are exact everywhere else and must not become floats here."""

    def test_a_decimal_is_written_as_its_own_text(self):
        self.assertEqual(_plain(D("0.2873")), "0.2873")

    def test_it_survives_a_round_trip(self):
        written = json.loads(json.dumps({"limit": _plain(D("0.2873"))}))
        self.assertEqual(Decimal(written["limit"]), D("0.2873"))

    def test_trailing_zeros_are_kept(self):
        """0.3000 and 0.3 are the same number but not the same statement."""
        self.assertEqual(_plain(D("0.3000")), "0.3000")

    def test_nested_decimals_are_converted(self):
        got = _plain({"a": [D("1.5"), {"b": D("2.5")}]})
        self.assertEqual(got, {"a": ["1.5", {"b": "2.5"}]})

    def test_dates_and_times_become_iso(self):
        self.assertEqual(_plain(date(2026, 9, 18)), "2026-09-18")

    def test_plain_values_are_left_alone(self):
        for value in (None, True, 3, "text"):
            self.assertEqual(_plain(value), value)

    def test_anything_else_is_stringified_rather_than_dropped(self):
        class Thing:
            def __repr__(self): return "<a thing>"
        self.assertEqual(_plain(Thing()), "<a thing>")


class TestWhatARollLeavesBehind(ExecutionCase):
    """The questions the log could not answer, asked of the journal."""

    def journal_of(self, engine):
        return read(engine.journal.path_for())

    def kinds(self, entries):
        return [e["kind"] for e in entries]

    def one(self, entries, kind):
        matching = [e for e in entries if e["kind"] == kind]
        self.assertTrue(matching, f"no {kind} entry")
        return matching[0]

    def test_a_completed_roll_records_every_stage(self):
        engine = self.build([outcome(1000, 1000), outcome(1000, 1000)])
        engine._execute(self.decision, self.near_q, self.far_q)

        kinds = self.kinds(self.journal_of(engine))
        for expected in ("decision", "margin", "order", "reconcile", "complete"):
            self.assertIn(expected, kinds)

    def test_the_limit_in_force_is_recorded_with_the_decision(self):
        engine = self.build([outcome(1000, 1000), outcome(1000, 1000)])
        engine._execute(self.decision, self.near_q, self.far_q)

        entry = self.one(self.journal_of(engine), "decision")
        self.assertEqual(entry["limit"], str(self.decision.limit))
        self.assertEqual(entry["roll_cost"], str(self.decision.roll_cost))
        self.assertEqual(entry["qty"], self.decision.qty)
        self.assertTrue(entry["qualifies"])

    def test_both_legs_are_recorded_with_their_reference(self):
        engine = self.build([outcome(1000, 1000), outcome(1000, 1000)])
        engine._execute(self.decision, self.near_q, self.far_q)

        orders = [e for e in self.journal_of(engine) if e["kind"] == "order"]
        self.assertEqual([o["leg"] for o in orders], ["near", "far"])
        self.assertEqual([o["side"] for o in orders], ["SELL", "BUY"])
        self.assertEqual(orders[0]["order_ref"], "X1")
        self.assertEqual(orders[0]["filled_qty"], 1000)

    def test_a_halt_is_recorded_with_its_reason(self):
        engine = self.build([outcome(1000, 1000), outcome(0, 1000)])
        engine._execute(self.decision, self.near_q, self.far_q)

        entry = self.one(self.journal_of(engine), "halt")
        self.assertIn("HALF ROLLED", entry["reason"])

    def test_a_refused_margin_is_recorded_before_anything_is_sent(self):
        engine = self.build([outcome(1000, 1000)], margin=900000, funds=50000)
        engine._execute(self.decision, self.near_q, self.far_q)

        entry = self.one(self.journal_of(engine), "margin")
        self.assertFalse(entry["affordable"])
        self.assertEqual(entry["required"], "900000")
        self.assertNotIn("order", self.kinds(self.journal_of(engine)))

    def test_the_reconciliation_verdict_is_recorded(self):
        engine = self.build([outcome(1000, 1000), outcome(1000, 1000)],
                            positions=[{"1769": 1000, "1584": 1000}])
        engine._execute(self.decision, self.near_q, self.far_q)

        entry = self.one(self.journal_of(engine), "reconcile")
        self.assertFalse(entry["agreed"])
        self.assertTrue(entry["checked"])
        self.assertEqual(entry["before"], {"near": 1000, "far": 1000})

    def test_a_dry_run_says_so(self):
        engine = self.build([outcome(1000, 1000)], dry_run=True)
        engine._execute(self.decision, self.near_q, self.far_q)
        self.assertTrue(self.one(self.journal_of(engine), "decision")["dry_run"])

    def test_it_can_be_switched_off(self):
        engine = self.build([outcome(1000, 1000), outcome(1000, 1000)],
                            journal=False)
        engine._execute(self.decision, self.near_q, self.far_q)
        self.assertEqual(self.journal_of(engine), [])


class TestItIsOnByDefault(unittest.TestCase):
    def test_the_default_config_keeps_a_journal(self):
        from rollover.config import RollConfig
        self.assertTrue(RollConfig().journal)


if __name__ == "__main__":
    unittest.main(verbosity=2)
