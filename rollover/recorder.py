"""Writing down what the market actually did.

Gate results are shown on screen and then forgotten, so a month of dry running
would answer none of the questions that decide whether this strategy works. The
most important of those is not *did it trade* but **how far out of the money the
limit actually is**.

"Your 50 bps limit was never touched; 57 would have been available for eleven
minutes today" is something a client can act on. "Nothing happened again" is
not.

So every few seconds a row goes to a dated CSV, and the session keeps a running
tally of how close the cost came and for how long. Nothing here can affect a
trading decision: it only observes.
"""
from __future__ import annotations

import csv
import os
import threading
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, List, Optional

from .money import money

COLUMNS = (
    "timestamp", "source",
    "near_token", "near_bid", "near_ask", "near_bid_qty", "near_ask_qty",
    "far_token", "far_bid", "far_ask", "far_bid_qty", "far_ask_qty",
    "roll_cost", "cost_bps", "limit", "limit_bps", "tenor_days",
    "qualifies", "blocking",
)

# How close did we get? Reported as seconds at or below each of these
# distances from the limit, in basis points.
NEAR_MISS_BPS = (0, 1, 2, 5, 10)


def _filename_part(name: str) -> str:
    """A section key such as "1769>1584" is not a filename on Windows."""
    keep = [c if (c.isalnum() or c in "-_") else "_" for c in str(name or "")]
    return "".join(keep).strip("_")


class Recorder:
    """Appends one row per interval and keeps the day's statistics."""

    def __init__(self, directory: str, interval_sec: float = 5.0,
                 name: str = ""):
        self.directory = directory
        self.interval = max(0.5, float(interval_sec))
        # Each roll gets its own file. Sep into Oct and Sep into Nov are
        # different costs against different limits, and pooling them would
        # produce a "closest approach" belonging to neither.
        # Kept as given for anything a person reads, and flattened only
        # where it has to be a filename.
        self.name = str(name or "")
        os.makedirs(directory, exist_ok=True)

        self._lock = threading.Lock()
        self._day = date.today()
        self._last_write = 0.0
        self._last_sample_at: Optional[float] = None

        self.samples = 0
        self.best_bps: Optional[Decimal] = None
        self.best_at: Optional[datetime] = None
        self.seconds_observed = 0.0
        # bps distance above the limit -> seconds spent at or inside it
        self.seconds_within: Dict[int, float] = {b: 0.0 for b in NEAR_MISS_BPS}
        # how long the cost sat at or below each whole bps level
        self.seconds_at_or_below: Dict[int, float] = {}

    # ------------------------------------------------------------------ paths
    @property
    def path(self) -> str:
        stem = f"market-{self._day:%Y-%m-%d}"
        part = _filename_part(self.name)
        if part:
            stem += "-" + part
        return os.path.join(self.directory, stem + ".csv")

    def _roll_day_if_needed(self) -> None:
        today = date.today()
        if today != self._day:
            self._day = today
            self._reset_stats()

    def _reset_stats(self) -> None:
        self.samples = 0
        self.best_bps = None
        self.best_at = None
        self.seconds_observed = 0.0
        self.seconds_within = {b: 0.0 for b in NEAR_MISS_BPS}
        self.seconds_at_or_below = {}
        self._last_sample_at = None

    # ----------------------------------------------------------------- record
    def sample(self, near_quote, far_quote, decision, report,
               source: str = "") -> bool:
        """Record one observation. Returns True when a row was written.

        Called every tick; it writes only once per interval. Never raises — an
        observer that can break the trading loop is worse than no observer.
        """
        try:
            return self._sample(near_quote, far_quote, decision, report, source)
        except Exception:
            return False

    def _sample(self, near_quote, far_quote, decision, report, source) -> bool:
        now = time.monotonic()
        with self._lock:
            if now - self._last_write < self.interval:
                return False
            self._roll_day_if_needed()

            elapsed = 0.0 if self._last_sample_at is None else now - self._last_sample_at
            # A long gap means the app was not watching, not that the price sat
            # still, so do not credit it to any bucket.
            if elapsed > self.interval * 4:
                elapsed = 0.0
            self._last_sample_at = now
            self._last_write = now

            self._tally(decision, elapsed)
            row = self._row(near_quote, far_quote, decision, report, source)

        self._append(row)
        return True

    def _tally(self, decision, elapsed: float) -> None:
        self.samples += 1
        if decision is None or decision.cost_bps is None:
            return

        cost = decision.cost_bps
        if self.best_bps is None or cost < self.best_bps:
            self.best_bps = cost
            self.best_at = datetime.now()

        self.seconds_observed += elapsed

        # Whole bps levels, so "what limit would have worked" is answerable.
        level = int(cost.to_integral_value(rounding="ROUND_CEILING"))
        for known in list(self.seconds_at_or_below):
            if level <= known:
                self.seconds_at_or_below[known] += elapsed
        self.seconds_at_or_below.setdefault(level, 0.0)
        self.seconds_at_or_below[level] += elapsed

        limit_bps = decision.limit_bps
        if limit_bps is not None:
            over = cost - limit_bps
            for band in NEAR_MISS_BPS:
                if over <= band:
                    self.seconds_within[band] += elapsed

    def _row(self, near_quote, far_quote, decision, report, source) -> List:
        def price(quote, attr):
            return money(getattr(quote, attr)) if quote else ""

        def size(quote, attr):
            value = getattr(quote, attr, None) if quote else None
            return "" if value is None else value

        blocking = ""
        if report is not None:
            blocking = "; ".join(g.name for g in report.failures)

        return [
            datetime.now().isoformat(timespec="milliseconds"),
            source,
            getattr(near_quote, "token", ""),
            price(near_quote, "bid"), price(near_quote, "ask"),
            size(near_quote, "bid_qty"), size(near_quote, "ask_qty"),
            getattr(far_quote, "token", ""),
            price(far_quote, "bid"), price(far_quote, "ask"),
            size(far_quote, "bid_qty"), size(far_quote, "ask_qty"),
            money(decision.roll_cost) if decision else "",
            money(decision.cost_bps, 1) if decision and decision.cost_bps is not None else "",
            money(decision.limit) if decision else "",
            money(decision.limit_bps, 1) if decision and decision.limit_bps is not None else "",
            decision.limit_detail.tenor_days if decision and decision.limit_detail else "",
            "yes" if decision and decision.qualifies else "no",
            blocking,
        ]

    def _append(self, row: List) -> None:
        new_file = not os.path.exists(self.path)
        with open(self.path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if new_file:
                writer.writerow(COLUMNS)
            writer.writerow(row)

    # ---------------------------------------------------------------- reading
    def implied_limit(self, seconds_wanted: float = 60.0) -> Optional[int]:
        """The lowest whole bps limit that was available for long enough.

        This is the number worth taking back to the client: the level at which
        today's market would actually have let the roll happen.
        """
        with self._lock:
            candidates = [level for level, secs in sorted(self.seconds_at_or_below.items())
                          if secs >= seconds_wanted]
        return candidates[0] if candidates else None

    def summary(self, limit_bps: Optional[Decimal] = None) -> str:
        """A few lines for the log at the end of a session."""
        with self._lock:
            if not self.samples:
                who = f" for {self.name}" if self.name else ""
                return f"No market samples recorded{who}."

            who = f" ({self.name})" if self.name else ""
            lines = [f"Market summary for {self._day}{who}: {self.samples} samples, "
                     f"{self.seconds_observed / 60:.0f} minutes observed."]
            if self.best_bps is not None:
                when = f" at {self.best_at:%H:%M:%S}" if self.best_at else ""
                lines.append(f"  closest approach {money(self.best_bps, 1)} bps{when}")
            if limit_bps is not None:
                inside = self.seconds_within.get(0, 0.0)
                lines.append(f"  at or below the {money(limit_bps, 0)} bps limit: "
                             f"{inside / 60:.1f} minutes")
                for band in NEAR_MISS_BPS[1:]:
                    secs = self.seconds_within.get(band, 0.0)
                    lines.append(f"  within {band} bps of it: {secs / 60:.1f} minutes")

        implied = self.implied_limit()
        if implied is not None:
            lines.append(f"  a limit of {implied} bps would have been available "
                         "for at least a minute")
        else:
            lines.append("  no level was available for a full minute today")
        return "\n".join(lines)
