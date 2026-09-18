"""Tests for placing a single order and reading back what happened to it.

The vendor hard-codes ClientOrderNo, so an order cannot be identified by an id
we chose. It is found instead as the new one on that token and side, and these
tests pin that down along with the fill reading, which is the part the app
refuses to guess at.
"""
from __future__ import annotations

import unittest
from datetime import date

from rollover.broker import SELL, Broker, BrokerError, InstrumentInfo
from rollover.config import RollConfig
from rollover.money import D


class StubLog:
    def __init__(self):
        self.lines = []

    def info(self, m): self.lines.append(("INFO", m))
    def warn(self, m): self.lines.append(("WARN", m))
    def error(self, m): self.lines.append(("ERROR", m))
    def alert(self, m): self.lines.append(("ALERT", m))

    def text(self):
        return "\n".join(f"{a} {b}" for a, b in self.lines)


class StubOrders:
    def __init__(self, books, response=None):
        self.books = list(books)      # one order book per get_order_book call
        self.response = response or {"Status": "Success"}
        self.placed = []
        self.cancelled = []

    def get_order_book(self):
        if len(self.books) > 1:
            return self.books.pop(0)
        return self.books[0] if self.books else {"Response": []}

    def place_order(self, **kw):
        self.placed.append(kw)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def cancel_order(self, **kw):
        self.cancelled.append(kw)
        return {"Status": "Success"}


class StubClient:
    def __init__(self, orders):
        self.orders = orders


def instrument(token="1769", divisor="10000000", tick_units="25000"):
    return InstrumentInfo(
        token=token, symbol="USDINR", sec_desc="USDINR26SEPFUT", segment="13",
        lot_size=1000, expiry=date(2026, 9, 28), instrument="FUTCUR",
        price_divisor=D(divisor) if divisor else None,
        tick=D("0.0025"), tick_units=D(tick_units) if tick_units else None,
        low_range=D("93"), high_range=D("99"))


def order_row(token="1769", side="2", filled=1000, status="Complete", ref="A1"):
    return {"Token": token, "BS": side, "GatewayOrderNo": ref,
            "ExchangeOrderNo": ref, "ClientOrderNo": 123456,
            "FilledQty": filled, "OrderStatus": status}


def broker_with(orders, **cfg_kw):
    cfg_kw.setdefault("dry_run", False)
    cfg_kw.setdefault("fill_timeout_sec", 0.4)
    cfg = RollConfig(**cfg_kw)
    log = StubLog()
    b = Broker(cfg, log)
    b.client = StubClient(orders)
    return b, log


class TestExchangePrice(unittest.TestCase):
    def test_rupees_become_exchange_units(self):
        b, _ = broker_with(StubOrders([]))
        self.assertEqual(b.exchange_price(instrument(), D("95.9400")),
                         D("959400000"))

    def test_an_off_grid_price_is_refused(self):
        b, _ = broker_with(StubOrders([]))
        with self.assertRaises(BrokerError):
            b.exchange_price(instrument(), D("95.9410"))

    def test_a_contract_without_a_divisor_is_refused(self):
        b, _ = broker_with(StubOrders([]))
        with self.assertRaises(BrokerError):
            b.exchange_price(instrument(divisor=None), D("95.9400"))


class TestDryRun(unittest.TestCase):
    def test_nothing_is_sent(self):
        orders = StubOrders([])
        b, log = broker_with(orders, dry_run=True)
        out = b.place_leg(instrument(), SELL, 1000, D("95.9400"), "leg 1")

        self.assertEqual(orders.placed, [])
        self.assertFalse(out.sent)
        self.assertEqual(out.detail, "dry run")
        self.assertIn("DRY RUN", log.text())

    def test_a_bad_price_is_still_caught_in_dry_run(self):
        """Dry run must not hide a price the exchange would reject."""
        b, _ = broker_with(StubOrders([]), dry_run=True)
        with self.assertRaises(BrokerError):
            b.place_leg(instrument(), SELL, 1000, D("95.9410"), "leg 1")


class TestPlacingAnOrder(unittest.TestCase):
    def test_a_filled_order_is_reported(self):
        orders = StubOrders([{"Response": []}, {"Response": [order_row()]}])
        b, _ = broker_with(orders)
        out = b.place_leg(instrument(), SELL, 1000, D("95.9400"), "leg 1")

        self.assertTrue(out.sent)
        self.assertTrue(out.certain)
        self.assertEqual(out.filled_qty, 1000)
        self.assertTrue(out.fully_filled)

    def test_the_price_is_sent_in_exchange_units(self):
        orders = StubOrders([{"Response": []}, {"Response": [order_row()]}])
        b, _ = broker_with(orders)
        b.place_leg(instrument(), SELL, 1000, D("95.9400"), "leg 1")
        self.assertEqual(orders.placed[0]["price"], 959400000)
        self.assertEqual(orders.placed[0]["bs"], SELL)
        self.assertEqual(orders.placed[0]["qty"], 1000)

    def test_an_order_already_in_the_book_is_not_mistaken_for_ours(self):
        """ClientOrderNo is hard-coded by the vendor, so ours is the new row."""
        existing = order_row(ref="OLD", filled=500)
        orders = StubOrders([
            {"Response": [existing]},                       # snapshot before
            {"Response": [existing, order_row(ref="NEW", filled=1000)]},
        ])
        b, _ = broker_with(orders)
        out = b.place_leg(instrument(), SELL, 1000, D("95.9400"), "leg 1")
        self.assertEqual(out.order_ref, "NEW")
        self.assertEqual(out.filled_qty, 1000)

    def test_an_order_on_the_other_side_is_ignored(self):
        orders = StubOrders([
            {"Response": []},
            {"Response": [order_row(ref="BUYSIDE", side="1", filled=1000)]},
        ])
        b, _ = broker_with(orders)
        out = b.place_leg(instrument(), SELL, 1000, D("95.9400"), "leg 1")
        self.assertFalse(out.certain)       # ours never appeared

    def test_a_rejected_order_is_reported_not_raised(self):
        orders = StubOrders([{"Response": []}], response=Exception("margin"))
        b, _ = broker_with(orders)
        out = b.place_leg(instrument(), SELL, 1000, D("95.9400"), "leg 1")

        self.assertFalse(out.sent)
        self.assertTrue(out.certain)
        self.assertIn("rejected", out.detail)

    def test_an_order_that_never_appears_is_not_assumed_filled(self):
        orders = StubOrders([{"Response": []}, {"Response": []}])
        b, _ = broker_with(orders)
        out = b.place_leg(instrument(), SELL, 1000, D("95.9400"), "leg 1")

        self.assertFalse(out.certain)
        self.assertEqual(out.filled_qty, 0)
        self.assertIn("never appeared", out.detail)

    def test_a_partial_fill_cancels_the_remainder(self):
        orders = StubOrders([
            {"Response": []},
            {"Response": [order_row(filled=300, status="PartiallyFilled")]},
        ])
        b, _ = broker_with(orders)
        out = b.place_leg(instrument(), SELL, 1000, D("95.9400"), "leg 1")

        self.assertEqual(out.filled_qty, 300)
        self.assertFalse(out.fully_filled)
        self.assertEqual(len(orders.cancelled), 1)
        self.assertEqual(orders.cancelled[0]["qty"], 700)

    def test_a_full_fill_cancels_nothing(self):
        orders = StubOrders([{"Response": []}, {"Response": [order_row()]}])
        b, _ = broker_with(orders)
        b.place_leg(instrument(), SELL, 1000, D("95.9400"), "leg 1")
        self.assertEqual(orders.cancelled, [])


class TestReadingFills(unittest.TestCase):
    def setUp(self):
        self.b, self.log = broker_with(StubOrders([]))

    def test_an_explicit_filled_quantity_wins(self):
        filled, _ = self.b.read_fill({"FilledQty": 250, "OrderStatus": "x"}, 1000)
        self.assertEqual(filled, 250)

    def test_a_complete_status_without_a_quantity_means_all_of_it(self):
        filled, _ = self.b.read_fill({"OrderStatus": "Complete"}, 1000)
        self.assertEqual(filled, 1000)

    def test_a_rejected_status_means_none_of_it(self):
        for word in ("Rejected", "Cancelled", "Expired"):
            filled, _ = self.b.read_fill({"OrderStatus": word}, 1000)
            self.assertEqual(filled, 0, msg=word)

    def test_an_unreadable_status_is_not_guessed(self):
        filled, why = self.b.read_fill({"OrderStatus": "Something New"}, 1000)
        self.assertIsNone(filled)
        self.assertIn("not conclusive", why)

    def test_a_record_with_nothing_useful_is_not_guessed(self):
        filled, _ = self.b.read_fill({"Token": "1769"}, 1000)
        self.assertIsNone(filled)


class TestCancelFailure(unittest.TestCase):
    def test_a_failed_cancel_is_shouted_about(self):
        class Exploding(StubOrders):
            def cancel_order(self, **kw):
                raise Exception("gateway down")

        orders = Exploding([
            {"Response": []},
            {"Response": [order_row(filled=300, status="PartiallyFilled")]},
        ])
        b, log = broker_with(orders)
        b.place_leg(instrument(), SELL, 1000, D("95.9400"), "leg 1")
        self.assertIn("CANCEL FAILED", log.text())
        self.assertIn("check the terminal", log.text())


class TestFillAfterCancel(unittest.TestCase):
    """The quantity that matters is the one after the cancel has landed.

    place_leg used to read the fill, fire the cancel, then return the
    PRE-cancel number. Anything that traded in that gap was invisible, so the
    far leg was sized too small and the account ended up half rolled. At one
    lot this never bites; at twenty it does.
    """

    # The stub serves one book per get_order_book call and then repeats the
    # last. The poll loop consumes two before the cancel, so the settled state
    # has to be the final entry.
    PARTIAL = {"Response": [order_row(filled=300, status="PartiallyFilled")]}

    def test_a_fill_landing_during_the_cancel_is_picked_up(self):
        # Poll sees 300. Then the rest trades before the cancel takes effect.
        orders = StubOrders([
            {"Response": []},                                   # snapshot before
            self.PARTIAL,                                       # first poll
            self.PARTIAL,                                       # second poll
            {"Response": [order_row(filled=1000, status="Complete")]},
        ])
        b, log = broker_with(orders)
        out = b.place_leg(instrument(), SELL, 1000, D("95.9400"), "leg 1")

        self.assertEqual(out.filled_qty, 1000)
        self.assertTrue(out.certain)
        self.assertIn("more filled between the last poll", log.text())

    def test_a_cancelled_order_settles_at_what_it_actually_got(self):
        orders = StubOrders([
            {"Response": []},
            self.PARTIAL,
            self.PARTIAL,
            {"Response": [order_row(filled=400, status="Cancelled")]},
        ])
        b, _ = broker_with(orders)
        out = b.place_leg(instrument(), SELL, 1000, D("95.9400"), "leg 1")

        self.assertEqual(out.filled_qty, 400)
        self.assertTrue(out.certain)

    def test_an_unreadable_settlement_is_not_assumed(self):
        """If the final quantity cannot be read, the outcome is unknown."""
        orders = StubOrders([
            {"Response": []},
            self.PARTIAL,
            self.PARTIAL,
            {"Response": []},                                   # order vanished
        ])
        b, _ = broker_with(orders)
        out = b.place_leg(instrument(), SELL, 1000, D("95.9400"), "leg 1")

        self.assertFalse(out.certain)
        self.assertIn("could not be read", out.detail)
        self.assertIn("At least 300", out.detail)

    def test_a_full_fill_never_reaches_the_settle_path(self):
        orders = StubOrders([{"Response": []},
                             {"Response": [order_row(filled=1000)]}])
        b, _ = broker_with(orders)
        out = b.place_leg(instrument(), SELL, 1000, D("95.9400"), "leg 1")
        self.assertEqual(out.filled_qty, 1000)
        self.assertEqual(orders.cancelled, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
