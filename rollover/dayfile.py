"""Reading the day's recording back, and saying what would have worked.

The recorder has written a row every few seconds since it was built, and
nothing in the program has ever opened those files again. So the one question
that decides whether a roll happens at all has no answer on the screen, while
the answer sits on disk.

For 18 September it is blunt: across 2,117 observations of the Sep into Nov
roll the cost never came below 56.4 basis points, against a limit of 50. The
app would not have traded once, all day. At 60 it would have been available
for about a fifth of the time watched, at 65 for nearly three quarters.

That is not a setting to nudge. It is the evidence for a conversation about
whether the limit can roll this position before the contract expires, and
this module exists to put a number on it rather than an impression.

Three things it is careful about, each from getting them wrong first:

**It ignores the recorded `qualifies` column.** That column answers "was this
inside the limit in force at the time", and the limit changes during a
session. One real file holds limit_bps of 30 for 1,759 rows and 50 for 1,058
because the operator edited it at lunchtime. Everything here is recomputed
from `cost_bps` against one limit chosen now.

**It separates the contract pairs.** A single file interleaves them: the same
18 September file holds 2,117 rows of Sep into Nov and 701 of Sep into Oct,
because the contracts were changed mid-afternoon. Pooled, the distribution is
two humps and describes neither roll.

**It counts time, not rows.** Samples stop when the app is closed and resume
when it opens, so counting rows would treat a lunch break as a cheap market.
A gap longer than a few intervals is not credited to anything, which is the
same rule the recorder uses for its own tally.
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Sequence

# The levels the card offers. Chosen to bracket what this contract actually
# does rather than to be round: the interesting region is 55-70.
LEVELS: Sequence[int] = (25, 30, 40, 50, 55, 57, 60, 62, 65, 70)

# A gap longer than this many sample intervals means the app was not
# watching, not that the price sat still.
GAP_INTERVALS = 4


@dataclass
class Level:
    """One candidate limit, and how much of the watched day cleared it."""
    bps: Decimal
    seconds: float
    samples: int
    watched: float

    @property
    def share(self) -> float:
        return (self.seconds / self.watched) if self.watched > 0 else 0.0

    @property
    def minutes(self) -> float:
        return self.seconds / 60.0

    def describe(self) -> str:
        if self.seconds <= 0:
            return "never"
        return f"{self.minutes:,.1f} min ({self.share:.0%})"


@dataclass
class Reading:
    """One contract pair's day."""
    near_token: str
    far_token: str
    samples: int = 0
    watched_seconds: float = 0.0
    first_at: Optional[datetime] = None
    last_at: Optional[datetime] = None
    best_bps: Optional[Decimal] = None
    best_at: Optional[datetime] = None
    worst_bps: Optional[Decimal] = None
    median_bps: Optional[Decimal] = None
    levels: List[Level] = field(default_factory=list)
    limit_bps: Optional[Decimal] = None
    limit_seconds: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.near_token}>{self.far_token}"

    @property
    def watched_minutes(self) -> float:
        return self.watched_seconds / 60.0

    @property
    def span_minutes(self) -> float:
        """Wall clock from the first sample to the last, gaps included."""
        if not (self.first_at and self.last_at):
            return 0.0
        return (self.last_at - self.first_at).total_seconds() / 60.0

    @property
    def coverage(self) -> float:
        return (self.watched_seconds / (self.span_minutes * 60.0)
                if self.span_minutes > 0 else 0.0)

    @property
    def limit_minutes(self) -> float:
        return self.limit_seconds / 60.0

    @property
    def ever_met(self) -> bool:
        return self.limit_seconds > 0

    def level(self, bps) -> Optional[Level]:
        for entry in self.levels:
            if entry.bps == Decimal(str(bps)):
                return entry
        return None

    def cheapest_workable(self) -> Optional[Level]:
        """The tightest offered level that was available at all."""
        for entry in self.levels:
            if entry.seconds > 0:
                return entry
        return None

    def headline(self) -> str:
        """One line, for above the fold."""
        if not self.samples:
            return "nothing recorded yet today"
        best = f"best {self.best_bps} bps" if self.best_bps is not None else "no cost recorded"
        when = f" at {self.best_at:%H:%M}" if self.best_at else ""
        if self.limit_bps is None:
            return f"{best}{when} over {self.watched_minutes:,.0f} min watched"
        met = (f"{self.limit_minutes:,.1f} of {self.watched_minutes:,.0f} min"
               if self.ever_met else
               f"0.0 of {self.watched_minutes:,.0f} min")
        return (f"{best}{when} · at or below your {self.limit_bps} bps limit: "
                f"{met} watched")


def _decimal(text) -> Optional[Decimal]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _stamp(text) -> Optional[datetime]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def path_for(directory: str, when: Optional[date] = None,
             name: str = "") -> str:
    """Where the recorder would have written a given day's file."""
    from .recorder import _filename_part

    stem = f"market-{(when or date.today()):%Y-%m-%d}"
    part = _filename_part(name)
    if part:
        stem += "-" + part
    return os.path.join(directory, stem + ".csv")


def files_for(directory: str, when: Optional[date] = None) -> List[str]:
    """Every recording for a day: the plain file and any per-section ones."""
    stem = f"market-{(when or date.today()):%Y-%m-%d}"
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [os.path.join(directory, n) for n in names
            if n.startswith(stem) and n.endswith(".csv")]


def read(paths, limit_bps=None, levels: Sequence[int] = LEVELS,
         interval_sec: float = 5.0) -> List[Reading]:
    """What each contract pair did, across one or more recordings.

    `limit_bps` is the limit to judge against NOW -- not whatever was in
    force when the row was written. Pass None to get the levels without a
    verdict against any particular limit.

    Never raises. A file that cannot be read contributes nothing, because a
    broken recording is not a reason to take the screen down.
    """
    if isinstance(paths, str):
        paths = [paths]

    limit = None if limit_bps is None else Decimal(str(limit_bps))
    wanted = [Decimal(str(b)) for b in levels]

    # Per pair: the costs seen, and seconds credited to each level.
    costs: Dict[str, List[Decimal]] = {}
    seconds: Dict[str, Dict[Decimal, float]] = {}
    within: Dict[str, float] = {}
    readings: Dict[str, Reading] = {}
    previous: Dict[str, datetime] = {}

    for path in paths:
        try:
            handle = open(path, encoding="utf-8", newline="")
        except OSError:
            continue
        with handle:
            try:
                rows = csv.DictReader(handle)
                for row in rows:
                    _absorb(row, wanted, limit, interval_sec, costs, seconds,
                            within, readings, previous)
            except (csv.Error, UnicodeDecodeError):
                continue

    out = []
    for key, reading in readings.items():
        ordered = sorted(costs.get(key) or [])
        if ordered:
            reading.best_bps = ordered[0]
            reading.worst_bps = ordered[-1]
            reading.median_bps = ordered[len(ordered) // 2]
        reading.watched_seconds = sum(
            v for k, v in (seconds.get(key) or {}).items() if k == wanted[-1]
        ) if False else reading.watched_seconds
        reading.levels = [
            Level(bps=b, seconds=(seconds.get(key) or {}).get(b, 0.0),
                  samples=sum(1 for c in ordered if c <= b),
                  watched=reading.watched_seconds)
            for b in wanted]
        reading.limit_bps = limit
        reading.limit_seconds = within.get(key, 0.0)
        out.append(reading)

    out.sort(key=lambda r: -r.samples)
    return out


def _absorb(row, wanted, limit, interval_sec, costs, seconds, within,
            readings, previous) -> None:
    near = (row.get("near_token") or "").strip()
    far = (row.get("far_token") or "").strip()
    if not near or not far:
        return
    cost = _decimal(row.get("cost_bps"))
    if cost is None:
        return
    at = _stamp(row.get("timestamp"))
    key = f"{near}>{far}"

    reading = readings.get(key)
    if reading is None:
        reading = readings[key] = Reading(near_token=near, far_token=far)
        seconds[key] = {b: 0.0 for b in wanted}
        costs[key] = []
        within[key] = 0.0

    reading.samples += 1
    costs[key].append(cost)
    # Kept as a pair, so the time shown always belongs to the price
    # shown. A file with no timestamps still gets a best, without a when.
    if reading.best_bps is None or cost < reading.best_bps:
        reading.best_bps, reading.best_at = cost, at
    if at is not None:
        if reading.first_at is None or at < reading.first_at:
            reading.first_at = at
        if reading.last_at is None or at > reading.last_at:
            reading.last_at = at

    # How long this observation stood for: the gap back to the previous
    # sample of the same pair, unless that gap means nobody was watching.
    elapsed = interval_sec
    last = previous.get(key)
    if at is not None and last is not None:
        gap = (at - last).total_seconds()
        elapsed = gap if 0 < gap <= interval_sec * GAP_INTERVALS else 0.0
    if at is not None:
        previous[key] = at

    reading.watched_seconds += elapsed
    for level in wanted:
        if cost <= level:
            seconds[key][level] += elapsed
    if limit is not None and cost <= limit:
        within[key] += elapsed
