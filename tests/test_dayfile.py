"""Reading the day's recording back.

The recorder has written a row every few seconds since it was built and
nothing ever opened those files again, so the question that decides whether
this roll happens had no answer on screen while the answer sat on disk.

Three of these tests exist because the obvious implementation is wrong in a
way that reads as authoritative: trusting the recorded `qualifies` column,
pooling the contract pairs, and counting rows instead of time. Each produces
a confident number about nothing.

The last class runs against the real 18 September recording if it is still
in the repo, so the figures quoted to the operator are checked rather than
remembered.
"""
from __future__ import annotations

import csv
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from rollover import dayfile
from rollover.recorder import COLUMNS

REAL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "dist", "data", "market-2026-09-18.csv")


class Writer:
    """Builds a recording the way the recorder would."""

    def __init__(self, path):
        self.path = path
        self.at = datetime(2026, 9, 18, 10, 0, 0)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerow(COLUMNS)

    def row(self, cost_bps, near="1769", far="1584", limit_bps="50",
            gap_sec=5, qualifies=None):
        self.at += timedelta(seconds=gap_sec)
        if qualifies is None:
            # A recording can hold a blank or unreadable cost, and the
            # writer has to be able to produce one.
            try:
                qualifies = ("yes" if Decimal(str(cost_bps))
                             <= Decimal(limit_bps) else "no")
            except Exception:
                qualifies = "no"
        values = {c: "" for c in COLUMNS}
        values.update({
            "timestamp": self.at.isoformat(timespec="milliseconds"),
            "source": "live feed", "near_token": near, "far_token": far,
            "cost_bps": str(cost_bps), "limit_bps": str(limit_bps),
            "qualifies": qualifies, "tenor_days": "59",
        })
        with open(self.path, "a", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerow([values[c] for c in COLUMNS])
        return self


class DayCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dayfile_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "market-2026-09-18.csv")
        self.writer = Writer(self.path)

    def read(self, **kw):
        return dayfile.read(self.path, **kw)

    def one(self, **kw):
        got = self.read(**kw)
        self.assertEqual(len(got), 1, f"expected one pair, got {len(got)}")
        return got[0]


class TestTheBasicCount(DayCase):
    def setUp(self):
        super().setUp()
        for cost in ("60", "62", "58", "61", "59"):
            self.writer.row(cost)

    def test_it_finds_the_pair(self):
        self.assertEqual(self.one().key, "1769>1584")

    def test_it_counts_the_samples(self):
        self.assertEqual(self.one().samples, 5)

    def test_the_best_and_worst(self):
        got = self.one()
        self.assertEqual(got.best_bps, Decimal("58"))
        self.assertEqual(got.worst_bps, Decimal("62"))

    def test_the_median(self):
        self.assertEqual(self.one().median_bps, Decimal("60"))

    def test_the_time_shown_belongs_to_the_price_shown(self):
        got = self.one()
        self.assertEqual(got.best_at.minute, 0)
        self.assertEqual(got.best_at.second, 15)     # the third row

    def test_each_level_gets_its_seconds(self):
        got = self.one(levels=(57, 60, 65))
        self.assertEqual(got.level(57).seconds, 0.0)
        self.assertEqual(got.level(60).seconds, 15.0)    # 58, 59, 60
        self.assertEqual(got.level(65).seconds, 25.0)    # all five

    def test_share_is_of_the_watched_time(self):
        got = self.one(levels=(65,))
        self.assertAlmostEqual(got.level(65).share, 1.0)


class TestItIgnoresTheRecordedVerdict(DayCase):
    """`qualifies` answers a question that changed during the session.

    One real file holds limit_bps of 30 for 1,759 rows and 50 for 1,058,
    because the operator edited the limit at lunchtime. A reader that
    believed that column would describe two different questions as one.
    """

    def setUp(self):
        super().setUp()
        # Same cost throughout. The recorded verdict flips because the limit
        # in force changed, not because the market did.
        for _ in range(4):
            self.writer.row("45", limit_bps="30")     # recorded "no"
        for _ in range(4):
            self.writer.row("45", limit_bps="50")     # recorded "yes"

    def test_the_recorded_verdict_disagrees_with_itself(self):
        with open(self.path, encoding="utf-8") as fh:
            said = {r["qualifies"] for r in csv.DictReader(fh)}
        self.assertEqual(said, {"yes", "no"})

    def test_but_one_limit_now_gives_one_answer(self):
        got = self.one(limit_bps=50)
        self.assertEqual(got.samples, 8)
        self.assertEqual(got.limit_seconds, got.watched_seconds)

    def test_and_a_tighter_one_gives_the_other(self):
        self.assertEqual(self.one(limit_bps=30).limit_seconds, 0.0)

    def test_the_limit_reported_is_the_one_asked_for(self):
        self.assertEqual(self.one(limit_bps=57).limit_bps, Decimal("57"))


class TestItSeparatesThePairs(DayCase):
    """One file interleaves them when the contracts change mid-session.

    Pooled, the 18 September distribution is two humps -- a two month roll
    around 60 bps and a one month roll around 31 -- and describes neither.
    """

    def setUp(self):
        super().setUp()
        for _ in range(6):
            self.writer.row("62", far="1584")
        for _ in range(3):
            self.writer.row("31", far="1284")

    def test_two_readings_come_back(self):
        self.assertEqual(len(self.read()), 2)

    def test_the_busier_one_is_first(self):
        self.assertEqual(self.read()[0].key, "1769>1584")

    def test_each_keeps_its_own_best(self):
        by_key = {r.key: r for r in self.read()}
        self.assertEqual(by_key["1769>1584"].best_bps, Decimal("62"))
        self.assertEqual(by_key["1769>1284"].best_bps, Decimal("31"))

    def test_a_cheap_second_pair_does_not_flatter_the_first(self):
        """Pooled, 50 bps would look available a third of the time."""
        by_key = {r.key: r for r in self.read(limit_bps=50)}
        self.assertEqual(by_key["1769>1584"].limit_seconds, 0.0)
        self.assertGreater(by_key["1769>1284"].limit_seconds, 0.0)


class TestItCountsTimeNotRows(DayCase):
    """Samples stop when the app is closed. Counting rows would treat a
    lunch break as a cheap market."""

    def setUp(self):
        super().setUp()
        for _ in range(4):
            self.writer.row("58")                       # 20s at 58
        self.writer.row("58", gap_sec=3600)             # an hour later
        for _ in range(4):
            self.writer.row("58")

    def test_the_gap_is_not_credited_to_anything(self):
        got = self.one(levels=(60,))
        self.assertLess(got.watched_seconds, 60.0)

    def test_the_span_still_shows_the_whole_stretch(self):
        self.assertGreater(self.one().span_minutes, 60.0)

    def test_so_coverage_says_how_much_was_actually_watched(self):
        self.assertLess(self.one().coverage, 0.05)

    def test_the_samples_either_side_are_all_kept(self):
        self.assertEqual(self.one().samples, 9)


class TestWhatItSaysInAWord(DayCase):
    def test_a_limit_never_met_says_zero_of_the_watched_time(self):
        for _ in range(5):
            self.writer.row("60")
        got = self.one(limit_bps=50)
        self.assertFalse(got.ever_met)
        self.assertIn("0.0 of", got.headline())
        self.assertIn("50 bps limit", got.headline())

    def test_and_names_the_tightest_level_that_would_have_worked(self):
        for _ in range(5):
            self.writer.row("60")
        got = self.one(limit_bps=50, levels=(50, 55, 62, 70))
        self.assertEqual(got.cheapest_workable().bps, Decimal("62"))

    def test_a_level_never_reached_describes_itself_as_never(self):
        self.writer.row("60")
        self.assertEqual(self.one(levels=(50,)).level(50).describe(), "never")

    def test_with_no_limit_it_still_reports_the_best(self):
        self.writer.row("60")
        self.assertIn("best 60 bps", self.one().headline())


class TestItNeverRaises(DayCase):
    """A broken recording is not a reason to take the screen down."""

    def test_a_missing_file(self):
        self.assertEqual(dayfile.read(os.path.join(self.dir, "nope.csv")), [])

    def test_a_file_that_is_not_csv_at_all(self):
        path = os.path.join(self.dir, "junk.csv")
        with open(path, "wb") as fh:
            fh.write(b"\x00\x01\x02 not a recording")
        self.assertEqual(dayfile.read(path), [])

    def test_rows_with_no_cost_are_skipped(self):
        self.writer.row("")
        self.writer.row("60")
        self.assertEqual(self.one().samples, 1)

    def test_rows_with_no_tokens_are_skipped(self):
        self.writer.row("60", near="", far="")
        self.assertEqual(dayfile.read(self.path), [])

    def test_an_unreadable_cost_is_skipped_rather_than_guessed(self):
        self.writer.row("not-a-number")
        self.writer.row("60")
        self.assertEqual(self.one().samples, 1)

    def test_an_empty_recording(self):
        self.assertEqual(dayfile.read(self.path), [])

    def test_one_bad_file_does_not_lose_a_good_one(self):
        bad = os.path.join(self.dir, "bad.csv")
        with open(bad, "wb") as fh:
            fh.write(b"\xff\xfe nonsense")
        self.writer.row("60")
        self.assertEqual(len(dayfile.read([bad, self.path])), 1)


class TestFindingTheFiles(DayCase):
    def test_it_picks_up_the_plain_file_and_the_per_section_ones(self):
        from datetime import date
        for name in ("market-2026-09-18-Sep_into_Nov.csv",
                     "market-2026-09-18-Sep_into_Oct.csv"):
            Writer(os.path.join(self.dir, name)).row("60")
        got = dayfile.files_for(self.dir, date(2026, 9, 18))
        self.assertEqual(len(got), 3)

    def test_it_ignores_other_days(self):
        from datetime import date
        Writer(os.path.join(self.dir, "market-2026-09-17.csv")).row("60")
        got = dayfile.files_for(self.dir, date(2026, 9, 18))
        self.assertEqual([os.path.basename(p) for p in got],
                         ["market-2026-09-18.csv"])

    def test_a_missing_directory_is_not_an_error(self):
        self.assertEqual(dayfile.files_for(os.path.join(self.dir, "nope")), [])


@unittest.skipUnless(os.path.exists(REAL), "the 18 September recording is gone")
class TestAgainstTheRealRecording(unittest.TestCase):
    """The figures quoted to the operator, checked rather than remembered."""

    @classmethod
    def setUpClass(cls):
        cls.readings = dayfile.read(REAL, limit_bps=50)
        cls.by_key = {r.key: r for r in cls.readings}

    def nov(self):
        return self.by_key["1769>1584"]

    def test_both_pairs_are_in_that_one_file(self):
        self.assertEqual(sorted(self.by_key), ["1769>1284", "1769>1584"])

    def test_the_target_roll_has_the_samples_reported(self):
        self.assertEqual(self.nov().samples, 2117)

    def test_the_cheapest_it_ever_got(self):
        self.assertEqual(self.nov().best_bps, Decimal("56.4"))

    def test_the_median(self):
        self.assertEqual(self.nov().median_bps, Decimal("63.6"))

    def test_fifty_was_never_available(self):
        """The number the whole card exists to state."""
        self.assertEqual(self.nov().limit_seconds, 0.0)
        self.assertFalse(self.nov().ever_met)

    def test_sixty_would_have_been_about_a_fifth_of_the_time(self):
        self.assertAlmostEqual(self.nov().level(60).share, 0.21, places=2)

    def test_sixty_five_would_have_been_about_three_quarters(self):
        self.assertAlmostEqual(self.nov().level(65).share, 0.72, places=2)

    def test_the_other_pair_is_a_different_market_entirely(self):
        self.assertEqual(self.by_key["1769>1284"].best_bps, Decimal("29.2"))

    def test_coverage_is_reported_because_it_is_well_under_half(self):
        """180 minutes watched across a span of 427."""
        self.assertLess(self.nov().coverage, 0.5)
        self.assertGreater(self.nov().watched_minutes, 170)


if __name__ == "__main__":
    unittest.main(verbosity=2)
