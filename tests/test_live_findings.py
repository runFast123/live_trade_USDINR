"""What the live probe of 18 September 2026 actually taught us.

One real order was placed on the account: BUY 1 of USDINR26SEPFUT at 93.0600,
the contract's lower circuit. The exchange rejected it, and the rejection was
more informative than a fill would have been.

Everything in this file is taken verbatim from
``data/probe-2026-09-18_111951.json``. These are not invented fixtures; each
one pins a belief the app previously held that the real response contradicted.
"""
from __future__ import annotations

import unittest
from datetime import date

from rollover.broker import (BUY, Broker, InstrumentInfo, call_failed,
                             is_terminal, order_identity, order_reason,
                             status_word)
from rollover.config import RollConfig
from rollover.money import D

# The order book row, exactly as it came back.
REJECTED_ROW = {
    "SegmentId": 13, "Token": 1769, "Symbol": "USDINR",
    "ClientOrderNo": 100000014, "OrderType": "1", "BS": "1", "Qty": 1,
    "DisclosedQty": 0, "Price": 93.06, "TriggerPrice": 0.0, "Validity": 1,
    "ProductType": "D", "ExchangeOrderNo": "", "ResponseType": 0,
    "Remarks": None, "GatewayOrderNo": None, "Time": "2026-09-18 11:19:27",
    "ErrorString": "exchg not enabled for this acct",
    "OrderStatus": "REJECTED", "SeqNo": 0,
    "ExchangeOrderTime": "1474197567", "TradedQty": 0,
    "TotalQtyRemaining": 0, "LTP": 957800000.0, "InitiatedBy": "WEB",
    "ModifiedBy": "", "GTDDays": 0, "GTDStatus": None,
    "BracketOrderId": None, "BracketGatewayOrderId": None,
    "SLJumpprice": 0.0, "LTPJumpPrice": 0.0, "SLOrderPrice": 0.0,
    "SLTriggerPrice": 0.0, "ProfitOrderPrice": 0.0,
    "BracketOrderStatus": None, "BracketOrderModifyBit": 0,
    "LegIndicator": 0, "AvgBuyPrice": 0.0,
}

PLACE_RESPONSE = {"Status": "Success", "Response": "260918000069382",
                  "Reason": ""}
CANCEL_RESPONSE = {"Status": "Fail", "Response": "Invalid Exchange Order Number",
                   "Reason": "Error"}
ORDER_BOOK = {"Status": "Success", "Response": {"Orders": [REJECTED_ROW]},
              "Reason": ""}
TRADE_BOOK = {"Status": "Success", "Response": {"Trades": []}, "Reason": ""}


class StubLog:
    def __init__(self):
        self.lines = []

    def info(self, m): self.lines.append(("INFO", m))
    def warn(self, m): self.lines.append(("WARN", m))
    def error(self, m): self.lines.append(("ERROR", m))
    def alert(self, m): self.lines.append(("ALERT", m))

    def text(self):
        return "\n".join(f"{a} {b}" for a, b in self.lines)


def instrument():
    return InstrumentInfo(
        token="1769", symbol="USDINR", sec_desc="USDINR26SEPFUT", segment="13",
        lot_size=1000, expiry=date(2026, 9, 28), instrument="FUTCUR",
        price_divisor=D("10000000"), tick=D("0.0025"), tick_units=D("25000"),
        low_range=D("93.06"), high_range=D("98.815"))


class TestSuccessIsNotAcceptance(unittest.TestCase):
    """place_order said Success. The exchange rejected the order anyway."""

    def test_the_place_response_reported_success(self):
        self.assertIsNone(call_failed(PLACE_RESPONSE))

    def test_and_the_order_was_rejected_regardless(self):
        self.assertEqual(status_word(REJECTED_ROW), "rejected")

    def test_so_a_fill_may_never_be_inferred_from_the_place_response(self):
        """The two disagree, which is the entire point: only the book counts."""
        self.assertIsNone(call_failed(PLACE_RESPONSE))
        self.assertTrue(is_terminal(REJECTED_ROW))
        filled, _ = Broker(RollConfig(), StubLog()).read_fill(REJECTED_ROW, 1)
        self.assertEqual(filled, 0)


class TestAFailureArrivesAsAnOrdinaryResponse(unittest.TestCase):
    """The cancel failed and raised nothing. The app logged "cancel sent"."""

    def test_the_refusal_is_detected(self):
        self.assertEqual(call_failed(CANCEL_RESPONSE),
                         "Invalid Exchange Order Number")

    def test_a_success_envelope_is_not_a_failure(self):
        self.assertIsNone(call_failed(ORDER_BOOK))
        self.assertIsNone(call_failed(TRADE_BOOK))

    def test_a_non_dict_is_not_treated_as_a_failure(self):
        for value in (None, "", [], 0):
            self.assertIsNone(call_failed(value))


class TestTheReasonReachesThePerson(unittest.TestCase):
    """Without ErrorString the operator sees "filled 0 of 1" and no cause."""

    def test_the_reason_is_read(self):
        self.assertEqual(order_reason(REJECTED_ROW),
                         "exchg not enabled for this acct")

    def test_the_reason_travels_with_the_status(self):
        _, status = Broker(RollConfig(), StubLog()).read_fill(REJECTED_ROW, 1)
        self.assertIn("rejected", status)
        self.assertIn("exchg not enabled", status)

    def test_a_null_remarks_does_not_become_the_reason(self):
        """Remarks is None here; ErrorString must win and None must not print."""
        self.assertNotIn("None", order_reason(REJECTED_ROW))


class TestIdentity(unittest.TestCase):
    """ExchangeOrderNo is empty until the exchange acknowledges the order."""

    def test_a_fresh_order_has_no_exchange_number(self):
        self.assertEqual(REJECTED_ROW["ExchangeOrderNo"], "")
        self.assertIsNone(REJECTED_ROW["GatewayOrderNo"])

    def test_the_broker_assigns_its_own_client_order_number(self):
        """choice_api sends 123456; the book came back with a real sequence."""
        self.assertNotEqual(str(REJECTED_ROW["ClientOrderNo"]), "123456")
        self.assertEqual(order_identity(REJECTED_ROW), "100000014")

    def test_the_identity_does_not_change_once_the_exchange_acknowledges(self):
        acknowledged = dict(REJECTED_ROW, ExchangeOrderNo="1200000045678",
                            OrderStatus="Open")
        self.assertEqual(order_identity(acknowledged),
                         order_identity(REJECTED_ROW))

    def test_the_vendor_placeholder_falls_back_rather_than_colliding(self):
        """If some account does echo 123456, do not call every order the same."""
        a = {"ClientOrderNo": 123456, "ExchangeOrderNo": "AAA"}
        b = {"ClientOrderNo": 123456, "ExchangeOrderNo": "BBB"}
        self.assertNotEqual(order_identity(a), order_identity(b))

    def test_a_row_with_nothing_usable_has_no_identity(self):
        self.assertIsNone(order_identity({"Token": 1769}))


class TestTerminalOrders(unittest.TestCase):
    def test_a_rejected_order_is_finished(self):
        self.assertTrue(is_terminal(REJECTED_ROW))

    def test_a_working_order_is_not(self):
        self.assertFalse(is_terminal(dict(REJECTED_ROW, OrderStatus="Open")))

    def test_a_partial_fill_is_not_finished(self):
        """"PartiallyFilled" contains "filled"; it is still live."""
        for word in ("PartiallyFilled", "Partially Executed", "PARTIAL"):
            self.assertFalse(is_terminal(dict(REJECTED_ROW, OrderStatus=word)),
                             msg=word)

    def test_the_reason_text_cannot_make_an_order_look_cancelled(self):
        """Terminality reads the status field, never the free text beside it."""
        row = dict(REJECTED_ROW, OrderStatus="Open",
                   ErrorString="cannot cancel: order is live")
        self.assertFalse(is_terminal(row))


class TestPriceScaleConfirmed(unittest.TestCase):
    """The strongest positive result of the probe.

    930600000 was sent and the book echoed Price 93.06, so the divisor of
    10,000,000 for USDINR is right. Had rupees been sent, the echo would have
    been 0.0000093.
    """

    def test_what_was_sent_scales_to_what_came_back(self):
        sent = Broker(RollConfig(), StubLog()).exchange_price(
            instrument(), D("93.0600"))
        self.assertEqual(sent, D("930600000"))
        self.assertEqual(sent / D("10000000"), D(str(REJECTED_ROW["Price"])))

    def test_the_books_own_ltp_agrees_with_the_divisor(self):
        """LTP 957800000 against a market bid of 95.78: a second witness."""
        self.assertEqual(D(str(REJECTED_ROW["LTP"])) / D("10000000"),
                         D("95.78"))


class StubOrders:
    """Replays the probe's responses."""

    def __init__(self, books, cancel=None, place=None):
        self.books = list(books)
        self.cancel_response = cancel or {"Status": "Success"}
        self.place_response = place or PLACE_RESPONSE
        self.placed, self.cancelled = [], []

    def get_order_book(self):
        if len(self.books) > 1:
            return self.books.pop(0)
        return self.books[0] if self.books else {"Response": {"Orders": []}}

    def place_order(self, **kw):
        self.placed.append(kw)
        return self.place_response

    def cancel_order(self, **kw):
        self.cancelled.append(kw)
        return self.cancel_response


def broker_with(orders, **kw):
    kw.setdefault("dry_run", False)
    kw.setdefault("fill_timeout_sec", 0.4)
    log = StubLog()
    b = Broker(RollConfig(**kw), log)
    b.client = type("C", (), {"orders": orders})()
    return b, log


class TestPlacingIntoThisAccount(unittest.TestCase):
    """End to end, against the responses the account really gave."""

    def setUp(self):
        self.orders = StubOrders(
            [{"Response": {"Orders": []}}, ORDER_BOOK],
            cancel=CANCEL_RESPONSE)
        self.broker, self.log = broker_with(self.orders)
        self.out = self.broker.place_leg(instrument(), BUY, 1,
                                         D("93.0600"), "leg 1")

    def test_the_rejection_is_certain_not_unknown(self):
        """Nothing filled and nothing is working: that is a definite answer."""
        self.assertTrue(self.out.certain)
        self.assertEqual(self.out.filled_qty, 0)

    def test_the_operator_is_told_why(self):
        self.assertIn("exchg not enabled", self.out.detail)

    def test_a_rejected_order_is_not_cancelled(self):
        """The real cancel was refused with "Invalid Exchange Order Number"."""
        self.assertEqual(self.orders.cancelled, [])
        self.assertIn("nothing to cancel", self.log.text())

    def test_it_does_not_sit_out_the_fill_timeout(self):
        """A refused order will not change. Waiting only delays the alarm."""
        import time
        orders = StubOrders([{"Response": {"Orders": []}}, ORDER_BOOK])
        broker, _ = broker_with(orders, fill_timeout_sec=5.0)

        started = time.monotonic()
        broker.place_leg(instrument(), BUY, 1, D("93.0600"), "leg 1")
        self.assertLess(time.monotonic() - started, 2.0)

    def test_the_order_reference_is_the_brokers_sequence(self):
        self.assertEqual(self.out.order_ref, "100000014")


class TestARefusedCancelIsNotASettledOutcome(unittest.TestCase):
    def test_a_live_order_whose_cancel_fails_is_uncertain(self):
        working = dict(REJECTED_ROW, OrderStatus="Open", TradedQty=0,
                       ErrorString="")
        orders = StubOrders(
            [{"Response": {"Orders": []}},
             {"Status": "Success", "Response": {"Orders": [working]}}],
            cancel=CANCEL_RESPONSE)
        broker, log = broker_with(orders)
        out = broker.place_leg(instrument(), BUY, 1, D("93.0600"), "leg 1")

        self.assertEqual(len(orders.cancelled), 1)
        self.assertFalse(out.certain)
        self.assertIn("may still be live", out.detail)
        self.assertIn("CANCEL REFUSED", log.text())


class TestTheEnvelopeShape(unittest.TestCase):
    """Orders arrive under Response.Orders, trades under Response.Trades."""

    def test_the_order_book_row_is_reachable(self):
        broker, _ = broker_with(StubOrders([ORDER_BOOK]))
        found = broker._find_order("1769", 1, set())
        self.assertIsNotNone(found)
        self.assertEqual(found["ClientOrderNo"], 100000014)

    def test_an_empty_trade_book_yields_no_rows(self):
        from rollover.broker import _iter_records
        rows = [r for r in _iter_records(TRADE_BOOK) if "Token" in r]
        self.assertEqual(rows, [])


class TestWhatIsStillUnknown(unittest.TestCase):
    """The probe did not settle the quantity unit, and must not pretend to.

    The order never reached the exchange's own validation -- it was refused on
    account entitlement first -- so "the book says Qty 1" only tells us the
    broker echoed what we sent. Whether 1 means one contract or one dollar is
    still open, and the app's clip may be 1000x too large.
    """

    def test_the_book_merely_echoed_the_quantity_we_sent(self):
        self.assertEqual(REJECTED_ROW["Qty"], 1)
        self.assertEqual(REJECTED_ROW["TradedQty"], 0)

    def test_the_rejection_was_about_the_account_not_the_order(self):
        reason = order_reason(REJECTED_ROW).lower()
        self.assertIn("not enabled", reason)
        for word in ("quantity", "qty", "lot", "price", "circuit"):
            self.assertNotIn(word, reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
