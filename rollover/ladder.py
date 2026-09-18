"""Several limits at once, each with its own quantity.

The single limit answers "may I roll?". It cannot answer the question an
operator actually has, which is "how much am I willing to roll, and at what
price?" Those are different amounts at different prices: ten thousand is worth
doing at thirty basis points, and twenty thousand is worth doing if it takes
fifty to get it done.

So a ladder is a list of rungs, each a limit and a quantity:

    30 bps -> 10,000
    50 bps -> 20,000

**Each rung is its own allocation, and they add up.** The ladder above is a
campaign of 30,000: ten at up to thirty bps, and twenty more at up to fifty.
It is not "twenty thousand in total, ten of which must be cheap".

**The cheapest rung with budget left is always worked first.** When the market
prints twenty eight basis points both rungs qualify, and which one is credited
matters, because it decides what is still tradeable later:

    fill the 30 rung  -> the 50 rung keeps its 20,000, and a later market at
                         forty five basis points can still be traded
    fill the 50 rung  -> the 50 rung is spent, and the remaining 10,000 now
                         needs thirty basis points or better, forever

Same price paid either way. Cheapest-first simply leaves more of the campaign
reachable, so it is never worse.

**No rung may be looser than the tenor limit.** The schedule in
`limit_bps_schedule` is the client's instruction about what this roll is worth
paying; a ladder decides how to spend within it, and may not raise it. A rung
above the ceiling is a configuration error rather than a silent loosening.

Nothing here has a clock, a network or a Tk import.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .limits import from_bps, to_bps
from .money import D, money, q4


class LadderError(ValueError):
    """The ladder as configured cannot be used."""


@dataclass(frozen=True)
class Rung:
    """One limit, and how much the operator will roll at it."""
    bps: Decimal
    qty: int

    @property
    def key(self) -> str:
        """Stable across restarts and independent of ordering."""
        return f"{self.bps.normalize():f}"

    def lots(self, lot_size: int) -> Optional[int]:
        if not lot_size:
            return None
        return self.qty // lot_size

    def describe(self, lot_size: Optional[int] = None) -> str:
        size = f"{self.qty:,}"
        if lot_size:
            size += f" ({self.qty // lot_size} lot" + \
                    ("s)" if self.qty // lot_size != 1 else ")")
        return f"{money(self.bps, 0)} bps -> {size}"


@dataclass
class RungView:
    """A rung measured against the market as it is right now."""
    rung: Rung
    done: int
    limit_rupees: Optional[Decimal] = None
    required_far_ask: Optional[Decimal] = None
    distance_bps: Optional[Decimal] = None   # negative means it qualifies
    qualifies: bool = False

    @property
    def remaining(self) -> int:
        return max(0, self.rung.qty - self.done)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    @property
    def workable(self) -> bool:
        return self.qualifies and not self.exhausted

    @property
    def status(self) -> str:
        if self.exhausted:
            return "done"
        if self.qualifies:
            return "READY"
        if self.distance_bps is None:
            return "no price"
        return f"{money(self.distance_bps, 1)} bps away"


@dataclass
class Ladder:
    """The rungs, cheapest first."""
    rungs: List[Rung] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.rungs)

    @property
    def total_qty(self) -> int:
        return sum(r.qty for r in self.rungs)

    @property
    def loosest_bps(self) -> Optional[Decimal]:
        return max((r.bps for r in self.rungs), default=None)

    def done_total(self, progress: Dict[str, int]) -> int:
        return sum(min(r.qty, int(progress.get(r.key, 0) or 0)) for r in self.rungs)

    def remaining_total(self, progress: Dict[str, int]) -> int:
        return max(0, self.total_qty - self.done_total(progress))

    def evaluate(self, roll_cost: Optional[Decimal], reference: Optional[Decimal],
                 near_bid: Optional[Decimal],
                 progress: Optional[Dict[str, int]] = None) -> List[RungView]:
        """Every rung, priced against this quote. Cheapest first."""
        progress = progress or {}
        cost_bps = (to_bps(roll_cost, reference)
                    if roll_cost is not None and reference else None)

        views = []
        for rung in self.rungs:
            view = RungView(rung=rung,
                            done=min(rung.qty, int(progress.get(rung.key, 0) or 0)))
            if reference is not None and D(reference) > 0:
                view.limit_rupees = from_bps(rung.bps, reference)
                if near_bid is not None:
                    # The far ask at which this rung would just qualify. This is
                    # the number to watch the market against.
                    view.required_far_ask = q4(D(near_bid) + view.limit_rupees)
            if cost_bps is not None:
                view.distance_bps = (cost_bps - rung.bps).quantize(Decimal("0.1"))
                view.qualifies = cost_bps < rung.bps
            views.append(view)
        return views

    def active(self, views: List[RungView]) -> Optional[RungView]:
        """The rung to work now: cheapest that qualifies and still has budget."""
        for view in views:               # already cheapest first
            if view.workable:
                return view
        return None

    def credit(self, progress: Dict[str, int], rung: Rung, qty: int) -> Dict[str, int]:
        """Record a fill against a rung. Returns a new mapping."""
        out = dict(progress or {})
        out[rung.key] = min(rung.qty, int(out.get(rung.key, 0) or 0) + max(0, int(qty)))
        return out


def parse(raw: Any, lot_size: Optional[int] = None,
          ceiling_bps: Optional[Decimal] = None) -> Ladder:
    """Build a ladder from configuration. Raises LadderError on anything unusable.

    Refuses rather than repairs: a ladder that quietly dropped a malformed rung
    would roll less than the operator asked for and say nothing about it.
    """
    if raw in (None, "", [], {}):
        return Ladder([])
    if not isinstance(raw, (list, tuple)):
        raise LadderError("limit_ladder must be a list of {\"bps\": ..., \"qty\": ...}")

    rungs: List[Rung] = []
    seen = set()
    for index, entry in enumerate(raw, start=1):
        where = f"limit_ladder entry {index}"
        if not isinstance(entry, dict):
            raise LadderError(f"{where} is not an object: {entry!r}")

        if "bps" not in entry:
            raise LadderError(f"{where} has no bps")
        if "qty" not in entry:
            raise LadderError(f"{where} has no qty")

        try:
            bps = D(entry["bps"])
        except Exception:
            raise LadderError(f"{where}: bps {entry['bps']!r} is not a number")
        if bps <= 0:
            raise LadderError(f"{where}: bps must be positive")

        try:
            qty = int(D(entry["qty"]))
        except Exception:
            raise LadderError(f"{where}: qty {entry['qty']!r} is not a whole number")
        if qty <= 0:
            raise LadderError(f"{where}: qty must be positive")
        if lot_size and qty % lot_size:
            raise LadderError(
                f"{where}: qty {qty:,} is not a multiple of the lot size "
                f"{lot_size:,}; the exchange would refuse it")

        if bps in seen:
            raise LadderError(
                f"{where}: {money(bps, 0)} bps appears twice. Give one rung the "
                "combined quantity instead, so the total is unambiguous.")
        seen.add(bps)

        if ceiling_bps is not None and bps > ceiling_bps:
            raise LadderError(
                f"{where}: {money(bps, 0)} bps is looser than the {money(ceiling_bps, 0)} "
                "bps limit for this tenor. A ladder decides how to spend within "
                "the limit and may not raise it; change limit_bps_schedule if "
                "the limit itself is meant to move.")

        rungs.append(Rung(bps=bps, qty=qty))

    rungs.sort(key=lambda r: r.bps)
    return Ladder(rungs)


def campaign_key(near_token: Any, far_token: Any) -> str:
    """Identifies which roll the progress belongs to.

    Progress is campaign-level, not daily: rolling 30,000 over a fortnight is
    the normal case. But it must not survive a change of contracts, because
    that is a different roll entirely.
    """
    return f"{str(near_token or '').strip()}>{str(far_token or '').strip()}"
