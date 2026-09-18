"""The roll rule itself.

This module has no network, no clock and no state. Given two quotes and a
configuration it returns the decision and the exact limit prices. Everything
here is Decimal arithmetic on prices in rupees.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

from .limits import Limit, LimitError, resolve, to_bps
from .money import ceil_tick, floor_tick, money, q4
from .quotes import Quote


@dataclass
class RollDecision:
    near_bid: Decimal
    far_ask: Decimal
    roll_cost: Decimal
    sell_limit: Decimal
    buy_limit: Decimal
    worst_case: Decimal
    limit: Decimal
    qty: int
    qualifies: bool
    blockers: List[str] = field(default_factory=list)
    limit_detail: Optional[Limit] = None    # how that limit was arrived at
    reference: Optional[Decimal] = None     # the price basis points are taken of

    @property
    def cost_per_lot(self) -> Decimal:
        """Rupees per lot at the observed roll cost."""
        return q4(self.roll_cost * self.qty)

    @property
    def worst_case_per_lot(self) -> Decimal:
        return q4(self.worst_case * self.qty)

    @property
    def cost_bps(self) -> Optional[Decimal]:
        """The roll cost as basis points of the reference price.

        This is the unit the cost is actually discussed in: a one month roll
        trades around thirty bps whatever the rupee happens to be doing.
        """
        return to_bps(self.roll_cost, self.reference) if self.reference else None

    @property
    def worst_case_bps(self) -> Optional[Decimal]:
        return to_bps(self.worst_case, self.reference) if self.reference else None

    @property
    def limit_bps(self) -> Optional[Decimal]:
        if self.limit_detail and self.limit_detail.bps is not None:
            return self.limit_detail.bps
        return to_bps(self.limit, self.reference) if self.reference else None

    def describe(self) -> str:
        lines = [
            f"near bid      {money(self.near_bid)}",
            f"far ask       {money(self.far_ask)}",
            f"roll cost     {money(self.roll_cost)}"
            + (f" ({money(self.cost_bps, 1)} bps)" if self.cost_bps is not None else "")
            + f"   limit {money(self.limit)}"
            + (f" ({self.limit_detail.describe()})" if self.limit_detail else ""),
            f"sell limit    {money(self.sell_limit)}",
            f"buy limit     {money(self.buy_limit)}",
            f"worst case    {money(self.worst_case)}",
            f"qty           {self.qty}",
            f"qualifies     {'YES' if self.qualifies else 'no'}",
        ]
        lines.extend(f"blocked by    {b}" for b in self.blockers)
        return "\n".join(lines)


def compute(near: Quote, far: Quote, cfg,
            days: Optional[int] = None) -> RollDecision:
    """Evaluate the roll rule against one pair of quotes.

    roll_cost = far.ask - near.bid

    Read in that order and no other. The reverse, near.bid - far.ask, is
    negative in a normal contango market, which is always below any positive
    limit, so it would fire on every single tick.

    `days` is how far apart the two contracts expire. In basis point mode it
    decides which limit applies, so without it there is no limit and the rule
    refuses rather than falling back to some other tenor's number.
    """
    tick = cfg.tick_d
    allowance = cfg.allowance

    near_bid = q4(near.bid)
    far_ask = q4(far.ask)
    roll_cost = q4(far_ask - near_bid)

    # Basis points are taken of the near contract's mid rather than the bid we
    # happen to be hitting, so the limit does not shift with our own side of
    # the spread. At thirty bps the difference is under a thousandth of a
    # paisa, but the mid is the honest reference.
    reference = q4((near.bid + near.ask) / 2)

    limit_error = None
    try:
        detail = resolve(cfg, reference, days)
        limit = detail.rupees
    except LimitError as exc:
        limit_error = str(exc)
        detail = None
        limit = None

    # Sell lower and buy higher by the allowance to improve the chance of a
    # fill, then move each to a real tick in the direction that keeps it
    # marketable. Both roundings widen the cost, so the worst case is
    # recomputed from the rounded prices, never from the raw ones.
    sell_limit = floor_tick(near_bid - allowance, tick)
    buy_limit = ceil_tick(far_ask + allowance, tick)
    worst_case = q4(buy_limit - sell_limit)

    blockers: List[str] = []
    if limit is None:
        # No limit means no comparison, and no comparison means no trade.
        return RollDecision(
            near_bid=near_bid, far_ask=far_ask, roll_cost=roll_cost,
            sell_limit=sell_limit, buy_limit=buy_limit, worst_case=worst_case,
            limit=q4(Decimal(0)), qty=cfg.clip_qty, qualifies=False,
            blockers=[limit_error], limit_detail=None, reference=reference,
        )

    if roll_cost >= limit:
        blockers.append(
            f"roll cost {money(roll_cost)} is not below the limit {money(limit)}"
        )
    elif worst_case >= limit:
        # The observed cost qualifies but the prices actually being sent do not.
        blockers.append(
            f"roll cost {money(roll_cost)} qualifies, but the worst case fill at these "
            f"limit prices is {money(worst_case)}, which reaches the limit "
            f"{money(limit)}; reduce allowance_ticks (currently {cfg.allowance_ticks})"
        )
    if sell_limit <= 0:
        blockers.append("sell limit computed at or below zero")

    return RollDecision(
        near_bid=near_bid,
        far_ask=far_ask,
        roll_cost=roll_cost,
        sell_limit=sell_limit,
        buy_limit=buy_limit,
        worst_case=worst_case,
        limit=limit,
        qty=cfg.clip_qty,
        qualifies=not blockers,
        blockers=blockers,
        limit_detail=detail,
        reference=reference,
    )
