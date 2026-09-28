"""Signed in is not the same as able to place orders.

28 Sep 2026, one account and one session on both of Choice's servers.

  finx      the order book answered null. Two orders were acknowledged with
            {"Status": "Success", "Response": ""} and neither was created:
            not in the API's books, not in the broker's terminal.
  finxomne  the order book answered {"Orders": []}. The probe's order came
            back with an order number, reached the exchange as PENDING, and
            was cancelled.

Funds, positions and prices were identical on both, so nothing on the
screen told the two apart. The order book's answer does, and it is read at
login. Going live is refused unless the server is known to place orders.
"""
from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from datetime import date

from rollover import livemode, probe
from rollover.broker import Broker, order_book_is_live
from rollover.config import RollConfig
from rollover.money import D

FINX = {"Status": "Success", "Response": None, "Reason": ""}
FINXOMNE = {"Status": "Success", "Response": {"Orders": []}, "Reason": ""}


class Log:
    def __init__(self):
        self.lines = []

    def info(self, m): self.lines.append(("INFO", m))
    def warn(self, m): self.lines.append(("WARN", m))
    def error(self, m): self.lines.append(("ERROR", m))
    def alert(self, m): self.lines.append(("ALERT", m))

    def alerts(self):
        return [m for level, m in self.lines if level == "ALERT"]


class TestReadingTheAnswer(unittest.TestCase):
    def test_finx_on_28_september_does_not_place_orders(self):
        self.assertIs(order_book_is_live(FINX), False)

    def test_finxomne_on_28_september_does(self):
        self.assertIs(order_book_is_live(FINXOMNE), True)

    def test_a_book_with_orders_in_it_does(self):
        self.assertIs(order_book_is_live(
            {"Status": "Success", "Response": {"Orders": [{"Token": 1284}]}}),
            True)

    def test_a_bare_list_does(self):
        self.assertIs(order_book_is_live({"Status": "Success", "Response": []}),
                      True)

    def test_a_refusal_says_nothing_either_way(self):
        """A failed call is not evidence about the server's order handling."""
        self.assertIsNone(order_book_is_live(
            {"Status": "Fail", "Response": None, "Reason": "Error"}))

    def test_an_answer_without_a_status_says_nothing(self):
        self.assertIsNone(order_book_is_live({"Response": None}))

    def test_shapes_never_seen_say_nothing(self):
        for answer in (None, "Success", [], {"Status": "Success"}):
            self.assertIsNone(order_book_is_live(answer), answer)


class Orders:
    def __init__(self, answer):
        self.answer = answer

    def get_order_book(self):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def broker(answer, host="https://finx.choiceindia.com"):
    b = Broker(RollConfig(base_url=host), Log())
    b.client = type("Client", (), {"orders": Orders(answer)})()
    return b


class TestCheckingTheServer(unittest.TestCase):
    def test_unknown_until_asked(self):
        b = Broker(RollConfig(), Log())
        self.assertIsNone(b.order_route[0])

    def test_finx_is_refused_and_said_loudly(self):
        b = broker(FINX)
        verdict, why = b.check_order_route()
        self.assertIs(verdict, False)
        self.assertEqual(b.order_route, (verdict, why))
        self.assertIn("finx.choiceindia.com", why)
        self.assertIn("finxomne", why)             # what to do about it
        self.assertEqual(len(b.log.alerts()), 1)

    def test_finxomne_is_accepted_quietly(self):
        b = broker(FINXOMNE, host="https://finxomne.choiceindia.com")
        verdict, _ = b.check_order_route()
        self.assertIs(verdict, True)
        self.assertEqual(b.log.alerts(), [])

    def test_an_unreadable_book_is_unknown_not_refused(self):
        """finxomne answered 500s for ten minutes that afternoon. That is a
        reason to ask again, not a verdict on the server."""
        b = broker(RuntimeError("HTTP 500 Internal Server Error"))
        verdict, why = b.check_order_route()
        self.assertIsNone(verdict)
        self.assertIn("could not be read", why)
        self.assertEqual(b.log.alerts(), [])

    def test_it_never_raises(self):
        b = broker(KeyError("anything"))
        b.check_order_route()

    def test_it_is_asked_at_login_and_on_resume(self):
        import inspect
        self.assertIn("self.check_order_route()",
                      inspect.getsource(Broker._after_login))
        self.assertIn("self.check_order_route()",
                      inspect.getsource(Broker.resume))

    def test_the_watch_loop_asks_again_while_it_is_unknown(self):
        import inspect

        from rollover.engine import RollEngine
        source = inspect.getsource(RollEngine._slow_refresh)
        self.assertIn("check_order_route", source)
        self.assertIn("route[0] is None", source)


class Engine:
    def __init__(self, route):
        self.broker = type("B", (), {"logged_in": True,
                                     "scrip_file_date": date.today(),
                                     "order_route": route})()
        self.session = type("S", (), {"halted_reason": None,
                                      "near": None, "far": None})()
        self.feed = None


def config():
    return RollConfig(near_token="1284", far_token="1584",
                      quantity_unit_confirmed=True, dry_run=True)


class TestGoingLive(unittest.TestCase):
    def check(self, route):
        checks = livemode.preflight(config(), Engine(route), feed_healthy=True)
        return next(c for c in checks if c.name == "orders reach the exchange")

    def test_a_server_that_swallows_orders_blocks_going_live(self):
        found = self.check((False, "finx answers the order book with nothing"))
        self.assertFalse(found.passed)
        self.assertIn("finx", found.detail)

    def test_not_knowing_yet_blocks_too(self):
        found = self.check((None, "the order book could not be read"))
        self.assertFalse(found.passed)
        self.assertIn("not known yet", found.detail)

    def test_a_server_that_places_orders_passes(self):
        self.assertTrue(self.check((True, "finxomne answers")).passed)

    def test_a_broker_that_was_never_checked_blocks(self):
        """An older broker object, or one built before login."""
        engine = Engine(None)
        del type(engine.broker).order_route
        checks = livemode.preflight(config(), engine, feed_healthy=True)
        found = next(c for c in checks if c.name == "orders reach the exchange")
        self.assertFalse(found.passed)


class TestTheProbeWillNotFeedIt(unittest.TestCase):
    """Sending one more order to a server known to swallow them proves
    nothing, and a real order is a real order."""

    def test_nothing_is_sent_to_a_server_that_answers_null(self):
        sent = []

        class Orders:
            def get_order_book(self): return dict(FINX)
            def get_order_book_v2(self): return dict(FINX)
            def get_trade_book(self): return {"Status": "Success",
                                              "Response": {"Trades": []}}
            def place_order(self, **kw):
                sent.append(kw)
                return {"Status": "Success", "Response": ""}

        class FakeBroker:
            client = type("C", (), {"orders": Orders()})()
            def __init__(self, cfg, log): pass
            def build_client(self): pass
            def resume(self, path): return True
            def load_scrip_master(self, force=False): pass
            def instrument(self, token):
                from rollover.broker import InstrumentInfo
                return InstrumentInfo(
                    token="1284", symbol="USDINR", sec_desc="USDINR26OCTFUT",
                    segment="13", lot_size=1000, expiry=date(2026, 10, 28),
                    instrument="FUTCUR", price_divisor=D("10000000"),
                    tick=D("0.0025"), tick_units=D("25000"),
                    low_range=D("93.235"), high_range=D("99"))
            def exchange_price(self, info, price): return D("934050000")

        folder = tempfile.mkdtemp(prefix="route_")
        self.addCleanup(shutil.rmtree, folder, True)
        original = probe.Broker
        probe.Broker = FakeBroker
        said = io.StringIO()
        try:
            with contextlib.redirect_stdout(said):
                code = probe.run(RollConfig(near_token="1284", vendor_id="V",
                                            api_key="K", mobile_no="9"),
                                 type("L", (), {"info": lambda *a: None,
                                                "warn": lambda *a: None})(),
                                 folder, confirm=lambda _p: probe.CONFIRM)
        finally:
            probe.Broker = original
        self.assertEqual(sent, [])
        self.assertEqual(code, 2)
        self.assertIn("finxomne", said.getvalue())
        self.assertIn("Nothing was sent", said.getvalue())


class TestTheCommandLinePreflightAsks(unittest.TestCase):
    def test_it_checks_the_route(self):
        import inspect

        from rollover import preflight
        source = inspect.getsource(preflight.run)
        self.assertIn("broker.check_order_route()", source)
        self.assertIn('"order route"', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
