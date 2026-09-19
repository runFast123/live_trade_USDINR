"""Exact decimal arithmetic for prices.

Every price in this application is a Decimal built from a string. Floating point
is never used for a price or for the roll cost, because 96.52 - 95.94 evaluates
to 0.5799999999999983 in binary floating point, and a comparison against a limit
can silently flip on that error.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, getcontext

getcontext().prec = 28

Q4 = Decimal("0.0001")   # price resolution in rupees
Q2 = Decimal("0.01")     # two decimal places, for display
ZERO = Decimal("0")


class PriceError(ValueError):
    """Raised when a value cannot be trusted as a price."""


def D(value) -> Decimal:
    """Build an exact Decimal. Floats are rejected rather than silently coerced."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # A float has already lost precision; repr() is the closest honest reading.
        return Decimal(repr(value))
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            raise PriceError("empty price string")
        try:
            return Decimal(text)
        except InvalidOperation as exc:
            raise PriceError(f"not a number: {value!r}") from exc
    raise PriceError(f"unsupported price type: {type(value).__name__}")


def q4(value) -> Decimal:
    """Quantize to 4 decimal places, the resolution of a currency futures price."""
    return D(value).quantize(Q4)


def floor4(value) -> Decimal:
    """Quantize to 4 decimal places, always downwards.

    For a limit rather than a price: rounding a limit to nearest can push it
    up onto the grid a roll cost lands on, and accept a cost fractionally
    above what was asked for. Down is the direction that can only be stricter.
    """
    return D(value).quantize(Q4, rounding=ROUND_FLOOR)


def floor_tick(value, tick: Decimal) -> Decimal:
    """Round down to the nearest tick multiple."""
    value, tick = D(value), D(tick)
    if tick <= 0:
        raise PriceError("tick must be positive")
    return ((value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick).quantize(Q4)


def ceil_tick(value, tick: Decimal) -> Decimal:
    """Round up to the nearest tick multiple."""
    value, tick = D(value), D(tick)
    if tick <= 0:
        raise PriceError("tick must be positive")
    return ((value / tick).to_integral_value(rounding=ROUND_CEILING) * tick).quantize(Q4)


def on_tick(value, tick: Decimal) -> bool:
    """True when the price sits exactly on the tick grid."""
    value, tick = D(value), D(tick)
    return (value % tick) == ZERO


def to_exchange_units(rupees, divisor) -> Decimal:
    """Convert rupees into the integer units the order API expects.

    The unit is whatever the scrip master declares as PriceDivisor for that
    contract, not a fixed conversion:

        equity      PriceDivisor 100        1300.00 rupees -> 130000
        USDINR fut  PriceDivisor 10000000   95.9400 rupees -> 959400000

    The documented "price in paisa" is simply what a divisor of 100 amounts to
    for equity. Using 100 for a currency future would understate the price by a
    factor of a hundred thousand, so the contract's own divisor is always used.
    """
    divisor = D(divisor)
    if divisor <= 0:
        raise PriceError("price divisor must be positive")
    value = D(rupees) * divisor
    if value != value.to_integral_value():
        raise PriceError(
            f"{rupees} does not convert to a whole number of exchange units "
            f"at a divisor of {divisor} (got {value})"
        )
    return value.to_integral_value()


def money(value, places: int = 4) -> str:
    """Format a Decimal for display without exponent notation."""
    try:
        return f"{D(value).quantize(Decimal(1).scaleb(-places)):f}"
    except (PriceError, InvalidOperation):
        return "--"
