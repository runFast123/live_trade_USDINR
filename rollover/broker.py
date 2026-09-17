"""Everything that talks to Choice, wrapped so the rest of the app never sees
a raw API response.

Several parts of the vendor API are undocumented: the field names in the order
book, the shape of the net position record and the market status payload. Each
reader below returns None when it cannot be certain, and None is treated by the
gates as a failure. Nothing here guesses.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .money import D, PriceError, money, to_exchange_units

BUY, SELL = 1, 2

# "01OCT26" is what the Choice scrip master actually writes, so the two digit
# year form comes first. Every other form here is a fallback.
_EXPIRY_FORMATS = (
    "%d%b%y", "%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y", "%d-%B-%Y", "%d/%m/%Y",
    "%d%b%Y", "%d-%m-%Y", "%Y/%m/%d", "%Y%m%d", "%b %d %Y", "%d %b %Y",
)

# Instrument codes in segment 13. Futures only: OPTCUR and OPTIRC are options
# and must never appear as a leg of this roll.
FUTURES_PREFIX = "FUT"

_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

_ORDER_NO_KEYS = ("GatewayOrderNo", "ExchangeOrderNo", "ClientOrderNo",
                  "OrderNo", "OrderNumber", "NestOrderNumber")
_FILLED_KEYS = ("FilledQty", "TradedQty", "FilledQuantity", "ExecutedQty",
                "TradedQuantity", "FillQty", "FilledShares")
_STATUS_KEYS = ("OrderStatus", "Status", "OrdStatus", "OrderStatusDesc", "Stat")
_NET_QTY_KEYS = ("NetQty", "NetQuantity", "NetQTY", "Netqty", "NQty")

_FILLED_WORDS = ("complete", "filled", "executed", "traded", "fullyexecuted")
_DEAD_WORDS = ("reject", "cancel", "expired", "lapsed")


class BrokerError(RuntimeError):
    pass


@dataclass
class InstrumentInfo:
    token: str
    symbol: str
    sec_desc: str
    segment: str
    lot_size: int
    expiry: Optional[date]
    instrument: str = ""                      # FUTCUR for currency futures
    price_divisor: Optional[Decimal] = None   # what the exchange scales prices by
    tick: Optional[Decimal] = None            # PriceTick, already unscaled
    tick_units: Optional[Decimal] = None      # PriceTick as the exchange states it
    low_range: Optional[Decimal] = None       # the day's circuit limits, unscaled
    high_range: Optional[Decimal] = None
    row: Dict[str, Any] = field(default_factory=dict)

    def label(self) -> str:
        name = self.sec_desc or self.symbol or self.token
        return f"{name} ({self.token})"

    @property
    def is_futures(self) -> bool:
        return self.instrument.upper().startswith(FUTURES_PREFIX)

    @property
    def is_month_end(self) -> bool:
        """True for the monthly contract, which spells the month in its name.

        USDINR26SEPFUT is the September monthly. USDINR26918FUT and
        USDINR26O01FUT are weeklies, which use the day number instead.
        """
        body = self.sec_desc.upper()
        for prefix in (self.symbol.upper(), "FUT"):
            body = body.replace(prefix, "")
        return any(month in body for month in _MONTHS)


@dataclass
class OrderOutcome:
    """What actually happened to one leg."""
    sent: bool
    filled_qty: int
    requested_qty: int
    order_ref: Optional[str]
    certain: bool          # False means the fill state could not be confirmed
    detail: str
    raw: Any = None

    @property
    def fully_filled(self) -> bool:
        return self.certain and self.filled_qty >= self.requested_qty

    @property
    def unfilled(self) -> bool:
        return self.certain and self.filled_qty == 0


def parse_expiry(value: Any) -> Optional[date]:
    """Read an expiry from a scrip master cell, or None if it is not a date."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in _EXPIRY_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # ISO timestamp such as 2026-09-28T00:00:00
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        try:
            return date(*(int(g) for g in match.groups()))
        except ValueError:
            return None
    return None


def _first(record: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[Any]:
    lowered = {str(k).lower(): k for k in record}
    for key in keys:
        actual = lowered.get(key.lower())
        if actual is not None and record[actual] not in (None, ""):
            return record[actual]
    return None


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(D(value))
    except Exception:
        return None


def _iter_records(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_records(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_records(item)


class Broker:
    """Thin, defensive wrapper over the kkunal ChoiceClient."""

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.client = None

    # ------------------------------------------------------------- connection
    #
    # The login is split into steps so the window can drive it: build the
    # client, try today's saved session, then log in, asking for an OTP only if
    # the broker does not hand one back itself.

    def build_client(self) -> None:
        """Create the API client. No network call happens here."""
        from choice_api import ChoiceClient

        missing = [name for name in ("vendor_id", "api_key", "mobile_no")
                   if not str(getattr(self.cfg, name, "")).strip()]
        if missing:
            raise BrokerError("these are still empty: " + ", ".join(missing))

        self.client = ChoiceClient(
            vendor_id=self.cfg.vendor_id.strip(),
            api_key=self.cfg.api_key.strip(),
            base_url=self.cfg.base_url.strip(),
        )

    def resume(self, session_path: str) -> bool:
        """Reuse a session saved earlier today. False means a fresh login is needed."""
        if self.client is None:
            self.build_client()
        if self.client.load_session(session_path):
            self.log.info("Reused today's saved session.")
            return True
        return False

    def request_otp(self) -> Optional[str]:
        """Start the login and return the OTP if the broker supplies it.

        None means the broker did not hand one back and the operator must type
        the code from their authenticator.
        """
        if self.client is None:
            self.build_client()

        mobile = self.client._get_encoded_mobile(self.cfg.mobile_no.strip())
        first = self.client.request("POST", "api/OpenAPIV1/LoginTOTP",
                                    {"MobileNo": mobile}, require_auth=False)
        if str(first.get("Status", "")).lower() != "success":
            raise BrokerError(f"LoginTOTP was refused: {first}")

        try:
            second = self.client.request("POST", "api/OpenAPIV1/GetClientLoginTOTP",
                                         {"MobileNo": mobile}, require_auth=False)
        except Exception as exc:
            self.log.info(f"The broker did not return an OTP ({exc}). Asking for one.")
            return None

        if str(second.get("Status", "")).lower() != "success" or not second.get("Response"):
            self.log.info("The broker did not return an OTP. Asking for one.")
            return None
        return str(second["Response"]).strip()

    def submit_otp(self, otp: str, session_path: str) -> None:
        """Finish the login with an OTP and make the session usable."""
        if self.client is None:
            raise BrokerError("no client; call request_otp first")
        otp = str(otp).strip()
        if not otp:
            raise BrokerError("no OTP entered")

        mobile = self.client._get_encoded_mobile(self.cfg.mobile_no.strip())
        result = self.client.request("POST", "api/OpenAPIV1/ValidateTOTP",
                                     {"MobileNo": mobile, "OTP": otp}, require_auth=False)
        if str(result.get("Status", "")).lower() != "success":
            raise BrokerError(f"the OTP was not accepted: {result}")

        self._store_session(result.get("Response", {}))
        self._after_login(session_path)

    def _store_session(self, payload: Any) -> None:
        """Pull the session id out of the ValidateTOTP reply."""
        if isinstance(payload, str):
            self.client.session_id = payload
        elif isinstance(payload, dict):
            self.client.session_id = (payload.get("SessionId")
                                      or payload.get("session_id"))
            self.client.access_token = payload.get("AccessToken") or self.cfg.api_key
            self.client.bcast_ip = payload.get("OdinBcastIP")
            port = payload.get("OdinBcastPort")
            if port:
                try:
                    self.client.bcast_port = int(port)
                except (TypeError, ValueError):
                    self.client.bcast_port = None

        if not self.client.session_id:
            raise BrokerError("the login succeeded but no SessionId came back")

    def _after_login(self, session_path: str) -> None:
        self.client.save_session(session_path)
        self.log.info("Login complete.")
        self.load_scrip_master()

    def load_scrip_master(self) -> None:
        if self.client.scrip_master.is_loaded:
            return
        self.log.info("Fetching scrip master...")
        if not self.client.scrip_master.fetch():
            raise BrokerError("could not download the scrip master; contracts cannot "
                              "be verified, so the app will not trade")

    def connect(self, session_path: str) -> None:
        """Whole login in one call, for the command line where nobody can type
        an OTP into a window."""
        self.build_client()
        if self.resume(session_path):
            self.load_scrip_master()
            return

        self.log.info("Logging in...")
        otp = self.request_otp()
        if otp is None:
            raise BrokerError(
                "the broker did not return an OTP, so this needs the login window. "
                "Run the app without --find and log in there.")
        self.submit_otp(otp, session_path)

    @property
    def logged_in(self) -> bool:
        return bool(self.client and self.client.session_id)

    # ------------------------------------------------------------ instruments
    def _scaled(self, row: Dict[str, Any], column: str,
                divisor: Optional[Decimal]) -> Optional[Decimal]:
        """Read a price column and undo the exchange's PriceDivisor."""
        raw = row.get(column)
        if raw in (None, "") or divisor in (None, 0):
            return None
        try:
            return (D(raw) / divisor).quantize(Decimal("0.000001"))
        except Exception:
            return None

    def instrument(self, token: str) -> InstrumentInfo:
        row = self.client.scrip_master.get_details(str(token))
        if not row:
            raise BrokerError(f"token {token} is not in today's scrip master")

        if self.cfg.expiry_field:
            expiry_raw = row.get(self.cfg.expiry_field)
        else:
            expiry_raw = None
            for key in row:
                if "expiry" in str(key).lower():
                    expiry_raw = row[key]
                    if parse_expiry(expiry_raw) is not None:
                        break

        divisor = None
        raw_divisor = row.get("PriceDivisor")
        if raw_divisor not in (None, ""):
            try:
                candidate = D(raw_divisor)
                if candidate > 0:
                    divisor = candidate
            except Exception:
                divisor = None

        return InstrumentInfo(
            token=str(token),
            symbol=str(row.get("Symbol", "")).strip(),
            sec_desc=str(row.get("SecDesc", "")).strip(),
            segment=str(row.get("Segment", "")).strip(),
            lot_size=self.client.scrip_master.get_lot_size(str(token)),
            expiry=parse_expiry(expiry_raw),
            instrument=str(row.get("Instrument", "")).strip(),
            price_divisor=divisor,
            tick=self._scaled(row, "PriceTick", divisor),
            tick_units=(D(row["PriceTick"]) if row.get("PriceTick") else None),
            low_range=self._scaled(row, "LowPriceRange", divisor),
            high_range=self._scaled(row, "HighPriceRange", divisor),
            row=row,
        )

    def search(self, name: str, futures_only: bool = True) -> List[Dict[str, Any]]:
        """Look up contracts so the operator can choose the two legs.

        Options are filtered out by default: segment 13 holds thousands of
        OPTCUR rows and not one of them is a leg of this roll.
        """
        hits = self.client.scrip_master.search(name)
        wanted = str(self.cfg.segment_id)
        rows = [h for h in hits if h.get("Segment") == wanted] or hits

        out = []
        seen = set()
        for hit in rows:
            token = hit.get("Token")
            if not token or token in seen:
                continue
            seen.add(token)
            info = self.instrument(token)
            if futures_only and not info.is_futures:
                continue
            out.append({
                "Token": info.token,
                "SecDesc": info.sec_desc or info.symbol,
                "Segment": info.segment,
                "Instrument": info.instrument,
                "Expiry": info.expiry.isoformat() if info.expiry else "?",
                "LotSize": info.lot_size,
                "Kind": "monthly" if info.is_month_end else "weekly",
                "Tick": str(info.tick) if info.tick else "?",
            })
        out.sort(key=lambda r: (r["Expiry"] == "?", r["Expiry"]))
        return out

    # ---------------------------------------------------------- market status
    #
    # MarketStatus nests the segment as a dictionary KEY, not as a field:
    #
    #   Response.lstMktStatus["13"]["1"] == {"MktType": 1, "Status": "1"}
    #
    # where the inner key is the market type, 1 being the normal market, and a
    # Status of "1" means open.

    NORMAL_MARKET = "1"
    OPEN_STATUS = "1"

    def _status_table(self, node: Any) -> Optional[Dict[str, Any]]:
        """Find the segment keyed status table anywhere in the response."""
        if isinstance(node, dict):
            table = node.get("lstMktStatus")
            if isinstance(table, dict):
                return table
            # Fall back to any dict that looks like {segment: {mkttype: {...}}}
            if node and all(str(k).isdigit() and isinstance(v, dict)
                            for k, v in node.items()):
                return node
            for value in node.values():
                found = self._status_table(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = self._status_table(item)
                if found is not None:
                    return found
        return None

    def market_open(self) -> Optional[bool]:
        """True, False, or None when the response cannot be read confidently."""
        try:
            resp = self.client.market.get_market_status()
        except Exception as exc:
            self.log.warn(f"Market status call failed: {exc}")
            return None

        table = self._status_table(resp)
        if table is None:
            self.log.warn("Market status came back in a shape this app cannot read.")
            return None

        segment = table.get(str(self.cfg.segment_id))
        if not isinstance(segment, dict):
            self.log.warn(f"Market status has no entry for segment {self.cfg.segment_id}.")
            return None

        normal = segment.get(self.NORMAL_MARKET)
        if not isinstance(normal, dict) or "Status" not in normal:
            self.log.warn("Market status has no normal market entry for this segment.")
            return None

        # Anything other than the open code counts as closed, never as unknown:
        # a status we can read but do not recognise must not let a trade through.
        return str(normal["Status"]).strip() == self.OPEN_STATUS

    # -------------------------------------------------------------- positions
    def long_qty(self, token: str) -> Optional[int]:
        """Net long units held in one contract, or None when it cannot be read."""
        try:
            resp = self.client.portfolio.get_net_position()
        except Exception as exc:
            self.log.warn(f"Net position call failed: {exc}")
            return None

        token = str(token).strip()
        for record in _iter_records(resp):
            rec_token = _first(record, ("Token", "ScripToken", "InstrumentToken"))
            if rec_token is None or str(rec_token).strip() != token:
                continue

            net = _first(record, _NET_QTY_KEYS)
            if net is not None:
                value = _as_int(net)
                if value is not None:
                    return max(value, 0)

            buy = _as_int(_first(record, ("BuyQty", "BuyQuantity", "TotalBuyQty")) or 0)
            sell = _as_int(_first(record, ("SellQty", "SellQuantity", "TotalSellQty")) or 0)
            if buy is not None and sell is not None:
                return max(buy - sell, 0)
            return None
        return 0  # the position book was readable and this contract is not in it

    # ----------------------------------------------------------------- orders
    def _order_snapshot(self) -> set:
        try:
            resp = self.client.orders.get_order_book()
        except Exception as exc:
            self.log.warn(f"Order book call failed: {exc}")
            return set()
        refs = set()
        for record in _iter_records(resp):
            ref = _first(record, _ORDER_NO_KEYS)
            if ref is not None:
                refs.add(str(ref))
        return refs

    def _find_order(self, token: str, side: int, known_before: set) -> Optional[Dict[str, Any]]:
        """Locate our order in the book. place_order hard-codes ClientOrderNo, so
        the order is identified as the new one on this token and side."""
        try:
            resp = self.client.orders.get_order_book()
        except Exception as exc:
            self.log.warn(f"Order book call failed: {exc}")
            return None

        token = str(token).strip()
        best = None
        for record in _iter_records(resp):
            rec_token = _first(record, ("Token", "ScripToken", "InstrumentToken"))
            if rec_token is None or str(rec_token).strip() != token:
                continue
            ref = _first(record, _ORDER_NO_KEYS)
            if ref is None or str(ref) in known_before:
                continue
            rec_side = _first(record, ("BS", "BuySell", "TransactionType", "Side"))
            if rec_side is not None:
                text = str(rec_side).strip().lower()
                is_sell = text in ("2", "s", "sell")
                if is_sell != (side == SELL):
                    continue
            best = record
        return best

    def read_fill(self, record: Dict[str, Any], requested: int) -> Tuple[Optional[int], str]:
        """Filled quantity and a human status, or (None, reason) when unreadable."""
        status_raw = _first(record, _STATUS_KEYS)
        status = re.sub(r"[^a-z]", "", str(status_raw).lower()) if status_raw else ""

        filled = _as_int(_first(record, _FILLED_KEYS))
        if filled is not None:
            return filled, status or "unknown status"

        if any(word in status for word in _FILLED_WORDS):
            return requested, status
        if any(word in status for word in _DEAD_WORDS):
            return 0, status
        return None, (f"order book has no filled-quantity field and status {status_raw!r} "
                      "is not conclusive")

    def exchange_price(self, info: InstrumentInfo, rupees: Decimal) -> Decimal:
        """Turn a rupee price into the integer the order API wants.

        Refuses rather than rounds. A price that does not land exactly on the
        exchange's tick grid is a bug upstream, and sending a rounded version of
        it would hide that.
        """
        if info.price_divisor is None:
            raise BrokerError(
                f"{info.label()} has no PriceDivisor in the scrip master, so the "
                "order price cannot be scaled. Refusing to send.")
        try:
            units = to_exchange_units(rupees, info.price_divisor)
        except PriceError as exc:
            raise BrokerError(f"{info.label()}: {exc}") from exc

        if info.tick_units and units % info.tick_units != 0:
            raise BrokerError(
                f"{info.label()}: {rupees} is {units} exchange units, which is not a "
                f"multiple of the tick {info.tick_units}")
        return units

    def place_leg(self, info: InstrumentInfo, side: int, qty: int,
                  limit_price: Decimal, label: str) -> OrderOutcome:
        """Send one limit order and wait for it to fill, cancelling any remainder.

        The API documents only Day validity, so immediate-or-cancel is built here:
        place, watch for fill_timeout_sec, then cancel whatever is left. The
        outcome reports exactly how much was filled, and says so plainly when
        that could not be confirmed.
        """
        token = info.token
        units = self.exchange_price(info, limit_price)
        side_word = "SELL" if side == SELL else "BUY"
        summary = (f"{label}: {side_word} {qty} of {info.label()} "
                   f"at {money(limit_price)} (sent as {units})")

        if self.cfg.dry_run:
            self.log.info(f"DRY RUN, not sent -- {summary}")
            return OrderOutcome(sent=False, filled_qty=0, requested_qty=qty,
                                order_ref=None, certain=True, detail="dry run")

        before = self._order_snapshot()
        self.log.info(f"Sending {summary}")
        try:
            resp = self.client.orders.place_order(
                segment_id=self.cfg.segment_id,
                token=int(token),
                order_type=self.cfg.order_type,
                bs=side,
                qty=qty,
                price=int(units),
                trigger_price=0,
                validity=self.cfg.validity,
                product_type=self.cfg.product_type,
            )
        except Exception as exc:
            return OrderOutcome(sent=False, filled_qty=0, requested_qty=qty,
                                order_ref=None, certain=True,
                                detail=f"order rejected before reaching the exchange: {exc}")

        self.log.info(f"{label} response: {resp}")

        deadline = time.monotonic() + self.cfg.fill_timeout_sec
        record, filled, status = None, None, "not seen in the order book"
        while time.monotonic() < deadline:
            record = self._find_order(token, side, before)
            if record is not None:
                filled, status = self.read_fill(record, qty)
                if filled is not None and filled >= qty:
                    break
            time.sleep(0.25)

        ref = str(_first(record, _ORDER_NO_KEYS)) if record else None

        if record is None:
            return OrderOutcome(sent=True, filled_qty=0, requested_qty=qty, order_ref=None,
                                certain=False, raw=resp,
                                detail="order was accepted but never appeared in the order "
                                       "book; check the terminal before doing anything else")

        if filled is None:
            return OrderOutcome(sent=True, filled_qty=0, requested_qty=qty, order_ref=ref,
                                certain=False, raw=record, detail=status)

        if filled < qty:
            self._cancel(record, info, side, qty - filled, units, label)

        return OrderOutcome(sent=True, filled_qty=filled, requested_qty=qty, order_ref=ref,
                            certain=True, raw=record,
                            detail=f"filled {filled} of {qty} ({status})")

    def _cancel(self, record: Dict[str, Any], info: InstrumentInfo, side: int,
                remainder: int, units: Decimal, label: str) -> None:
        self.log.warn(f"{label}: cancelling the unfilled {remainder} units")
        try:
            self.client.orders.cancel_order(
                client_order_no=_as_int(_first(record, ("ClientOrderNo",))) or 0,
                exchange_order_no=str(_first(record, ("ExchangeOrderNo",)) or ""),
                gateway_order_no=str(_first(record, ("GatewayOrderNo",)) or ""),
                segment_id=self.cfg.segment_id,
                token=int(info.token),
                order_type=self.cfg.order_type,
                bs=side,
                qty=remainder,
                price=int(units),
                trigger_price=0,
                validity=self.cfg.validity,
                product_type=self.cfg.product_type,
            )
            self.log.info(f"{label}: cancel sent")
        except Exception as exc:
            self.log.error(f"{label}: CANCEL FAILED ({exc}). A working order may still "
                           "be live at the exchange -- check the terminal now.")
