"""Which leg goes out first.

Near-first is right when both legs fill trivially: sell the near month, buy the
far month, and if the second leg fails you are left flat, which is a state you
can correct at leisure.

It inverts when the far leg is thin. The far book here shows eight to eighty
lots at the touch against a near book showing fourteen to four hundred. Sell
twenty five lots of the near month into that and the far leg is the one likely
to come up short, so a half roll stops being the exception and becomes the
expected outcome.

Far-first turns an uncertain outcome into a smaller certain one. Buy what the
far book actually offers, then sell exactly that much of the near month. The
thin leg sets the size after the fact instead of being a gamble: no residual,
no unwind, no telephone call.

**It is not free.** Far-first commits you to selling the near leg afterwards. If
that sale then fails you are long both months, which is the mirror of the
problem and no better. So it is only taken when the near book is deep enough
that the sale is not in doubt -- three times the clip, by default.

**Expiry day is unconditional.** The near contract stops trading at 12:30 on its
expiry day. A sold near leg with no far leg at that point cannot be corrected at
any price, because the instrument you would have to buy back is gone. Far-first
always, whatever the books look like and whatever the configuration says.

Nothing here has a clock, a network or a Tk import. Every input is a number.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

NEAR_FIRST = "near_first"
FAR_FIRST = "far_first"

AUTO = "auto"
MODES = (AUTO, NEAR_FIRST, FAR_FIRST)

# How much deeper than the clip the near book must be before we will commit to
# selling it *after* having bought the far leg. Three times is not a precise
# figure; it is "comfortably more than we need", so that the sale is not in
# doubt in the latency between the two orders.
NEAR_DEPTH_MULTIPLE = 3


@dataclass(frozen=True)
class Sequence:
    """Which leg first, and the reason, which is shown and logged."""
    order: str
    reason: str

    @property
    def far_first(self) -> bool:
        return self.order == FAR_FIRST

    @property
    def near_first(self) -> bool:
        return self.order == NEAR_FIRST

    def describe(self) -> str:
        which = "far leg first" if self.far_first else "near leg first"
        return f"{which} ({self.reason})"


def choose(clip: int,
           near_bid_qty: Optional[int],
           far_ask_qty: Optional[int],
           expiry_day: bool = False,
           mode: str = AUTO,
           near_depth_multiple: int = NEAR_DEPTH_MULTIPLE) -> Sequence:
    """Decide which leg to send first.

    `near_bid_qty` is the size resting on the near bid, which is what we sell
    into. `far_ask_qty` is the size resting on the far ask, which is what we buy
    from. Either may be None when the feed did not give a size, and None is
    never read as "plenty".
    """
    if expiry_day:
        # Not a preference and not overridable. After 12:30 the near contract
        # does not exist to be bought back.
        return Sequence(FAR_FIRST,
                        "expiry day: a sold near leg with no far leg cannot be "
                        "corrected once the near contract stops trading")

    if mode == FAR_FIRST:
        return Sequence(FAR_FIRST, "far_first set in config")
    if mode == NEAR_FIRST:
        return Sequence(NEAR_FIRST, "near_first set in config")

    if clip <= 0:
        return Sequence(NEAR_FIRST, "no clip to size")

    # Far-first means committing to sell the near leg afterwards. Without a
    # readable near book that commitment cannot be justified, and an unknown
    # size is not a large one.
    if near_bid_qty is None:
        return Sequence(NEAR_FIRST,
                        "the near bid size is unknown, so selling it afterwards "
                        "cannot be relied on")

    need = clip * max(1, int(near_depth_multiple))
    if near_bid_qty < need:
        return Sequence(NEAR_FIRST,
                        f"the near bid shows {near_bid_qty:,}, under the "
                        f"{need:,} wanted before committing to sell it second")

    if far_ask_qty is None:
        return Sequence(NEAR_FIRST,
                        "the far ask size is unknown, so it is not known to be "
                        "the thin side")

    if far_ask_qty < clip:
        return Sequence(FAR_FIRST,
                        f"the far ask shows {far_ask_qty:,} against a clip of "
                        f"{clip:,}, so the far leg sets the size")

    return Sequence(NEAR_FIRST,
                    f"the far ask shows {far_ask_qty:,}, enough for the whole "
                    f"clip of {clip:,}")


def valid_mode(mode: str) -> bool:
    return str(mode).strip().lower() in MODES
