"""Reading the best bid and best ask for both legs.

Two things about the touchline feed are not documented and must never be
guessed at:

  1. The name of the bid and ask fields in the response.
  2. The unit. The touchline returns rupees, while the order API takes the
     contract's own exchange units. These are two different scales and must not
     be confused: 95.9200 arrives from the feed as 95.9200 but is sent to the
     order API as 959200000.

The feed also sends prices as 32 bit floats, so 95.92 arrives as
95.91999816894531 and has to be put back on the tick grid before it is used.

Both are resolved once, explicitly, and checked against a plausibility band. If
either is ambiguous the app refuses to produce a quote at all, which stops the
rule from ever being evaluated on a misread price.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Dict, List, Optional, Tuple

from .money import D, PriceError, q4

# Field names seen across Choice/Odin touchline payloads, best candidate first.
# The live CDS touchline uses plain "Buy" and "Sell", and each is the five level
# depth ladder rather than a single price, so the top of book is element zero.
BID_CANDIDATES = (
    "BestBidPrice", "BestBuyPrice", "BidPrice", "BuyPrice",
    "Bid", "BBP", "BuyRate", "BestBid", "Buy",
)
ASK_CANDIDATES = (
    "BestAskPrice", "BestSellPrice", "AskPrice", "SellPrice", "OfferPrice",
    "Ask", "BSP", "SellRate", "BestAsk", "BestOffer", "Sell",
)
DEPTH_PRICE_KEYS = ("Price", "price", "Rate", "rate")
DEPTH_QTY_KEYS = ("Qty", "qty", "Quantity", "quantity")
TOKEN_CANDIDATES = ("Token", "token", "ScripToken", "InstrumentToken", "TokenNo")

# Fallback unit conversions, used only when the scrip master does not declare a
# PriceDivisor for the contract. USDINR futures declare 10000000.
ALLOWED_DIVISORS = (D("1"), D("100"))


class QuoteError(RuntimeError):
    """The feed could not be turned into a price we are willing to trade on."""


@dataclass
class Quote:
    token: str
    bid: Decimal
    ask: Decimal
    at: float                       # time.monotonic() when the fetch returned
    divisor: Decimal
    bid_qty: Optional[int] = None   # size resting at the top of book
    ask_qty: Optional[int] = None
    moved_at: Optional[float] = None  # when the book last actually changed
    raw: Dict[str, Any] = field(default_factory=dict)

    def age(self, now: Optional[float] = None) -> float:
        return (time.monotonic() if now is None else now) - self.at

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    def since_move(self, now: Optional[float] = None) -> Optional[float]:
        """Seconds since the book last changed, as opposed to since we read it."""
        if self.moved_at is None:
            return None
        return (time.monotonic() if now is None else now) - self.moved_at


def _walk_records(node: Any, out: List[dict]) -> None:
    """Collect every dict in the response that looks like an instrument record."""
    if isinstance(node, dict):
        if any(k in node for k in TOKEN_CANDIDATES):
            out.append(node)
        for value in node.values():
            _walk_records(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk_records(item, out)


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(D(value))
    except Exception:
        return None


def _record_token(record: dict) -> Optional[str]:
    for key in TOKEN_CANDIDATES:
        if key in record and record[key] not in (None, ""):
            return str(record[key]).strip()
    return None


def top_of_book(value: Any, label: str, token: str) -> Tuple[Decimal, Optional[int]]:
    """Read the best price, and its size, from a touchline field.

    The field is either a single price or the depth ladder, in which case the
    first rung is the top of book.
    """
    if isinstance(value, list):
        if not value:
            raise QuoteError(f"token {token}: the {label} ladder is empty, so there "
                             "is nothing resting on that side")
        value = value[0]

    if isinstance(value, dict):
        price = None
        for key in DEPTH_PRICE_KEYS:
            if key in value:
                price = value[key]
                break
        if price is None:
            raise QuoteError(
                f"token {token}: the {label} entry has no price field. "
                f"Keys present: {', '.join(sorted(str(k) for k in value))}")
        qty = None
        for key in DEPTH_QTY_KEYS:
            if key in value:
                try:
                    qty = int(D(value[key]))
                except Exception:
                    qty = None
                break
        return D(price), qty

    return D(value), None


def _pick_field(record: dict, candidates: Tuple[str, ...], override: Optional[str],
                label: str) -> str:
    """Resolve which key in the record holds the bid or the ask."""
    if override:
        if override not in record:
            raise QuoteError(
                f"configured {label}_field {override!r} is not in the touchline record. "
                f"Available keys: {', '.join(sorted(record))}"
            )
        return override

    lowered = {k.lower(): k for k in record}
    for name in candidates:
        if name.lower() in lowered:
            return lowered[name.lower()]

    raise QuoteError(
        f"could not find the {label} field in the touchline record. "
        f"Set {label}_field in config.json to one of: {', '.join(sorted(record))}"
    )


def detect_divisor(values: List[Decimal], low: Decimal, high: Decimal,
                   forced: Optional[Decimal] = None,
                   candidates: Optional[Tuple[Decimal, ...]] = None) -> Decimal:
    """Find the one unit conversion that puts every price inside the band.

    `candidates` normally leads with the PriceDivisor the scrip master declares
    for the contract, so this is a check of a stated scale rather than a guess.
    A forced divisor from the configuration is checked too, so a wrong manual
    setting fails loudly instead of trading on a mis-scaled price.
    """
    if not values:
        raise QuoteError("no prices to scale")

    if forced is not None:
        scaled = [v / forced for v in values]
        if all(low <= s <= high for s in scaled):
            return forced
        raise QuoteError(
            f"configured price_divisor {forced} gives "
            f"{[str(q4(s)) for s in scaled]}, outside the band {low}..{high}"
        )

    pool = tuple(candidates) if candidates else ALLOWED_DIVISORS
    workable = [d for d in pool
                if all(low <= (v / d) <= high for v in values)]

    if len(workable) == 1:
        return workable[0]
    if not workable:
        raise QuoteError(
            f"prices {[str(v) for v in values]} are not inside the band {low}..{high} "
            f"at any supported scale. Check price_band_low / price_band_high, or the feed."
        )
    raise QuoteError(
        f"prices {[str(v) for v in values]} fit the band {low}..{high} at more than one "
        f"scale ({', '.join(str(d) for d in workable)}). Set price_divisor in config.json."
    )


class QuoteReader:
    """Turns one touchline call into a validated Quote for each leg."""

    def __init__(self, client, cfg):
        self.client = client
        self.cfg = cfg
        self.divisor: Optional[Decimal] = None      # the last one used, for display
        # The two sources legitimately use different scales: the websocket
        # sends raw exchange units while REST sends rupees. The guard against a
        # scale changing under us therefore has to be kept per source, or
        # falling back from one to the other would look like corruption.
        self._divisors: Dict[str, Decimal] = {}
        self.bid_key: Optional[str] = None
        self.ask_key: Optional[str] = None
        self.last_raw: Any = None
        self.instruments: Dict[str, Any] = {}

    def set_instruments(self, instruments: Dict[str, Any]) -> None:
        """Hand over the resolved contracts so their declared PriceDivisor and
        circuit limits can be used instead of a guessed scale and a generic band."""
        self.instruments = dict(instruments)

    def _divisor_candidates(self) -> Tuple[Decimal, ...]:
        declared = []
        for info in self.instruments.values():
            value = getattr(info, "price_divisor", None)
            if value and value > 0 and value not in declared:
                declared.append(value)
        # Declared scales first, then the generic fallbacks.
        return tuple(declared) + tuple(d for d in ALLOWED_DIVISORS if d not in declared)

    def _band(self) -> Tuple[Decimal, Decimal]:
        """The widest circuit band across both legs, or the configured band."""
        lows = [i.low_range for i in self.instruments.values()
                if getattr(i, "low_range", None) is not None]
        highs = [i.high_range for i in self.instruments.values()
                 if getattr(i, "high_range", None) is not None]
        if lows and highs:
            return min(lows), max(highs)
        return self.cfg.price_band_low_d, self.cfg.price_band_high_d

    def _snap(self, token: str, price: Decimal, label: str) -> Decimal:
        """Put a feed price back exactly on the exchange's tick grid.

        The touchline sends prices as 32 bit floats, so 95.92 arrives as
        95.91999816894531. Left alone, rounding that down to the tick grid would
        give 95.9175, a whole tick below the real bid, and the app would sell a
        tick cheaper than it meant to.
        """
        info = self.instruments.get(token)
        tick = getattr(info, "tick", None)
        if not tick or tick <= 0:
            return q4(price)

        steps = (price / tick).to_integral_value(rounding=ROUND_HALF_EVEN)
        snapped = steps * tick
        drift = abs(price - snapped)
        if drift > tick / 4:
            raise QuoteError(
                f"token {token}: {label} {price} is {drift} away from the tick grid "
                f"of {tick}, which is too far to be float rounding"
            )
        return q4(snapped)

    def _check_against_limits(self, token: str, bid: Decimal, ask: Decimal) -> None:
        info = self.instruments.get(token)
        low = getattr(info, "low_range", None)
        high = getattr(info, "high_range", None)
        if low is None or high is None:
            return
        for name, price in (("bid", bid), ("ask", ask)):
            if not (low <= price <= high):
                raise QuoteError(
                    f"token {token}: {name} {price} is outside the day's limits "
                    f"{low}..{high} for this contract"
                )

    def _request(self, tokens: List[str]) -> Any:
        spec = ",".join(f"{self.cfg.segment_id}@{t}" for t in tokens)
        return self.client.market.get_multiple_touchline(spec)

    def fetch(self, tokens: List[str]) -> Dict[str, Quote]:
        """Fetch both legs in one call. Raises QuoteError rather than returning
        a partial or doubtful result."""
        raw = self._request(tokens)
        self.last_raw = raw
        at = time.monotonic()
        return self.parse(raw, tokens, at)

    def parse(self, raw: Any, tokens: List[str], at: float) -> Dict[str, Quote]:
        if isinstance(raw, dict):
            status = str(raw.get("Status", "")).strip().lower()
            if status and status != "success":
                raise QuoteError(f"touchline call failed: {raw}")

        records: List[dict] = []
        _walk_records(raw, records)
        if not records:
            raise QuoteError(f"touchline response contained no instrument records: {raw}")

        wanted = {str(t).strip() for t in tokens}
        by_token: Dict[str, dict] = {}
        for record in records:
            token = _record_token(record)
            if token in wanted and token not in by_token:
                by_token[token] = record

        missing = wanted - set(by_token)
        if missing:
            raise QuoteError(
                "touchline did not return these tokens: " + ", ".join(sorted(missing))
            )

        sample = by_token[next(iter(wanted))]
        self.bid_key = _pick_field(sample, BID_CANDIDATES, self.cfg.bid_field, "bid")
        self.ask_key = _pick_field(sample, ASK_CANDIDATES, self.cfg.ask_field, "ask")

        raw_values: Dict[str, Tuple[Decimal, Decimal]] = {}
        sizes: Dict[str, Tuple[Optional[int], Optional[int]]] = {}
        for token, record in by_token.items():
            try:
                bid, bid_qty = top_of_book(record.get(self.bid_key), "bid", token)
                ask, ask_qty = top_of_book(record.get(self.ask_key), "ask", token)
            except PriceError as exc:
                raise QuoteError(f"token {token}: unreadable price ({exc})") from exc
            if bid <= 0 or ask <= 0:
                raise QuoteError(
                    f"token {token}: no two-sided market (bid={bid}, ask={ask}). "
                    "An empty side means there is nothing to trade against."
                )
            raw_values[token] = (bid, ask)
            sizes[token] = (bid_qty, ask_qty)

        return self._finish(raw_values, sizes, at, by_token, source="touchline")

    def from_feed(self, ticks: Dict[str, Any], tokens: List[str],
                  at: Optional[float] = None) -> Dict[str, Quote]:
        """Build quotes from websocket ticks, through the same checks as REST.

        The feed states the price scale per message in tag 399. It is used as a
        candidate and still verified against the contract's price band, exactly
        as the scrip master's divisor is.
        """
        at = time.monotonic() if at is None else at
        raw_values: Dict[str, Tuple[Decimal, Decimal]] = {}
        sizes: Dict[str, Tuple[Optional[int], Optional[int]]] = {}
        moved: Dict[str, float] = {}
        declared: List[Decimal] = []

        for token in (str(t) for t in tokens):
            tick = ticks.get(token)
            if tick is None:
                raise QuoteError(f"no live tick for token {token}")
            try:
                bid, ask = D(tick.bid), D(tick.ask)
            except PriceError as exc:
                raise QuoteError(f"token {token}: unreadable live price ({exc})") from exc
            if bid <= 0 or ask <= 0:
                raise QuoteError(
                    f"token {token}: no two-sided market on the live feed "
                    f"(bid={bid}, ask={ask})")
            raw_values[token] = (bid, ask)
            sizes[token] = (_as_int(tick.bid_qty), _as_int(tick.ask_qty))
            moved[token] = tick.at
            if tick.divisor:
                try:
                    value = D(tick.divisor)
                    if value > 0 and value not in declared:
                        declared.append(value)
                except PriceError:
                    pass

        return self._finish(raw_values, sizes, at,
                            {t: ticks[t].raw for t in raw_values},
                            extra_divisors=tuple(declared), source="live feed",
                            moved=moved)

    def _finish(self, raw_values, sizes, at, by_token,
                extra_divisors: Tuple[Decimal, ...] = (),
                source: str = "touchline",
                moved: Optional[Dict[str, float]] = None) -> Dict[str, Quote]:
        flat = [v for pair in raw_values.values() for v in pair]
        forced = D(self.cfg.price_divisor) if self.cfg.price_divisor else None
        low, high = self._band()
        candidates = tuple(extra_divisors) + tuple(
            d for d in self._divisor_candidates() if d not in extra_divisors)
        divisor = detect_divisor(flat, low, high, forced, candidates)

        known = self._divisors.get(source)
        if known is not None and divisor != known:
            raise QuoteError(
                f"the {source} price scale changed mid-session "
                f"({known} -> {divisor}). Refusing to trade until this is "
                "understood."
            )
        self._divisors[source] = divisor
        self.divisor = divisor

        quotes: Dict[str, Quote] = {}
        for token, (bid, ask) in raw_values.items():
            bid_r = self._snap(token, bid / divisor, "bid")
            ask_r = self._snap(token, ask / divisor, "ask")
            if bid_r >= ask_r:
                raise QuoteError(
                    f"token {token}: crossed or locked book (bid {bid_r} >= ask {ask_r}). "
                    "This is bad data or a halted contract."
                )
            self._check_against_limits(token, bid_r, ask_r)
            bid_qty, ask_qty = sizes[token]
            quotes[token] = Quote(
                token=token, bid=bid_r, ask=ask_r, at=at, divisor=divisor,
                bid_qty=bid_qty, ask_qty=ask_qty, raw=by_token[token],
                moved_at=(moved or {}).get(token),
            )
        return quotes
