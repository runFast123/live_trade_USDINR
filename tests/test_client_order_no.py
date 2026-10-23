"""Every order carries its own reference number.

28 Sep 2026, Phase 3.1. The probe's order went to the finx host for the
funded account and the broker answered {"Status": "Success", "Response": ""}.
No order was created: not in the API's order books, not in the broker's
terminal. The same call on 18 Sep, on the finxomne host, answered with an
order number and the order was in the book.

choice_api 1.3.0 sends "ClientOrderNo": 123456 with every order and has no
way to change it. The newer choice_api lists that as a bug. A reference the
broker has already seen is a standard reason for a "Success" that creates
nothing. It is not proven to be the cause, so the second half of the fix is
to make the answer unmissable: an acknowledgement without an order number is
said in so many words, and the leg is still treated as unknown, so the app
halts rather than trading on.
"""
from __future__ import annotations

import threading
import unittest
from datetime import date
from unittest import mock

from rollover import broker as brokerlib
from rollover.broker import (BUY, SELL, Broker, InstrumentInfo,
                             NEW_ORDER_ENDPOINT, CLIENT_NO_CEILING,
                             VENDOR_CLIENT_ORDER_NO, next_client_order_no,
                             order_payload, reply_order_no, send_order)
from rollover.config import RollConfig
from rollover.money import D


class Log:
    def __init__(self):
        self.lines = []

    def info(self, m): self.lines.append(m)
    warn = error = alert = info

    def text(self):
        return " ".join(self.lines)


class Transport:
    """The broker's HTTP layer, recording what would have been sent."""

    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply if reply is not None else {"Status": "Success",
                                                      "Response": "1"}

    def request(self, method, endpoint, data=None, **kw):
        self.calls.append((method, endpoint, data))
        return self.reply


def contract(token="1284"):
    return InstrumentInfo(token=token, symbol="USDINR", sec_desc="USDINR26OCTFUT",
                          segment="13", lot_size=1000, expiry=date(2026, 10, 28),
                          instrument="FUTCUR", price_divisor=D("10000000"),
                          tick=D("0.0025"), tick_units=D("25000"),
                          low_range=D("93.235"), high_range=D("99"))


class TestTheNumber(unittest.TestCase):
    def test_two_orders_never_share_one(self):
        numbers = [next_client_order_no() for _ in range(10_000)]
        self.assertEqual(len(set(numbers)), len(numbers))

    def test_they_only_go_up(self):
        numbers = [next_client_order_no() for _ in range(1_000)]
        self.assertEqual(numbers, sorted(numbers))

    def test_they_fit_the_brokers_integer_field(self):
        for _ in range(1_000):
            number = next_client_order_no()
            self.assertGreater(number, 0)
            self.assertLess(number, CLIENT_NO_CEILING)
            self.assertLess(number, 2 ** 31)

    def test_it_is_never_the_vendors_placeholder(self):
        """The clock can land exactly on 123456 milliseconds past a wrap."""
        with mock.patch.object(brokerlib, "_last_client_no", 0), \
                mock.patch.object(brokerlib.time, "time", return_value=123.456):
            self.assertNotEqual(str(next_client_order_no()),
                                VENDOR_CLIENT_ORDER_NO)

    def test_it_stays_inside_the_ceiling_at_the_wrap(self):
        with mock.patch.object(brokerlib, "_last_client_no",
                               CLIENT_NO_CEILING - 1), \
                mock.patch.object(brokerlib.time, "time", return_value=0.0):
            self.assertLess(next_client_order_no(), CLIENT_NO_CEILING)

    def test_threads_placing_at_once_get_different_numbers(self):
        """The app's engine and a cancel-all can both be sending."""
        seen, lock = [], threading.Lock()

        def take():
            mine = [next_client_order_no() for _ in range(500)]
            with lock:
                seen.extend(mine)

        threads = [threading.Thread(target=take) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(seen)), 8 * 500)


class TestTheRequest(unittest.TestCase):
    def setUp(self):
        self.cfg = RollConfig()

    def test_it_is_the_vendors_request_with_our_number(self):
        """Field for field what choice_api builds, so nothing else changed
        between the order that failed and the one sent now."""
        from choice_api.orders import OrdersAPI

        transport = Transport()
        api = OrdersAPI(transport)
        if brokerlib._accepts(api.place_order, "client_order_no"):
            self.skipTest("this choice_api numbers orders itself")
        api.place_order(segment_id=self.cfg.segment_id, token=1284,
                        order_type=self.cfg.order_type, bs=BUY, qty=1000,
                        price=934325000, trigger_price=0,
                        validity=self.cfg.validity,
                        product_type=self.cfg.product_type)
        vendor = transport.calls[0][2]
        ours = order_payload(self.cfg, "1284", BUY, 1000, 934325000, 777)

        self.assertEqual(set(vendor), set(ours))
        for key in vendor:
            if key != "ClientOrderNo":
                self.assertEqual(vendor[key], ours[key], key)
        self.assertEqual(ours["ClientOrderNo"], 777)

    def test_the_installed_library_is_sent_our_number(self):
        from choice_api.orders import OrdersAPI

        transport = Transport()
        transport.orders = OrdersAPI(transport)
        reply, number = send_order(transport, self.cfg, "1284", BUY, 1000,
                                   934325000)
        method, endpoint, data = transport.calls[0]
        self.assertEqual((method, endpoint), ("POST", NEW_ORDER_ENDPOINT))
        self.assertEqual(data["ClientOrderNo"], number)
        self.assertNotEqual(str(number), VENDOR_CLIENT_ORDER_NO)
        self.assertEqual(reply, transport.reply)

    def test_a_library_that_takes_the_number_is_handed_it(self):
        """choice_api 1.3.1 onwards can be given one; nothing is built here."""
        handed = {}

        class Orders:
            def place_order(self, segment_id, token, order_type, bs, qty, price,
                            trigger_price, validity, product_type,
                            disclosed_qty=0, is_edis_req=False,
                            client_order_no=None):
                handed.update(client_order_no=client_order_no, qty=qty)
                return {"Status": "Success", "Response": "9"}

        transport = Transport()
        transport.orders = Orders()
        _, number = send_order(transport, self.cfg, "1284", SELL, 1000,
                               934325000)
        self.assertEqual(handed["client_order_no"], number)
        self.assertEqual(transport.calls, [])

    def test_two_sends_carry_two_numbers(self):
        from choice_api.orders import OrdersAPI

        transport = Transport()
        transport.orders = OrdersAPI(transport)
        _, first = send_order(transport, self.cfg, "1284", BUY, 1000, 934325000)
        _, second = send_order(transport, self.cfg, "1284", BUY, 1000, 934325000)
        self.assertNotEqual(first, second)
        self.assertNotEqual(transport.calls[0][2]["ClientOrderNo"],
                            transport.calls[1][2]["ClientOrderNo"])

    def test_no_order_is_sent_any_other_way(self):
        """Every path that places an order goes through send_order."""
        import inspect

        from rollover import probe
        for source in (inspect.getsource(Broker.place_leg),
                       inspect.getsource(probe.run)):
            self.assertNotIn("orders.place_order(", source)
            self.assertIn("send_order(", source)


class TestTheAnswer(unittest.TestCase):
    def test_18_september_carried_an_order_number(self):
        self.assertEqual(reply_order_no({"Status": "Success",
                                         "Response": "260918000069382",
                                         "Reason": ""}),
                         "260918000069382")

    def test_28_september_carried_nothing(self):
        self.assertIsNone(reply_order_no({"Status": "Success",
                                          "Response": "", "Reason": ""}))

    def test_other_empty_shapes_are_nothing_too(self):
        for reply in ({"Status": "Success"},
                      {"Status": "Success", "Response": None},
                      {"Status": "Success", "Response": "   "},
                      {"Status": "Success", "Response": {}},
                      {"Status": "Success", "Response": False},
                      None, "Success", []):
            self.assertIsNone(reply_order_no(reply), reply)

    def test_a_number_inside_an_object_is_found(self):
        self.assertEqual(reply_order_no({"Status": "Success",
                                         "Response": {"OrderNo": 41}}), "41")


class Orders:
    """An order book that fills whatever carries the number we sent."""

    def __init__(self, reply, echo=True):
        self.reply = reply
        self.echo = echo
        self.placed = []

    def place_order(self, **kw):
        self.placed.append(kw)
        return self.reply

    def get_order_book(self):
        if not self.placed or not self.echo:
            return {"Status": "Success", "Response": None}
        mine = self.placed[-1]["client_order_no"]
        return {"Response": [
            {"Token": "1284", "BS": "2", "ClientOrderNo": mine,
             "GatewayOrderNo": "MINE", "FilledQty": 1000,
             "OrderStatus": "EXECUTED"},
            # Someone else's order on the same contract and side, listed
            # after ours. "The newest new row" would have taken this one.
            {"Token": "1284", "BS": "2", "ClientOrderNo": 999,
             "GatewayOrderNo": "SOMEONE", "FilledQty": 0,
             "OrderStatus": "PENDING"},
        ]}

    def cancel_order(self, **kw):
        return {"Status": "Success"}


class TestPlacingALeg(unittest.TestCase):
    def broker(self, orders):
        b = Broker(RollConfig(dry_run=False, fill_timeout_sec=0.3, order_in_lots=False), Log())
        b.client = type("Client", (), {"orders": orders})()
        return b

    def test_an_answer_without_a_number_is_said_plainly_and_halts(self):
        """What happened on 28 Sep, as the execution path would meet it."""
        orders = Orders({"Status": "Success", "Response": "", "Reason": ""},
                        echo=False)
        b = self.broker(orders)
        out = b.place_leg(contract(), SELL, 1000, D("96.4025"), "leg 1")

        self.assertTrue(out.sent)
        self.assertFalse(out.certain)          # unknown, so the engine halts
        self.assertEqual(out.filled_qty, 0)
        self.assertIn("no order number", out.detail)
        self.assertIn("never appeared", out.detail)
        self.assertIn(str(orders.placed[0]["client_order_no"]), out.detail)

    def test_an_answer_with_a_number_still_says_what_it_was(self):
        orders = Orders({"Status": "Success", "Response": "260918000069382"},
                        echo=False)
        out = self.broker(orders).place_leg(contract(), SELL, 1000,
                                            D("96.4025"), "leg 1")
        self.assertFalse(out.certain)
        self.assertIn("260918000069382", out.detail)
        self.assertEqual(out.order_ref, "260918000069382")

    def test_our_own_number_finds_our_row_over_a_newer_one(self):
        orders = Orders({"Status": "Success", "Response": "1"})
        out = self.broker(orders).place_leg(contract(), SELL, 1000,
                                            D("96.4025"), "leg 1")
        self.assertTrue(out.certain)
        self.assertEqual(out.order_ref, "MINE")
        self.assertEqual(out.filled_qty, 1000)

    def test_the_number_is_in_the_log(self):
        """So it can be quoted to the broker when something goes wrong."""
        orders = Orders({"Status": "Success", "Response": "1"})
        b = self.broker(orders)
        b.place_leg(contract(), SELL, 1000, D("96.4025"), "leg 1")
        self.assertIn(f"client order no {orders.placed[0]['client_order_no']}",
                      b.log.text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
