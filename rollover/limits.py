"""Working out what the limit actually is.

A roll's cost scales with how far you are rolling. One month costs roughly
thirty basis points and two months roughly sixty, so a single fixed rupee
figure is only ever right for one tenor. Point it at a different pair of
contracts and it is silently wrong, which is exactly what happened: a one month
roll was left running a two month limit, seventy four percent looser than
intended.

So the limit is expressed in basis points against tenor, and turned into rupees
against the live price on every tick.

**A basis point is a share of the price, not a fixed amount.** Thirty basis
points is 0.2879 at USDINR 95.97 and 0.3150 at 105.00. It equals 0.30 only when
the price is exactly 100, and that near coincidence is the whole reason this
module exists rather than someone typing 0.30 and calling it thirty bps.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Optional, Tuple

from .money import D, floor4, money, q4

BPS = Decimal("10000")

# A calendar month is not a fixed number of days, and contract expiries do not
# land on neat boundaries: 28 Sep to 28 Oct is 30 days, 28 Sep to 26 Nov is 59.
DAYS_PER_MONTH = Decimal("30.44")


class LimitError(ValueError):
    """The limit for this pair of contracts cannot be determined."""


@dataclass(frozen=True)
class Limit:
    """The limit in force, in both the unit it was set in and rupees."""
    rupees: Decimal
    mode: str                       # "bps" or "absolute"
    bps: Optional[Decimal] = None
    reference: Optional[Decimal] = None     # the price the bps were applied to
    tenor_days: Optional[int] = None
    tenor_months: Optional[int] = None

    def describe(self) -> str:
        if self.mode != "bps":
            return f"{money(self.rupees)} fixed"
        return (f"{money(self.bps, 0)} bps of {money(self.reference)} "
                f"= {money(self.rupees)}  ({self.tenor_months} month"
                f"{'s' if self.tenor_months != 1 else ''}, {self.tenor_days} days)")


def to_bps(amount: Decimal, reference: Decimal) -> Optional[Decimal]:
    """Express a rupee amount as basis points of a reference price."""
    reference = D(reference)
    if reference <= 0:
        return None
    return (D(amount) / reference * BPS).quantize(Decimal("0.1"))


def from_bps(bps: Decimal, reference: Decimal) -> Decimal:
    """Turn basis points of a reference price into rupees.

    Quantized to four places, the same resolution prices are held at, so the
    comparison against the roll cost is between two numbers on the same grid
    and a decision can be reproduced exactly from the log.

    Rounded DOWN, never to nearest. A roll cost is the difference of two
    tick-grid prices, so it always lands exactly on that four-place grid --
    and a limit that rounded up onto it would accept a cost fractionally
    above the client's instruction. Measured at up to 0.0000477 rupees a
    unit, about five paise on a thousand: nothing in money, and the wrong
    direction. Rounding down can only ever make the app stricter than the
    instruction, never looser.
    """
    return floor4(D(bps) / BPS * D(reference))


def tenor_days(near_expiry, far_expiry) -> Optional[int]:
    if near_expiry is None or far_expiry is None:
        return None
    return (far_expiry - near_expiry).days


def tenor_months(days: Optional[int]) -> Optional[int]:
    """The nearest whole month to a day count, at least one."""
    if days is None or days <= 0:
        return None
    months = (D(days) / DAYS_PER_MONTH).to_integral_value()
    return max(1, int(months))


def parse_schedule(raw: Dict) -> Dict[int, Decimal]:
    out: Dict[int, Decimal] = {}
    for key, value in (raw or {}).items():
        try:
            months = int(str(key).strip())
        except (TypeError, ValueError):
            raise LimitError(f"limit_bps_schedule has a non numeric tenor: {key!r}")
        if months < 1:
            raise LimitError(f"limit_bps_schedule tenor must be at least 1 month: {key!r}")
        try:
            bps = D(value)
        except Exception:
            raise LimitError(
                f"limit_bps_schedule value for {months} months is not a number: {value!r}")
        if bps <= 0:
            raise LimitError(f"limit_bps_schedule value for {months} months must be positive")
        out[months] = bps
    return out


def match_tenor(days: int, schedule: Dict[int, Decimal],
                tolerance_days: int) -> Tuple[int, Decimal]:
    """Find the scheduled tenor this pair of contracts actually is.

    The day count has to land near the nominal length of that tenor. Without
    that check a three day weekly roll would count as "one month" on a calendar
    comparison and be given a full month's allowance, which is around ten times
    what it should be.
    """
    if not schedule:
        raise LimitError("limit_bps_schedule is empty, so there is no limit to apply")

    best = None
    for months, bps in sorted(schedule.items()):
        nominal = D(months) * DAYS_PER_MONTH
        distance = abs(D(days) - nominal)
        if distance <= tolerance_days and (best is None or distance < best[0]):
            best = (distance, months, bps)

    if best is None:
        known = ", ".join(f"{m} month{'s' if m != 1 else ''} at {money(b, 0)} bps"
                          for m, b in sorted(schedule.items()))
        raise LimitError(
            f"these contracts are {days} days apart, which is not within "
            f"{tolerance_days} days of any scheduled tenor ({known}). Add an entry "
            f"to limit_bps_schedule, or set limit_mode to absolute.")

    _, months, bps = best
    return months, bps


def resolve(cfg, reference_price: Optional[Decimal],
            days: Optional[int]) -> Limit:
    """The limit in force for this pair of contracts, right now."""
    if cfg.limit_mode == "absolute":
        return Limit(rupees=q4(cfg.roll_limit_d), mode="absolute",
                     tenor_days=days, tenor_months=tenor_months(days))

    if days is None:
        raise LimitError(
            "the tenor between the two contracts is unknown, so a basis point "
            "limit cannot be turned into a price")
    if reference_price is None or D(reference_price) <= 0:
        raise LimitError("no reference price, so basis points cannot be converted")

    schedule = parse_schedule(cfg.limit_bps_schedule)
    months, bps = match_tenor(days, schedule, cfg.tenor_tolerance_days)
    return Limit(
        rupees=from_bps(bps, reference_price),
        mode="bps",
        bps=bps,
        reference=q4(reference_price),
        tenor_days=days,
        tenor_months=months,
    )
