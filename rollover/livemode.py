"""Deciding whether real orders may be sent, and what that would commit.

Dry run and live are not two equally valid settings with a switch between them.
Dry run is the resting state; live is a thing a person does deliberately, for
one session, having been shown what it means. So this is written as a set of
preconditions rather than a boolean, and the answer to "may I go live?" is a
list of reasons rather than True or False.

Going back to dry run is always allowed and never asks anything. Safety
controls that are slow to engage and slow to release get switched off and left
off; this one is slow in only one direction.

Nothing here touches Tk, so all of it is testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, List, Optional

from .money import D, money

# Typed, not clicked. Long enough that it cannot be muscle memory.
CONFIRM = "GO LIVE"


@dataclass
class Check:
    """One precondition, and what to tell the operator about it."""
    name: str
    passed: bool
    detail: str

    @property
    def mark(self) -> str:
        return "OK" if self.passed else "NO"


def preflight(cfg, engine=None, feed_healthy: Optional[bool] = None) -> List[Check]:
    """Everything that must be true before real orders may be sent.

    `engine` is optional so this can be asked before one exists, in which case
    the engine-dependent checks are reported as unknown, which is a failure.
    """
    checks: List[Check] = []

    # The one that is not about this program at all.
    confirmed = bool(getattr(cfg, "quantity_unit_confirmed", False))
    checks.append(Check(
        "quantity unit confirmed", confirmed,
        "the exchange reads an order quantity as contracts or as units of the "
        "underlying, and which one has never been established. Confirm it with "
        "Choice, then set quantity_unit_confirmed to true in config.json."
        if not confirmed else
        "config.json records that the quantity unit has been confirmed"))

    try:
        cfg.validate()
        checks.append(Check("configuration valid", True, "config.json passes every check"))
    except Exception as exc:
        checks.append(Check("configuration valid", False, str(exc).splitlines()[0]))

    if engine is None:
        checks.append(Check("engine running", False, "the watch loop has not started"))
        return checks

    broker = getattr(engine, "broker", None)
    logged_in = bool(getattr(broker, "logged_in", False))
    checks.append(Check("signed in", logged_in,
                        "signed in to Choice" if logged_in else
                        "not signed in, so nothing can be sent"))

    session = getattr(engine, "session", None)
    halted = bool(getattr(session, "halted_reason", None))
    checks.append(Check(
        "not halted", not halted,
        (getattr(session, "halted_reason", "") or "")[:140] if halted
        else "no outstanding halt"))

    fresh = _scrip_is_today(engine)
    checks.append(Check(
        "scrip master is today's", fresh,
        "today's contract list, so circuit limits and ticks are current" if fresh
        else "the contract list is not today's; circuit limits may be stale"))

    if feed_healthy is None:
        feed_healthy = _feed_is_healthy(engine)
    checks.append(Check(
        "live price feed", bool(feed_healthy),
        "prices are coming from the websocket" if feed_healthy else
        "the websocket is not delivering; the REST fallback has been seen 13 "
        "minutes stale, which is not a basis for sending an order"))

    return checks


def blockers(checks: List[Check]) -> List[Check]:
    return [c for c in checks if not c.passed]


def may_go_live(checks: List[Check]) -> bool:
    return not blockers(checks)


def _scrip_is_today(engine) -> bool:
    broker = getattr(engine, "broker", None)
    stamp = getattr(broker, "scrip_file_date", None)
    if stamp is None:
        return False
    from datetime import date
    return stamp == date.today()


def _feed_is_healthy(engine) -> bool:
    feed = getattr(engine, "feed", None)
    if feed is None:
        return False
    try:
        tokens = [t for t in (getattr(engine.session, "near", None),
                              getattr(engine.session, "far", None)) if t]
        return bool(feed.healthy([t.token for t in tokens]))
    except Exception:
        return False


@dataclass
class Exposure:
    """What one clip commits, read both ways round.

    The second reading is the whole reason this is shown. Until the quantity
    unit is settled, "1 lot" may reach the exchange as one contract or as a
    thousand, and the difference is three orders of magnitude of money.
    """
    lots: int
    lot_size: int
    price: Optional[Decimal]
    qty_sent: int
    # What the whole ladder would roll, if one is configured. Engaging live
    # authorises the campaign, not a single clip, and a dialog that showed only
    # the clip would understate it by the number of clips in the ladder.
    campaign_qty: int = 0
    campaign_left: int = 0

    @property
    def as_units(self) -> Optional[Decimal]:
        """Rupee notional if the exchange reads the quantity as we intend."""
        if self.price is None:
            return None
        return D(self.lots) * D(self.lot_size) * self.price

    @property
    def as_contracts(self) -> Optional[Decimal]:
        """Rupee notional if the exchange reads qty_sent as contracts."""
        if self.price is None:
            return None
        return D(self.qty_sent) * D(self.lot_size) * self.price

    @property
    def ambiguous(self) -> bool:
        return self.qty_sent != self.lots

    @property
    def campaign_value(self) -> Optional[Decimal]:
        if self.price is None or not self.campaign_left:
            return None
        return D(self.campaign_left) * self.price

    def lines(self, unit_confirmed: bool) -> List[str]:
        out = [f"{self.lots} lot(s), sent to the exchange as qty {self.qty_sent}"]
        if self.price is None:
            out.append("no price available, so the notional cannot be shown")
        else:
            out.append(f"about {rupees(self.as_units)} at {money(self.price)}")
            if self.ambiguous and not unit_confirmed:
                out.append(
                    f"if the exchange reads qty {self.qty_sent} as CONTRACTS "
                    f"this is {rupees(self.as_contracts)}")

        if self.campaign_left:
            clips = -(-self.campaign_left // max(1, self.qty_sent))
            out.append("")
            out.append(f"the ladder still has {self.campaign_left:,} to roll, "
                       f"about {clips} clip(s)")
            if self.campaign_value is not None:
                out.append(f"which is {rupees(self.campaign_value)} in total")
        return out


def rupees(value: Optional[Decimal]) -> str:
    """A rupee amount in the Indian convention, which is how it will be read."""
    if value is None:
        return "unknown"
    amount = D(value)
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    if amount >= D("10000000"):
        return f"{sign}Rs {_round(amount / D('10000000'), 2):,.2f} crore"
    if amount >= D("100000"):
        return f"{sign}Rs {_round(amount / D('100000'), 2):,.2f} lakh"
    return f"{sign}Rs {_round(amount, 0):,.0f}"


def _round(value: Decimal, places: int) -> Decimal:
    """Round half up, because a reader checking it on a calculator will.

    Decimal formats with banker's rounding by default, which turns 23.945 into
    23.94 and invites someone to think the number is wrong.
    """
    from decimal import ROUND_HALF_UP
    return value.quantize(D(1).scaleb(-places), rounding=ROUND_HALF_UP)


def exposure(cfg, price: Optional[Decimal], lot_size: Optional[int] = None,
             engine=None) -> Exposure:
    lots = int(getattr(cfg, "lots", 1) or 1)
    size = int(lot_size or getattr(cfg, "lot_size", 1000) or 1000)

    total = left = 0
    try:
        # getattr's default only swallows AttributeError, so the lookup itself
        # belongs inside the guard. Nothing about the ladder may stop the
        # dialog that asks whether to send real orders from opening.
        ladder = getattr(engine, "ladder", None)
        if ladder:
            total = ladder.total_qty
            left = ladder.remaining_total(getattr(engine, "ladder_progress", {}))
    except Exception:
        total = left = 0

    return Exposure(lots=lots, lot_size=size, price=price, qty_sent=lots * size,
                    campaign_qty=total, campaign_left=left)


def summary(cfg, checks: List[Check], exposure_: Exposure) -> str:
    """The text an operator reads before deciding. Plain, and not reassuring."""
    lines = ["Switching to LIVE lets this program send real orders on your",
             "account, without asking again, for the rest of this session.", ""]

    lines.append("What this commits:")
    for line in exposure_.lines(bool(getattr(cfg, "quantity_unit_confirmed", False))):
        lines.append(f"    {line}")
    lines.append("")

    lines.append("Before it can:")
    for check in checks:
        lines.append(f"  [{check.mark}] {check.name}")
        if not check.passed:
            for piece in _wrap(check.detail, 62):
                lines.append(f"         {piece}")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> List[str]:
    line, out = "", []
    for word in str(text).split():
        # A halt reason can contain a path or an id with no spaces in it, and
        # one of those would run off the side of the dialog carrying the rest
        # of the sentence with it.
        while len(word) > width:
            if line:
                out.append(line)
                line = ""
            out.append(word[:width])
            word = word[width:]
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def go_live(cfg, engine, answer: str, checks: Optional[List[Check]] = None) -> Any:
    """Switch to live if everything permits. Returns None, or a refusal string."""
    checks = checks if checks is not None else preflight(cfg, engine)
    stopped = blockers(checks)
    if stopped:
        return "cannot go live: " + "; ".join(c.name for c in stopped)
    if (answer or "").strip() != CONFIRM:
        return "not confirmed, so nothing changed"

    cfg.dry_run = False
    # Live mode must never inherit an arm made while in dry run. Whoever armed
    # it was authorising a simulation.
    if engine is not None and hasattr(engine, "disarm"):
        engine.disarm("switched to live")
    return None


def go_dry(cfg, engine) -> None:
    """Back to dry run. Always allowed, never asks, takes effect at once."""
    cfg.dry_run = True
    if engine is not None and hasattr(engine, "disarm"):
        engine.disarm("switched to dry run")
