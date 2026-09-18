"""Tests for the market recorder.

The recorder's job is to answer one question well: **what limit would actually
have worked today?** These tests pin that, and pin that it can never disturb
trading.
"""
from __future__ import annotations

import csv
import os
import shutil
import tempfile
import time
import unittest

from rollover import rule
from rollover.config import RollConfig
from rollover.money import D
from rollover.quotes import Quote
from rollover.recorder import COLUMNS, Recorder


def quote(token, bid, ask, bid_qty=300, ask_qty=300):
    return Quote(token, D(bid), D(ask), time.monotonic(), D("1"),
                 bid_qty=bid_qty, ask_qty=ask_qty)


def decision_at(far_ask, cfg=None, days=30):
    """A decision whose cost is driven by the far ask."""
    cfg = cfg or RollConfig()
    near = quote("1769", "95.9675", "95.9700")
    far = quote("1284", str(D(far_ask) - D("0.0025")), far_ask)
    return rule.compute(near, far, cfg, days=days), near, far


class RecorderCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rec_")
        self.rec = Recorder(self.dir, interval_sec=0.0)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def rows(self):
        with open(self.rec.path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))


class TestWriting(RecorderCase):
    def test_a_row_is_written_with_the_expected_columns(self):
        d, near, far = decision_at("96.2800")
        self.assertTrue(self.rec.sample(near, far, d, None, "live feed"))

        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(list(rows[0]), list(COLUMNS))

    def test_the_row_carries_the_numbers_that_matter(self):
        d, near, far = decision_at("96.2800")
        self.rec.sample(near, far, d, None, "live feed")
        row = self.rows()[0]

        self.assertEqual(row["source"], "live feed")
        self.assertEqual(row["near_bid"], "95.9675")
        self.assertEqual(row["far_ask"], "96.2800")
        self.assertEqual(row["roll_cost"], "0.3125")
        self.assertEqual(row["cost_bps"], "32.6")
        self.assertEqual(row["limit_bps"], "30.0")
        self.assertEqual(row["tenor_days"], "30")
        self.assertEqual(row["qualifies"], "no")

    def test_depth_is_recorded_because_clip_sizing_will_depend_on_it(self):
        d, near, far = decision_at("96.2800")
        near.bid_qty, far.ask_qty = 338, 16
        self.rec.sample(near, far, d, None, "")
        row = self.rows()[0]
        self.assertEqual(row["near_bid_qty"], "338")
        self.assertEqual(row["far_ask_qty"], "16")

    def test_the_blocking_gates_are_named(self):
        class Gate:
            def __init__(self, name): self.name, self.ok = name, False

        class Report:
            failures = [Gate("position"), Gate("far touch size")]

        d, near, far = decision_at("96.2800")
        self.rec.sample(near, far, d, Report(), "")
        self.assertEqual(self.rows()[0]["blocking"], "position; far touch size")

    def test_the_interval_is_respected(self):
        rec = Recorder(self.dir, interval_sec=60)
        d, near, far = decision_at("96.2800")
        self.assertTrue(rec.sample(near, far, d, None, ""))
        self.assertFalse(rec.sample(near, far, d, None, ""))

    def test_the_header_is_written_once(self):
        d, near, far = decision_at("96.2800")
        for _ in range(3):
            self.rec.sample(near, far, d, None, "")
        with open(self.rec.path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertEqual(body.count("timestamp,source"), 1)


class TestItCannotBreakTrading(RecorderCase):
    def test_a_broken_quote_does_not_raise(self):
        class Exploding:
            def __getattr__(self, name):
                raise RuntimeError("boom")

        self.assertFalse(self.rec.sample(Exploding(), Exploding(), None, None, ""))

    def test_an_unwritable_directory_does_not_raise(self):
        self.rec.directory = os.path.join(self.dir, "gone")
        shutil.rmtree(self.dir, ignore_errors=True)
        d, near, far = decision_at("96.2800")
        self.assertFalse(self.rec.sample(near, far, d, None, ""))

    def test_no_decision_is_tolerated(self):
        _, near, far = decision_at("96.2800")
        self.assertTrue(self.rec.sample(near, far, None, None, ""))
        self.assertEqual(self.rows()[0]["cost_bps"], "")


class TestTheQuestionItExistsToAnswer(RecorderCase):
    """What limit would have worked, and for how long?"""

    def feed(self, bps_levels, seconds_each=10.0):
        """Push a sequence of costs, each held for a number of seconds."""
        for far_ask in bps_levels:
            d, near, far = decision_at(far_ask)
            self.rec._tally(d, seconds_each)
        return d

    def test_the_closest_approach_is_kept(self):
        for far_ask in ("96.2800", "96.2600", "96.2900"):
            d, near, far = decision_at(far_ask)
            self.rec._tally(d, 5.0)
        # 96.2600 is the cheapest of the three.
        self.assertEqual(self.rec.best_bps, D("30.5"))
        self.assertIsNotNone(self.rec.best_at)

    def test_time_within_a_few_bps_of_the_limit_is_counted(self):
        # 32.6 bps against a 30 bps limit is 2.6 bps over.
        d, near, far = decision_at("96.2800")
        self.rec._tally(d, 30.0)

        self.assertEqual(self.rec.seconds_within[0], 0.0)      # never inside
        self.assertEqual(self.rec.seconds_within[1], 0.0)
        self.assertEqual(self.rec.seconds_within[2], 0.0)
        self.assertEqual(self.rec.seconds_within[5], 30.0)     # within 5
        self.assertEqual(self.rec.seconds_within[10], 30.0)

    def test_time_below_the_limit_is_counted(self):
        d, near, far = decision_at("96.2400")                  # 28.4 bps
        self.rec._tally(d, 20.0)
        self.assertEqual(self.rec.seconds_within[0], 20.0)

    def test_the_implied_limit_is_the_level_that_would_have_worked(self):
        # Sit at 32.6 bps for two minutes.
        d, near, far = decision_at("96.2800")
        self.rec._tally(d, 120.0)

        self.assertEqual(self.rec.implied_limit(seconds_wanted=60), 33)
        self.assertIsNone(self.rec.implied_limit(seconds_wanted=600))

    def test_a_cheaper_moment_lowers_the_implied_limit(self):
        for far_ask, secs in (("96.2800", 120.0), ("96.2500", 90.0)):
            d, near, far = decision_at(far_ask)
            self.rec._tally(d, secs)
        # The cheaper level held long enough on its own.
        self.assertLessEqual(self.rec.implied_limit(seconds_wanted=60), 30)

    def test_the_summary_says_something_useful(self):
        d, near, far = decision_at("96.2800")
        self.rec._tally(d, 120.0)
        text = self.rec.summary(limit_bps=D("30"))

        self.assertIn("closest approach 32.6 bps", text)
        self.assertIn("within 5 bps", text)
        self.assertIn("33 bps would have been available", text)

    def test_the_summary_is_honest_when_nothing_was_seen(self):
        self.assertIn("No market samples", self.rec.summary())

    def test_a_long_gap_is_not_credited_as_time_in_the_market(self):
        """The app being closed overnight is not the price standing still."""
        rec = Recorder(self.dir, interval_sec=5.0)
        d, near, far = decision_at("96.2400")
        rec.sample(near, far, d, None, "")
        rec._last_write = 0.0                    # pretend the interval elapsed
        rec._last_sample_at = time.monotonic() - 3600
        rec.sample(near, far, d, None, "")
        self.assertLess(rec.seconds_observed, 60)


if __name__ == "__main__":
    unittest.main(verbosity=2)
