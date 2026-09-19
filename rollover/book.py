"""Several rolls competing for one position.

Rolling September into October and September into November are two different
trades, but they sell the same September. There is one inventory and two claims
on it, and the arithmetic that keeps those claims honest is the whole reason
this module exists.

**Each section's ladder is its allocation.** What a section may roll is what its
ladder says, and the sum of what the sections still have to roll may not exceed
the near-leg position actually held. That is checked before arming rather than
discovered afterwards, because the failure it prevents -- selling the same
September twice -- produces a short position in a month about to expire and
cannot be undone by noticing it later.

The invariant holds as the campaign runs without needing to be re-derived:
rolling thirty thousand out of a hundred thousand leaves seventy thousand held
and thirty thousand less to roll, so comparing *what is left to roll* against
*what is still held* stays true at every point.

**When more than one section qualifies, the one furthest inside its own limit
trades.** Not the cheapest in absolute terms: a section whose limit is thirty
basis points and whose cost is twenty six is doing better against its own
instruction than one at forty four against fifty, even though forty four is the
larger number. This is the same principle the ladder already uses when it works
its cheapest rung first, applied one level up.

Nothing here has a clock, a network or a Tk import.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .money import D


@dataclass
class Claim:
    """One section's outstanding claim on the shared near-leg position.

    `total` is None when the section has no ladder, and that is not the same as
    zero. A section without a ladder has no campaign cap: it rolls a clip at a
    time for as long as the market allows, so its claim on the position is
    "as much as it likes". Counting that as nothing is what makes the whole
    check vacuous -- two sections would sum to less than the position while one
    of them quietly consumed all of it.
    """
    name: str
    near_token: str
    far_token: str
    total: Optional[int]       # what the ladder would roll in all; None = uncapped
    done: int                  # what it has rolled so far
    halted: bool = False

    @property
    def uncapped(self) -> bool:
        return self.total is None

    @property
    def outstanding(self) -> Optional[int]:
        """What is left to roll, or None when there is no cap on it."""
        if self.total is None:
            return None
        return max(0, self.total - self.done)


@dataclass
class Allocation:
    """Whether the sections' claims fit inside the position held."""
    claims: List[Claim] = field(default_factory=list)
    held: Optional[int] = None

    @property
    def uncapped(self) -> List[Claim]:
        return [c for c in self.claims if c.uncapped]

    @property
    def outstanding(self) -> Optional[int]:
        """Total still to roll, or None when any section is uncapped."""
        if self.uncapped:
            return None
        return sum(c.outstanding for c in self.claims)

    @property
    def known(self) -> bool:
        return self.held is not None and not self.uncapped

    @property
    def spare(self) -> Optional[int]:
        if self.held is None or self.outstanding is None:
            return None
        return self.held - self.outstanding

    @property
    def fits(self) -> Optional[bool]:
        """True, False, or None when the position could not be read.

        A single uncapped section is the original single-pair arrangement and
        is allowed: the per-clip position gate is what bounds it. More than one
        section with any of them uncapped cannot be shown to fit, because an
        inventory cannot be divided between claims when one of them is
        unlimited.
        """
        if self.held is None:
            return None
        if self.uncapped:
            return len(self.claims) == 1
        return self.outstanding <= self.held

    def describe(self) -> str:
        count = len(self.claims)
        if self.uncapped and count > 1:
            names = ", ".join(c.name for c in self.uncapped)
            return (f"{count} sections, and {names} has no ladder, so there is "
                    "no cap on what it would roll")
        if self.outstanding is None:
            return "one section with no ladder; the position gate bounds it"
        if self.held is None:
            return (f"{self.outstanding:,} still to roll across "
                    f"{count} section(s); the near position could "
                    "not be read, so it is not known whether that fits")
        verdict = "fits" if self.fits else "DOES NOT FIT"
        return (f"{self.outstanding:,} still to roll across "
                f"{count} section(s) against {self.held:,} held: "
                f"{verdict} ({self.spare:,} spare)")

    def refusal(self) -> Optional[str]:
        """Why arming must be refused, or None if it may proceed.

        An unreadable position is a refusal. The whole point of the check is
        that the claims cannot exceed the inventory, and that cannot be
        established against a number nobody has.
        """
        if self.uncapped and len(self.claims) > 1:
            names = ", ".join(c.name for c in self.uncapped)
            return (f"{names} has no ladder, so there is no limit on how much "
                    "of the position it would roll. With more than one section "
                    "selling the same contract, every section needs a ladder, "
                    "or the others cannot be guaranteed anything to sell.")
        if self.held is None:
            return ("the near position could not be read, so it cannot be "
                    "shown that the sections together will not sell more than "
                    "is held")
        if not self.fits:
            parts = ", ".join(f"{c.name} {c.outstanding:,}"
                              for c in self.claims if c.outstanding)
            return (f"the sections would roll {self.outstanding:,} in total "
                    f"({parts}) out of a near position of only {self.held:,}. "
                    "Reduce a ladder, or the same position would be sold twice.")
        return None


def allocation(claims: List[Claim], held: Optional[int]) -> Allocation:
    """What the sections still claim, against what is actually held."""
    return Allocation(claims=list(claims or []), held=held)


@dataclass
class Candidate:
    """A section that could trade on this tick, and how well it is doing."""
    name: str
    cost_bps: Optional[Decimal]
    limit_bps: Optional[Decimal]
    qualifies: bool
    payload: Any = None        # the caller's decision object, carried through

    @property
    def inside_bps(self) -> Optional[Decimal]:
        """How far below its own limit the cost is. Negative means above it."""
        if self.cost_bps is None or self.limit_bps is None:
            return None
        return D(self.limit_bps) - D(self.cost_bps)


def pick(candidates: List[Candidate]) -> Optional[Candidate]:
    """The section that should trade, or None if none should.

    Furthest inside its own limit wins. Ties keep the order they were given in,
    so the result is reproducible from the log rather than depending on how a
    dictionary happened to be ordered.
    """
    workable = [c for c in (candidates or [])
                if c.qualifies and c.inside_bps is not None]
    if not workable:
        return None

    best = workable[0]
    for candidate in workable[1:]:
        if candidate.inside_bps > best.inside_bps:
            best = candidate
    return best


def explain(candidates: List[Candidate], chosen: Optional[Candidate]) -> str:
    """One line per section, saying where each stands and which was taken."""
    lines = []
    for candidate in (candidates or []):
        inside = candidate.inside_bps
        if inside is None:
            where = "not priced"
        elif inside > 0:
            where = f"{inside} bps inside its limit"
        elif inside == 0:
            where = "exactly at its limit"
        else:
            where = f"{-inside} bps above its limit"
        mark = " <- trading" if chosen is not None and candidate is chosen else ""
        lines.append(f"  {candidate.name}: {where}{mark}")
    return "\n".join(lines)


def claims_from(sections: List[Dict[str, Any]]) -> List[Claim]:
    """Build claims from whatever the caller is holding sections in.

    Deliberately tolerant: a section that cannot be read contributes a claim of
    nothing rather than silently vanishing from the total, because a claim
    missing from the sum is exactly how the position would be over-committed.
    """
    out = []
    for index, section in enumerate(sections or []):
        try:
            out.append(Claim(
                name=str(section.get("name") or f"section {index + 1}"),
                near_token=str(section.get("near_token") or ""),
                far_token=str(section.get("far_token") or ""),
                total=(None if section.get("total") is None
                       else int(section["total"])),
                done=int(section.get("done") or 0),
                halted=bool(section.get("halted")),
            ))
        except Exception:
            # Unreadable, so it is treated as uncapped: a section whose size
            # cannot be established must not be assumed to want nothing.
            out.append(Claim(name=f"section {index + 1}", near_token="",
                             far_token="", total=None, done=0, halted=True))
    return out
