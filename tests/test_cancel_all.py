"""Not walking away from live orders.

A Day order outlives this process at the exchange. The app-side synthetic IOC
that would have cancelled it does not: it dies with the process. So closing the
window used to abandon whatever was working, and there was no way to cancel
anything by hand at all.
"""
from __future__ import annotations

import unittest
from datetime import date

from rollover.broker import BUY, SELL, Broker, InstrumentInfo
from rollover.config import RollConfig
from rollover.money import D
from tests.test_execution import ExecutionCase, outcome


def order_row(token="1769", status="PENDING", qty=1000, price=95.94,
              client_no=100000014, side="2"):
    return {"Token": token, "OrderStatus": status, "Qty": qty,
            "TotalQtyRemaining": qty, "Price": price, "BS": side,
            "ClientOrderNo": client_no, "ExchangeOrderNo": "E1",
            "GatewayOrderNo": None, "ErrorString": ""}


def book(rows, status="Success"):
    return {"Status": status, "Response": {"Orders": list(rows)}, "Reason": ""}


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
    def __init__(self, response, cancel=None, raises=False):
        self.response = response
        self.cancel_response = cancel or {"Status": "Success"}
        self.raises = raises
        self.cancelled = []

    def get_order_book(self):
        if self.raises:
            raise RuntimeError("no network")
        return self.response

    def cancel_order(self, **kw):
        self.cancelled.append(kw)
        if callable(self.cancel_response):
            return self.cancel_response(kw)
        return self.cancel_response


def broker_with(orders, **cfg_kw):
    cfg_kw.setdefault("dry_run", False)
    log = StubLog()
    b = Broker(RollConfig(near_token="1769", far_token="1584", **cfg_kw), log)
    b.client = type("C", (), {"orders": orders})()
    b.instrument = lambda token: InstrumentInfo(
        token=str(token), symbol="USDINR", sec_desc="USDINR26SEPFUT",
        segment="13", lot_size=1000, expiry=date(2026, 9, 28),
        instrument="FUTCUR", price_divisor=D("10000000"), tick=D("0.0025"),
        tick_units=D("25000"))
    return b, log


class TestFindingWhatIsLive(unittest.TestCase):
    def test_a_working_order_is_found(self):
        b, _ = broker_with(StubOrders(book([order_row()])))
        self.assertEqual(len(b.working_orders()), 1)

    def test_finished_orders_are_not_listed(self):
        rows = [order_row(status=s) for s in
                ("REJECTED", "CANCELLED", "EXECUTED", "ORDER ERROR", "FROZEN")]
        b, _ = broker_with(StubOrders(book(rows)))
        self.assertEqual(b.working_orders(), [])

    def test_a_partial_fill_is_still_live(self):
        b, _ = broker_with(StubOrders(book([order_row(status="PartiallyFilled")])))
        self.assertEqual(len(b.working_orders()), 1)

    def test_other_contracts_are_left_alone(self):
        """Cancelling somebody else's order in another scrip is not our job."""
        b, _ = broker_with(StubOrders(book([order_row(token="1769"),
                                            order_row(token="9999")])))
        found = b.working_orders(["1769", "1584"])
        self.assertEqual([r["Token"] for r in found], ["1769"])

    def test_an_unreadable_book_is_none_not_empty(self):
        """"I could not look" must not read as "there is nothing there"."""
        b, _ = broker_with(StubOrders(None, raises=True))
        self.assertIsNone(b.working_orders())

    def test_a_refused_book_is_none_too(self):
        b, _ = broker_with(StubOrders(book([], status="Fail")))
        self.assertIsNone(b.working_orders())


class TestCancellingOne(unittest.TestCase):
    def test_the_price_is_scaled_back_to_exchange_units(self):
        """The book echoes rupees; the cancel has to send exchange units.

        The probe sent 930600000 and the book came back 93.06, so a cancel
        built from the row has to multiply by the divisor again.
        """
        orders = StubOrders(book([]))
        b, _ = broker_with(orders)
        self.assertTrue(b.cancel_record(order_row(price=95.94)))
        self.assertEqual(orders.cancelled[0]["price"], 959400000)

    def test_it_cancels_what_is_left_not_the_original_size(self):
        orders = StubOrders(book([]))
        b, _ = broker_with(orders)
        row = order_row(qty=1000)
        row["TotalQtyRemaining"] = 300
        b.cancel_record(row)
        self.assertEqual(orders.cancelled[0]["qty"], 300)

    def test_the_side_is_taken_from_the_row(self):
        orders = StubOrders(book([]))
        b, _ = broker_with(orders)
        b.cancel_record(order_row(side="2"))
        b.cancel_record(order_row(side="1"))
        self.assertEqual([c["bs"] for c in orders.cancelled], [SELL, BUY])

    def test_a_refusal_is_reported_not_swallowed(self):
        orders = StubOrders(book([]), cancel={"Status": "Fail",
                                              "Response": "Invalid Exchange Order Number"})
        b, log = broker_with(orders)
        self.assertFalse(b.cancel_record(order_row()))
        self.assertIn("cancel refused", log.text())

    def test_an_exception_is_reported_not_raised(self):
        class Boom(StubOrders):
            def cancel_order(self, **kw):
                raise RuntimeError("gateway down")

        b, log = broker_with(Boom(book([])))
        self.assertFalse(b.cancel_record(order_row()))
        self.assertIn("cancel failed", log.text())

    def test_a_row_with_no_token_is_skipped_rather_than_guessed_at(self):
        b, log = broker_with(StubOrders(book([])))
        self.assertFalse(b.cancel_record({"OrderStatus": "PENDING"}))
        self.assertIn("no token", log.text())


class TestCancelAll(ExecutionCase):
    def engine_with_book(self, rows, cancel=None, raises=False, **cfg_kw):
        engine = self.build([outcome(1000, 1000)], **cfg_kw)
        orders = StubOrders(book(rows), cancel=cancel, raises=raises)
        real, _ = broker_with(orders, **{"dry_run": self.cfg.dry_run})
        engine.broker.working_orders = real.working_orders
        engine.broker.cancel_record = real.cancel_record
        return engine, orders

    def test_every_working_order_is_cancelled(self):
        engine, orders = self.engine_with_book(
            [order_row(token="1769"), order_row(token="1584", side="1")])
        cancelled, failed, problem = engine.cancel_all()
        self.assertEqual((cancelled, failed, problem), (2, 0, None))
        self.assertEqual(len(orders.cancelled), 2)

    def test_nothing_working_cancels_nothing(self):
        engine, orders = self.engine_with_book([])
        self.assertEqual(engine.cancel_all(), (0, 0, None))
        self.assertEqual(orders.cancelled, [])

    def test_a_failure_is_counted_rather_than_hidden(self):
        engine, _ = self.engine_with_book(
            [order_row(), order_row(token="1584")],
            cancel=lambda kw: ({"Status": "Success"} if kw["token"] == 1769
                               else {"Status": "Fail", "Response": "no"}))
        cancelled, failed, problem = engine.cancel_all()
        self.assertEqual((cancelled, failed), (1, 1))
        self.assertIsNone(problem)

    def test_an_unreadable_book_is_reported_as_a_problem(self):
        engine, _ = self.engine_with_book([], raises=True)
        cancelled, failed, problem = engine.cancel_all()
        self.assertEqual((cancelled, failed), (0, 0))
        self.assertIn("could not be read", problem)

    def test_dry_run_sends_no_cancel(self):
        engine, orders = self.engine_with_book([order_row()], dry_run=True)
        self.assertEqual(engine.cancel_all(), (0, 0, None))
        self.assertEqual(orders.cancelled, [])


class TestShutdown(ExecutionCase):
    def engine_with_book(self, rows, cancel=None, raises=False, **cfg_kw):
        engine = self.build([outcome(1000, 1000)], **cfg_kw)
        orders = StubOrders(book(rows), cancel=cancel, raises=raises)
        real, _ = broker_with(orders, **{"dry_run": self.cfg.dry_run})
        engine.broker.working_orders = real.working_orders
        engine.broker.cancel_record = real.cancel_record
        return engine, orders

    def test_closing_cancels_what_is_working(self):
        engine, orders = self.engine_with_book([order_row()])
        self.assertIsNone(engine.shutdown())
        self.assertEqual(len(orders.cancelled), 1)

    def test_closing_disarms(self):
        engine, _ = self.engine_with_book([])
        engine.arm()
        engine.shutdown()
        self.assertFalse(engine.armed)

    def test_a_failed_cancel_is_reported_back_to_the_window(self):
        engine, _ = self.engine_with_book(
            [order_row()], cancel={"Status": "Fail", "Response": "no"})
        problem = engine.shutdown()
        self.assertIn("could not be cancelled", problem)
        self.assertIn("ALERT", self.log.text())

    def test_an_unreadable_book_is_reported_back(self):
        engine, _ = self.engine_with_book([], raises=True)
        self.assertIn("not known whether", engine.shutdown())

    def test_a_clean_close_says_nothing(self):
        engine, _ = self.engine_with_book([])
        self.assertIsNone(engine.shutdown())

    def test_it_still_stops_the_engine_when_cancelling_blows_up(self):
        """The window must always be able to close."""
        engine, _ = self.engine_with_book([])

        def boom(*_a, **_kw):
            raise RuntimeError("everything is on fire")

        engine.broker.working_orders = boom
        problem = engine.shutdown()
        self.assertIn("on fire", problem)
        self.assertTrue(engine._stop.is_set(), "the engine was left running")


if __name__ == "__main__":
    unittest.main(verbosity=2)
