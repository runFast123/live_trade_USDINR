"""Checking that the account moved the way the roll said it did.

"ROLL COMPLETE" is currently asserted from a row scraped out of the order book
and never checked against anything. That row is the broker's summary of what it
believes; the position book is what the account actually holds. They should
agree, and the only way to know they do is to look.

A roll of N units moves the near leg down by N and the far leg up by N. Nothing
else is a successful roll, and the failure that matters -- the near leg sold
without the far leg bought -- shows up here as plainly as it possibly could.

**An unreadable position book is not a mismatch.** If the endpoint fails there
is nothing to contradict; that is a warning, because both legs already reported
certain fills. A book that reads clearly and disagrees is something else
entirely, and halts.

**Positions do not update the instant a trade prints.** The broker's book lags,
so this waits for the numbers to arrive rather than reading once and declaring
a mismatch that was only ever a race.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class Positions:
    """What the account held in both legs at one moment."""
    near: Optional[int]
    far: Optional[int]

    @property
    def readable(self) -> bool:
        return self.near is not None and self.far is not None

    def describe(self) -> str:
        return (f"near {self.near if self.near is not None else '?'}, "
                f"far {self.far if self.far is not None else '?'}")


@dataclass
class Result:
    """The verdict, and enough detail to act on it."""
    agreed: bool
    checked: bool              # False when the position book could not be read
    detail: str
    before: Optional[Positions] = None
    after: Optional[Positions] = None
    near_moved: Optional[int] = None
    far_moved: Optional[int] = None
    expected: int = 0

    @property
    def contradicted(self) -> bool:
        """Read clearly, and disagrees. The only case that halts."""
        return self.checked and not self.agreed


def capture(broker, near_token: str, far_token: str) -> Positions:
    """Both legs as they stand. Never raises; unreadable comes back as None."""
    def read(token):
        if not token:
            return None
        try:
            return broker.net_qty(token)
        except Exception:
            return None

    return Positions(near=read(near_token), far=read(far_token))


def check(broker, near_token: str, far_token: str, before: Positions,
          expected: int, wait: float = 6.0,
          sleep: Callable[[float], None] = time.sleep) -> Result:
    """Confirm the position moved by `expected` in both legs.

    Waits for the broker's book to catch up rather than reading once. Returns
    as soon as the numbers agree, so a healthy roll costs almost nothing.
    """
    if expected <= 0:
        return Result(agreed=True, checked=False,
                      detail="nothing was filled, so there is nothing to reconcile",
                      before=before, expected=expected)

    if not before.readable:
        return Result(
            agreed=True, checked=False, before=before, expected=expected,
            detail=("the position book could not be read before the roll "
                    f"({before.describe()}), so there is nothing to compare "
                    "against. The fills themselves were confirmed."))

    deadline = time.monotonic() + wait
    after = before
    while True:
        after = capture(broker, near_token, far_token)
        if after.readable:
            near_moved = before.near - after.near      # a sale reduces it
            far_moved = after.far - before.far         # a purchase raises it
            if near_moved == expected and far_moved == expected:
                return Result(
                    agreed=True, checked=True, before=before, after=after,
                    near_moved=near_moved, far_moved=far_moved, expected=expected,
                    detail=(f"position confirmed: near down {near_moved}, "
                            f"far up {far_moved}, as expected"))
        if time.monotonic() >= deadline:
            break
        sleep(0.5)

    if not after.readable:
        return Result(
            agreed=True, checked=False, before=before, after=after,
            expected=expected,
            detail=("the position book could not be read after the roll "
                    f"({after.describe()}), so the fills could not be "
                    "independently confirmed."))

    near_moved = before.near - after.near
    far_moved = after.far - before.far
    return Result(
        agreed=False, checked=True, before=before, after=after,
        near_moved=near_moved, far_moved=far_moved, expected=expected,
        detail=_explain(near_moved, far_moved, expected, before, after))


def _explain(near_moved: int, far_moved: int, expected: int,
             before: Positions, after: Positions) -> str:
    """Say what is wrong in the terms the person fixing it will think in."""
    # near_moved is measured as a fall and far_moved as a rise, so the same
    # positive number means opposite directions and they cannot share a
    # description. Getting this backwards would describe a correct far leg as
    # having gone the wrong way.
    parts = [
        f"the position book does not match the roll. Expected near down "
        f"{expected} and far up {expected}; the book shows near "
        f"{_moved(near_moved, rose='up', fell='down')} and far "
        f"{_moved(far_moved, rose='down', fell='up')}."
    ]

    if near_moved == expected and far_moved != expected:
        short = expected - far_moved
        parts.append(f"The near leg moved but the far leg is {short} short, which "
                     "is a half-rolled position.")
    elif far_moved == expected and near_moved != expected:
        parts.append("The far leg moved but the near leg did not, so the account "
                     "may now be long both months.")
    elif near_moved == 0 and far_moved == 0:
        parts.append("Neither leg moved, although both orders reported fills. "
                     "Either the book is stale or the fills were not real.")

    parts.append(f"Before: {before.describe()}. After: {after.describe()}.")
    parts.append("Check the broker terminal before sending anything else.")
    return " ".join(parts)


def _moved(value: Optional[int], rose: str = "up", fell: str = "down") -> str:
    """Describe a movement. The caller says which way a positive number points."""
    if value is None:
        return "unknown"
    if value == 0:
        return "unchanged"
    return f"{fell if value > 0 else rose} {abs(value)}"
