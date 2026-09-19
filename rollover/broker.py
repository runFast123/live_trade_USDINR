"""Everything that talks to Choice, wrapped so the rest of the app never sees
a raw API response.

Several parts of the vendor API are undocumented: the field names in the order
book, the shape of the net position record and the market status payload. Each
reader below returns None when it cannot be certain, and None is treated by the
gates as a failure. Nothing here guesses.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import timedelta
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .config import IOC
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

# Identity is a separate question from display, and the live probe on 18 Sep
# 2026 showed why. A freshly placed order comes back with GatewayOrderNo null
# and ExchangeOrderNo "", so _ORDER_NO_KEYS resolves it to ClientOrderNo; once
# the exchange acknowledges it, ExchangeOrderNo fills in and the very same
# order answers to a different reference. Anything comparing references across
# two polls needs the field that is there from the first moment and does not
# change.
#
# choice_api hard-codes "ClientOrderNo": 123456 into every place_order payload,
# and the app was written believing the book would echo that back, which would
# make every order look alike. The probe showed the broker overwrites it with
# its own per-account sequence (100000014). That is a single observation, so
# the placeholder is still treated as meaningless and falls back to the old
# behaviour rather than collapsing every order onto one identity.
_IDENTITY_KEYS = ("ClientOrderNo",)
VENDOR_CLIENT_ORDER_NO = "123456"

_FILLED_KEYS = ("FilledQty", "TradedQty", "FilledQuantity", "ExecutedQty",
                "TradedQuantity", "FillQty", "FilledShares")
_STATUS_KEYS = ("OrderStatus", "Status", "OrdStatus", "OrderStatusDesc", "Stat")
_NET_QTY_KEYS = ("NetQty", "NetQuantity", "NetQTY", "Netqty", "NQty")

# The trade book came back empty from the live probe, so its row shape is the
# one thing about it still unconfirmed. These follow the order book's naming,
# which the probe did confirm, with the usual aliases behind them. Anything not
# covered here reads as unknown rather than as zero.
_TRADE_NO_KEYS = ("TradeNo", "TradeNumber", "ExchangeTradeNo", "FillId",
                  "TradeId", "ExchangeOrderNo", "ClientOrderNo")
_TRADE_QTY_KEYS = ("TradedQty", "TradeQty", "FilledQty", "Qty", "Quantity",
                   "TradeQuantity")

# Why an order was refused. The probe's rejection carried
# "exchg not enabled for this acct" here and nowhere else; without it the
# operator sees "filled 0 of 1 (rejected)" and has nothing to act on.
_REASON_KEYS = ("ErrorString", "RejectionReason", "ErrorMessage", "Remarks",
                "Reason")

# The vendor's own status vocabulary, from swagger.json (ChoiceOpenTransactionPusher).
# Kept here as documentation for the word lists below, and asserted against them
# in tests/test_live_findings.py so a change to either is caught.
#
#   CLIENT XMITTED    sent from the client, confirmation awaited
#   GATEWAY XMITTED   sent from the gateway, confirmation awaited
#   OMS XMITTED       sent from the OMS, confirmation awaited
#   EXCHANGE XMITTED  sent to the exchange, confirmation awaited
#   PENDING           confirmed by the exchange, i.e. working
#   CANCELLED         cancelled
#   EXECUTED          traded
#   GATEWAY REJECT    rejected by the gateway
#   OMS REJECT        rejected by the OMS
#   ORDER ERROR       rejected by the exchange
#   FROZEN            rejected by the exchange for breaching NSE's freeze quantity
#   A.ACCEPT / A.REJECT / A.MODIFY / A.CANCEL     admin actions
#   AMO SUBMITTED / AMO CANCELLED                 after-market orders
#
# The OpenAPI order book the app actually reads uses a shorter vocabulary of its
# own ("REJECTED" came back from the live probe), so matching stays on words
# rather than on an exact list.
_FILLED_WORDS = ("complete", "filled", "executed", "traded", "fullyexecuted")

# "error" catches ORDER ERROR and "frozen" catches FROZEN. Neither contains
# "reject", so both were previously read as live orders: the app would try to
# cancel an order the exchange had already thrown out, the cancel would be
# refused, and the roll would halt for no reason. FROZEN matters most, because
# it is the routine answer to a clip larger than NSE's freeze quantity and the
# sane response is to size down, not to stop.
_DEAD_WORDS = ("reject", "cancel", "expired", "lapsed", "error", "frozen")

# "PartiallyFilled" contains "filled" and "partiallyexecuted" contains
# "executed", so a substring test reads a live, partly-filled order as a
# finished one. That would skip the cancel and leave the remainder working at
# the exchange while the app moved on to the second leg.
_PARTIAL_WORDS = ("partial", "partly")


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


def call_failed(resp: Any) -> Optional[str]:
    """The reason a call failed, or None if it did not.

    The vendor client treats any HTTP 200 as success, so a refusal arrives as
    an ordinary return value rather than an exception. The probe's cancel came
    back as {"Status": "Fail", "Response": "Invalid Exchange Order Number",
    "Reason": "Error"} and the code logged "cancel sent". Nothing that matters
    may be decided without looking inside the envelope.
    """
    if not isinstance(resp, dict):
        return None
    status = str(resp.get("Status", "")).strip().lower()
    if not status or status in ("success", "ok", "true"):
        return None
    inner = resp.get("Response")
    detail = inner if isinstance(inner, str) and inner.strip() else resp.get("Reason")
    return str(detail).strip() if detail else f"the broker returned {status!r}"


def order_identity(record: Dict[str, Any]) -> Optional[str]:
    """A reference for this order that will not change between two polls."""
    value = _first(record, _IDENTITY_KEYS)
    if value is not None and str(value).strip() != VENDOR_CLIENT_ORDER_NO:
        return str(value)
    value = _first(record, _ORDER_NO_KEYS)
    return str(value) if value is not None else None


def order_reason(record: Dict[str, Any]) -> str:
    """Whatever the broker said about why an order ended as it did."""
    value = _first(record, _REASON_KEYS)
    return str(value).strip() if value is not None else ""


def status_word(record: Dict[str, Any]) -> str:
    """The order's status, letters only, e.g. "rejected"."""
    raw = _first(record, _STATUS_KEYS)
    return re.sub(r"[^a-z]", "", str(raw).lower()) if raw is not None else ""


def is_terminal(record: Dict[str, Any]) -> bool:
    """True when the order is finished and there is nothing left to cancel.

    Read from the status field alone. The human-readable detail now carries the
    broker's reason text as well, and a reason such as "cannot cancel" must not
    be mistaken for a cancelled order.
    """
    word = status_word(record)
    if any(w in word for w in _PARTIAL_WORDS):
        return False
    return any(w in word for w in _DEAD_WORDS + _FILLED_WORDS)


def _side_matches(record: Dict[str, Any], side: int) -> bool:
    """True when this row is the side we asked about, or does not say."""
    raw = _first(record, ("BS", "BuySell", "TransactionType", "Side"))
    if raw is None:
        return True
    text = str(raw).strip().lower()
    is_sell = text in ("2", "s", "sell", "sl")
    return is_sell == (side == SELL)


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

    SCRIP_URL = ("https://scripmaster.choiceindia.com/scripmaster/"
                 "SCRIP_MASTER_{stamp}.csv")

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.client = None
        # Which day's scrip master is in memory. The file changes every trading
        # day: contracts expire out of it, new ones appear, and every circuit
        # band moves. An app left running overnight must not keep yesterday's.
        self.scrip_loaded_on: Optional[date] = None
        self.scrip_file_date: Optional[date] = None

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

    def account_fingerprint(self) -> str:
        """Which account a session belongs to, without storing the secrets.

        A hash, so nothing identifying is written to a file that sits beside
        the app. It covers everything that decides WHICH account the broker
        will act on: the host, the vendor, the login and the key.
        """
        import hashlib

        parts = "|".join(str(getattr(self.cfg, name, "") or "").strip()
                         for name in ("base_url", "vendor_id", "mobile_no",
                                      "api_key"))
        return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:32]

    def resume(self, session_path: str) -> bool:
        """Reuse a session saved earlier today. False means a fresh login is needed.

        The saved session is checked against the credentials in force now, not
        only against the date. The vendor's loader looks at the date alone, so
        changing the account and restarting on the same day silently reused
        the PREVIOUS account's token: the app would say it was logged in, read
        that account's funds and positions, and send that account's orders,
        while the operator believed they were on the new one. Observed when
        the credentials and the host were both changed mid-afternoon.
        """
        if self.client is None:
            self.build_client()

        mine = self.account_fingerprint()
        try:
            with open(session_path, encoding="utf-8") as handle:
                saved = json.load(handle).get("account")
        except (OSError, ValueError):
            saved = None

        if saved != mine:
            # Unstamped sessions are from a build before this existed. Refuse
            # them too: a fresh login costs one OTP, and being wrong here
            # means trading the wrong account.
            self.log.warn(
                "The saved session does not belong to the account configured "
                "now" + ("" if saved else " (it predates this check)")
                + ". Logging in again rather than reusing it.")
            try:
                os.remove(session_path)
            except OSError:
                pass
            return False

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
        self._stamp_session(session_path)
        self.log.info("Login complete.")
        self.load_scrip_master()

    def _stamp_session(self, session_path: str) -> None:
        """Record which account this session is for, so resume can check."""
        try:
            with open(session_path, encoding="utf-8") as handle:
                saved = json.load(handle)
            saved["account"] = self.account_fingerprint()
            with open(session_path, "w", encoding="utf-8") as handle:
                json.dump(saved, handle, indent=2)
        except (OSError, ValueError) as exc:
            # Not fatal: an unstamped session is refused on the next resume,
            # which costs a login and never the wrong account.
            self.log.warn(f"Could not stamp the session file ({exc}). "
                          "The next start will log in again.")

    def published_scrip_date(self) -> Optional[date]:
        """Which day's scrip master is actually published right now.

        The library's fetch quietly falls back to previous days, so asking
        directly is the only way to know whether what got loaded is current.
        """
        for back in range(3):
            day = date.today() - timedelta(days=back)
            url = self.SCRIP_URL.format(stamp=day.strftime("%d%b%Y"))
            request = urllib.request.Request(
                url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    if 200 <= response.status < 300:
                        return day
            except (urllib.error.URLError, OSError, ValueError):
                continue
        return None

    def load_scrip_master(self, force: bool = False) -> None:
        loaded = self.client.scrip_master.is_loaded
        if loaded and not force and self.scrip_loaded_on == date.today():
            return

        self.log.info("Fetching scrip master...")
        # A refresh replaces the file wholesale rather than merging into a
        # day-old index, which would leave expired contracts behind.
        if force or self.scrip_loaded_on not in (None, date.today()):
            master = self.client.scrip_master
            master.symbol_to_rows.clear()
            master.token_to_details.clear()
            master.all_rows.clear()
            master.is_loaded = False

        if not self.client.scrip_master.fetch():
            raise BrokerError("could not download the scrip master; contracts cannot "
                              "be verified, so the app will not trade")

        self.scrip_loaded_on = date.today()
        self.scrip_file_date = self.published_scrip_date()
        if self.scrip_file_date == date.today():
            self.log.info(f"Scrip master loaded for {self.scrip_file_date}.")
        else:
            self.log.warn(
                f"The newest published scrip master is {self.scrip_file_date}, "
                f"not {date.today()}. Contract details may be a day behind.")

    def refresh_scrip_master_if_stale(self) -> bool:
        """Reload once the calendar day turns over. True when it reloaded."""
        if self.scrip_loaded_on == date.today():
            return False
        self.log.info("The date has changed; reloading the scrip master.")
        self.load_scrip_master(force=True)
        return True

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

    def net_qty(self, token: str) -> Optional[int]:
        """Net position in one contract, signed. None when it cannot be read.

        `long_qty` clamps at zero because the gates only ever ask "do I hold
        enough to sell?". Reconciliation needs the real number, including a
        short, or it cannot tell a half-rolled account from a flat one.
        """
        try:
            resp = self.client.portfolio.get_net_position()
        except Exception as exc:
            self.log.warn(f"Net position call failed: {exc}")
            return None
        if call_failed(resp):
            self.log.warn(f"Net position refused: {call_failed(resp)}")
            return None

        token = str(token).strip()
        for record in _iter_records(resp):
            rec_token = _first(record, ("Token", "ScripToken", "InstrumentToken"))
            if rec_token is None or str(rec_token).strip() != token:
                continue

            net = _first(record, _NET_QTY_KEYS)
            if net is not None:
                return _as_int(net)

            buy = _as_int(_first(record, ("BuyQty", "BuyQuantity", "TotalBuyQty")) or 0)
            sell = _as_int(_first(record, ("SellQty", "SellQuantity", "TotalSellQty")) or 0)
            if buy is not None and sell is not None:
                return buy - sell
            return None
        return 0  # readable, and this contract is simply not held

    # ------------------------------------------------------------ trade book
    def _trade_snapshot(self) -> Optional[set]:
        """Identities of every trade currently in the book, or None if unread.

        None and an empty set mean very different things: "I could not look"
        against "nothing has traded". Returning a set for both would make a
        failed call look like a clean slate, and every trade already done
        today would then count as ours.
        """
        try:
            resp = self.client.orders.get_trade_book()
        except Exception as exc:
            self.log.warn(f"Trade book call failed: {exc}")
            return None
        problem = call_failed(resp)
        if problem:
            self.log.warn(f"Trade book refused: {problem}")
            return None

        found = set()
        for record in _iter_records(resp):
            ref = _first(record, _TRADE_NO_KEYS)
            if ref is not None:
                found.add(str(ref))
        return found

    def traded_since(self, before: Optional[set], token: str,
                     side: int) -> Tuple[Optional[int], str]:
        """Quantity traded on this token and side since the snapshot.

        The trade book is the exchange's record of what actually executed; the
        order book is a summary of what the broker believes. They should agree,
        and this exists so that a disagreement can be seen rather than assumed
        away.
        """
        if before is None:
            return None, "the trade book could not be read before the order"

        try:
            resp = self.client.orders.get_trade_book()
        except Exception as exc:
            return None, f"the trade book could not be read back: {exc}"
        problem = call_failed(resp)
        if problem:
            return None, f"the trade book refused: {problem}"

        token = str(token).strip()
        total = 0
        counted = 0
        for record in _iter_records(resp):
            ref = _first(record, _TRADE_NO_KEYS)
            if ref is None or str(ref) in before:
                continue
            rec_token = _first(record, ("Token", "ScripToken", "InstrumentToken"))
            if rec_token is None or str(rec_token).strip() != token:
                continue
            if not _side_matches(record, side):
                continue
            qty = _as_int(_first(record, _TRADE_QTY_KEYS))
            if qty is None:
                return None, ("a new trade on this contract has no readable "
                              f"quantity; fields were {sorted(str(k) for k in record)}")
            total += qty
            counted += 1

        return total, (f"{counted} new trade(s) totalling {total}" if counted
                       else "no new trades")

    # ------------------------------------------------------------ cancel all
    def working_orders(self, tokens: Optional[List[str]] = None
                       ) -> Optional[List[Dict[str, Any]]]:
        """Every order still live at the exchange, or None if unreadable.

        None means "I could not look", which is not the same as "there are
        none". A caller closing the window has to tell those apart.
        """
        try:
            resp = self.client.orders.get_order_book()
        except Exception as exc:
            self.log.warn(f"Order book call failed: {exc}")
            return None
        if call_failed(resp):
            self.log.warn(f"Order book refused: {call_failed(resp)}")
            return None

        wanted = {str(t).strip() for t in (tokens or []) if t}
        live = []
        for record in _iter_records(resp):
            # The response envelope carries "Status": "Success", which looks
            # exactly like an order status. A token is what tells an order row
            # apart from the wrapper around it.
            token = _first(record, ("Token", "ScripToken", "InstrumentToken"))
            if token is None or _first(record, _STATUS_KEYS) is None:
                continue
            if is_terminal(record):
                continue
            if wanted and str(token).strip() not in wanted:
                continue
            live.append(record)
        return live

    def cancel_record(self, record: Dict[str, Any], label: str = "cancel all") -> bool:
        """Cancel one order described by its own order-book row.

        The row carries the price in rupees -- the probe sent 930600000 and the
        book echoed 93.06 -- so it has to be scaled back up before it is sent
        anywhere.
        """
        token = _first(record, ("Token", "ScripToken", "InstrumentToken"))
        if token is None:
            self.log.warn(f"{label}: an order row has no token; skipping it")
            return False

        try:
            info = self.instrument(str(token).strip())
            price = _first(record, ("Price", "OrderPrice", "LimitPrice")) or 0
            units = int(D(price) * (info.price_divisor or D(1)))
        except Exception as exc:
            self.log.warn(f"{label}: could not price the cancel for {token}: {exc}")
            return False

        qty = _as_int(_first(record, ("TotalQtyRemaining", "Qty", "Quantity"))) or 0
        side_raw = str(_first(record, ("BS", "BuySell", "Side")) or "").strip().lower()
        side = SELL if side_raw in ("2", "s", "sell") else BUY

        try:
            resp = self.client.orders.cancel_order(
                client_order_no=_as_int(_first(record, ("ClientOrderNo",))) or 0,
                exchange_order_no=str(_first(record, ("ExchangeOrderNo",)) or ""),
                gateway_order_no=str(_first(record, ("GatewayOrderNo",)) or ""),
                segment_id=self.cfg.segment_id,
                token=int(D(str(token).strip())),
                order_type=self.cfg.order_type,
                bs=side,
                qty=qty,
                price=units,
                trigger_price=0,
                validity=self.cfg.validity,
                product_type=self.cfg.product_type,
            )
        except Exception as exc:
            self.log.error(f"{label}: cancel failed for {token}: {exc}")
            return False

        problem = call_failed(resp)
        if problem:
            self.log.error(f"{label}: cancel refused for {token}: {problem}")
            return False
        self.log.info(f"{label}: cancelled {qty} on {token}")
        return True

    # ----------------------------------------------------------------- orders
    def _order_snapshot(self) -> set:
        try:
            resp = self.client.orders.get_order_book()
        except Exception as exc:
            self.log.warn(f"Order book call failed: {exc}")
            return set()
        refs = set()
        for record in _iter_records(resp):
            ref = order_identity(record)
            if ref is not None:
                refs.add(ref)
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
            ref = order_identity(record)
            if ref is None or ref in known_before:
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

        # A refusal without its reason is not something anyone can act on. The
        # probe's order came back REJECTED carrying "exchg not enabled for this
        # acct", which is the difference between "the market moved" and "this
        # account cannot trade this segment at all".
        reason = order_reason(record)
        spoken = f"{status}: {reason}" if reason and status else (reason or status)

        filled = _as_int(_first(record, _FILLED_KEYS))
        if filled is not None:
            return filled, spoken or "unknown status"

        if not any(word in status for word in _PARTIAL_WORDS):
            # Without a quantity field, only an all-or-nothing status says
            # anything. "Partially filled" with no number attached is exactly
            # the case the caller must not guess at.
            if any(word in status for word in _FILLED_WORDS):
                return requested, spoken
            if any(word in status for word in _DEAD_WORDS):
                return 0, spoken
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

        # The place response says only that the request was carried; it said
        # "Success" for the probe's order, which the exchange then rejected. A
        # failure here, though, is conclusive: nothing was sent.
        problem = call_failed(resp)
        if problem:
            return OrderOutcome(sent=False, filled_qty=0, requested_qty=qty,
                                order_ref=None, certain=True, raw=resp,
                                detail=f"the broker refused the order: {problem}")

        deadline = time.monotonic() + self.cfg.fill_timeout_sec
        record, filled, status = None, None, "not seen in the order book"
        while time.monotonic() < deadline:
            record = self._find_order(token, side, before)
            if record is not None:
                filled, status = self.read_fill(record, qty)
                if filled is not None and filled >= qty:
                    break
                # Rejected or cancelled: nothing more is going to happen, and
                # sitting out the rest of the timeout only delays telling
                # somebody. The probe spent its whole fill window watching an
                # order the exchange had already refused.
                if is_terminal(record):
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
            cancelled = self._cancel(record, info, side, qty - filled, units, label)
            if not cancelled:
                # The remainder may still be working. Reporting a settled
                # quantity here would let the other leg be sized against a
                # position that is still moving.
                return OrderOutcome(
                    sent=True, filled_qty=filled, requested_qty=qty, order_ref=ref,
                    certain=False, raw=record,
                    detail=(f"filled {filled} of {qty}, but the cancel was refused, "
                            "so the rest may still be live at the exchange. Check "
                            "the terminal before anything else is sent."))

            # A cancel is not instantaneous. Whatever traded between the last
            # poll and the cancel taking effect is absent from `filled`, and
            # sizing the second leg from a stale number is precisely how a half
            # roll gets manufactured. At one lot this never bites; at twenty it
            # does. Re-read once the order has settled and use that.
            settled, settled_status = self._settle_after_cancel(
                token, side, before, qty)

            if settled is None:
                return OrderOutcome(
                    sent=True, filled_qty=filled, requested_qty=qty, order_ref=ref,
                    certain=False, raw=record,
                    detail=("cancelled, but the final filled quantity could not be "
                            f"read ({settled_status}). At least {filled} of {qty} "
                            "filled; check the terminal."))

            if settled != filled:
                self.log.warn(
                    f"{label}: {settled - filled} more filled between the last poll "
                    f"and the cancel taking effect; using {settled}, not {filled}.")
            filled, status = settled, settled_status

        return OrderOutcome(sent=True, filled_qty=filled, requested_qty=qty, order_ref=ref,
                            certain=True, raw=record,
                            detail=f"filled {filled} of {qty} ({status})")

    def _settle_after_cancel(self, token: str, side: int, before: set, qty: int,
                             wait: float = 2.0) -> Tuple[Optional[int], str]:
        """Read the order's final filled quantity once the cancel has landed.

        Returns (None, reason) when it cannot be established, which the caller
        must treat as an unknown outcome rather than assuming the earlier
        reading still holds.
        """
        deadline = time.monotonic() + wait
        last: Optional[int] = None
        last_status = "the order could not be found after cancelling"

        while time.monotonic() < deadline:
            record = self._find_order(token, side, before)
            if record is not None:
                value, status = self.read_fill(record, qty)
                if value is not None:
                    last, last_status = value, status
                    if is_terminal(record) or value >= qty:
                        return value, status
            time.sleep(0.25)

        return last, last_status

    def _cancel(self, record: Dict[str, Any], info: InstrumentInfo, side: int,
                remainder: int, units: Decimal, label: str) -> bool:
        """Cancel the remainder. Returns False when it is not known to be gone.

        Nothing is sent for an order that has already finished. The probe's
        rejected order had an empty ExchangeOrderNo, and cancelling it was
        refused with "Invalid Exchange Order Number" -- a failure the caller
        would otherwise have to treat as a possibly-live order.
        """
        if is_terminal(record):
            self.log.info(
                f"{label}: the order is already {status_word(record)}, "
                "so there is nothing to cancel")
            return True

        if self.cfg.validity == IOC:
            # An IOC order cannot rest, so anything unfilled is already gone
            # and there is nothing for a cancel to act on. Seeing one still
            # open here means the exchange did not treat it as IOC, which is
            # worth stopping for rather than papering over with a cancel.
            self.log.error(
                f"{label}: an immediate-or-cancel order is still {status_word(record)}. "
                "It should not be able to rest. Check the terminal now.")
            return False

        self.log.warn(f"{label}: cancelling the unfilled {remainder} units")
        try:
            resp = self.client.orders.cancel_order(
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
            problem = call_failed(resp)
            if problem:
                self.log.error(
                    f"{label}: CANCEL REFUSED ({problem}). A working order may "
                    "still be live at the exchange -- check the terminal now.")
                return False
            self.log.info(f"{label}: cancel sent")
            return True
        except Exception as exc:
            self.log.error(f"{label}: CANCEL FAILED ({exc}). A working order may still "
                           "be live at the exchange -- check the terminal now.")
            return False
