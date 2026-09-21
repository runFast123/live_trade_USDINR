"""Can the account afford this roll, before any of it is sent.

There was no margin check at all. A shortfall surfaced as a filled near leg and
a rejected far one -- a naked short in the month that is about to expire, which
is the single worst state this program can produce.

**Both legs go in one request.** A calendar spread attracts far less margin than
two outright positions, because the exchange nets them. Asking about the sell
and the buy separately would add two outright numbers together and refuse rolls
the account can comfortably afford. `get_margin` takes
``"token|qty~token|qty"`` for exactly this reason, and it is why the app needs
kkunal 1.3.0.

**Unknown is not affordable.** If the margin or the funds cannot be read, this
says so and the gate blocks. Guessing in the optimistic direction here is how
the naked short happens.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from .broker import _first, _iter_records, call_failed
from .money import D

# The shape, now that it has been seen from a live account (21 Sep 2026,
# segment 13, one lot of each leg):
#
#   {"Status": "Success", "Response": {
#      "Margins": [{"Token": 1769, "QTY": 1, "InitialMargin": 1817.0,
#                   "ExpMgn": 478.9, "LastRate": 95.78}, ...],
#      "Span_Summary": {"Span": 3638.0, "ExpMgn": 959.35,
#                       "MgnBenefit": 0.0, "TotalMgn": 4597.35}}}
#
# The figure wanted is Span_Summary.TotalMgn -- the whole basket. It is read
# by name rather than by scanning, because a per-leg record is walked FIRST
# and taking a number out of one of those would report one leg's margin as
# the roll's, which is an undercount of about half.
_SUMMARY_KEYS = ("Span_Summary", "SpanSummary", "Summary")
_TOTAL_KEYS = ("TotalMgn", "TotalMargin", "TotalMarginRequired",
               "TotalRequirement")

# Fallbacks, for a response that carries no summary at all. Checked against
# the live shape: none of these appears on a per-leg record, which carries
# InitialMargin, ExpMgn, LastRate, Strike, QTY, Segment and Token. A spelling
# that did appear there would be found first by the generic walk and would
# report one leg's margin as the roll's.
_MARGIN_KEYS = ("TotalMgn", "TotalMargin", "RequiredMargin", "Margin",
                "MarginRequired", "TotalMarginRequired", "NetMargin",
                "SpanMargin", "OrderMargin", "TotalRequirement")
_FUNDS_KEYS = ("AvailableMargin", "AvailableBalance", "NetAvailableMargin",
               "CashAvailable", "AvailableCash", "Available", "NetCash",
               "WithdrawableBalance", "MarginAvailable")


@dataclass
class Estimate:
    """What the roll needs and what the account has."""
    required: Optional[Decimal]
    available: Optional[Decimal]
    detail: str

    @property
    def known(self) -> bool:
        return self.required is not None and self.available is not None

    @property
    def affordable(self) -> Optional[bool]:
        """True, False, or None when it could not be established."""
        if not self.known:
            return None
        return self.required <= self.available

    @property
    def headroom(self) -> Optional[Decimal]:
        if not self.known:
            return None
        return self.available - self.required

    def describe(self) -> str:
        if not self.known:
            return self.detail
        from .livemode import rupees
        verdict = "enough" if self.affordable else "NOT ENOUGH"
        return (f"margin {rupees(self.required)} needed, "
                f"{rupees(self.available)} available: {verdict} "
                f"({rupees(self.headroom)} spare)")


def _number(node: Any, keys) -> Optional[Decimal]:
    """The first recognised figure anywhere in a response, as a Decimal."""
    for record in _iter_records(node):
        value = _first(record, keys)
        if value is None:
            continue
        try:
            return D(value)
        except Exception:
            continue
    return None


def total_from(resp: Any) -> Optional[Decimal]:
    """The margin for the WHOLE basket, from a get_margin response.

    The summary is looked for first and by name. Scanning generically would
    reach a per-leg record before it, and one leg's margin is roughly half
    the roll's -- an undercount, in the direction that lets an order through.
    """
    for record in _iter_records(resp):
        for name in _SUMMARY_KEYS:
            summary = record.get(name) if isinstance(record, dict) else None
            if isinstance(summary, dict):
                value = _first(summary, _TOTAL_KEYS)
                if value is not None:
                    try:
                        return D(value)
                    except Exception:
                        pass
    return _number(resp, _MARGIN_KEYS)


def required(broker, near_token: str, far_token: str, qty: int) -> Optional[Decimal]:
    """Margin for the whole roll, both legs in one request. None if unreadable."""
    orders = getattr(getattr(broker, "client", None), "orders", None)
    if orders is None or not hasattr(orders, "get_margin"):
        return None
    try:
        resp = orders.get_margin(
            segment_id=broker.cfg.segment_id,
            token_qty=[(str(near_token), int(qty)), (str(far_token), int(qty))])
    except Exception as exc:
        broker.log.warn(f"Margin call failed: {exc}")
        return None
    if call_failed(resp):
        broker.log.warn(f"Margin call refused: {call_failed(resp)}")
        return None
    return total_from(resp)


def available(broker) -> Optional[Decimal]:
    """Funds the account can put behind a new position. None if unreadable."""
    funds = getattr(getattr(broker, "client", None), "funds", None)
    if funds is None:
        return None
    try:
        resp = funds.get_funds_view()
    except Exception as exc:
        broker.log.warn(f"Funds call failed: {exc}")
        return None
    if call_failed(resp):
        broker.log.warn(f"Funds call refused: {call_failed(resp)}")
        return None
    return _number(resp, _FUNDS_KEYS)


def estimate(broker, near_token: str, far_token: str, qty: int) -> Estimate:
    """What the roll needs against what the account has. Never raises."""
    if qty <= 0:
        return Estimate(None, None, "nothing to size, so no margin is needed")

    try:
        need = required(broker, near_token, far_token, qty)
    except Exception as exc:
        need = None
        broker.log.warn(f"Margin estimate failed: {exc}")
    try:
        have = available(broker)
    except Exception as exc:
        have = None
        broker.log.warn(f"Funds lookup failed: {exc}")

    if need is None and have is None:
        return Estimate(None, None,
                        "neither the margin requirement nor the available funds "
                        "could be read")
    if need is None:
        return Estimate(None, have,
                        "the margin requirement for this roll could not be read, "
                        "so it is not known whether the account can carry it")
    if have is None:
        return Estimate(need, None,
                        "the available funds could not be read, so the margin "
                        f"requirement of {need} cannot be checked against them")

    return Estimate(need, have, "")
