"""Tests for the resting-order probe.

It places a real order, so the things worth pinning are the refusals: it must
not send without an exact confirmation, and it must never choose a price that
could trade.
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from datetime import date

from rollover import probe
from rollover.broker import InstrumentInfo
from rollover.config import RollConfig
from rollover.money import D
from rollover.probe import CONFIRM, ProbeError, choose_price


def instrument(low="93.0800", high="98.8350", tick="0.0025"):
    return InstrumentInfo(
        token="1769", symbol="USDINR", sec_desc="USDINR26SEPFUT", segment="13",
        lot_size=1000, expiry=date(2026, 9, 28), instrument="FUTCUR",
        price_divisor=D("10000000"), tick=D(tick) if tick else None,
        tick_units=D("25000"),
        low_range=D(low) if low else None, high_range=D(high) if high else None)


class StubLog:
    def info(self, m): pass
    def warn(self, m): pass
    def error(self, m): pass
    def alert(self, m): pass


class TestPriceChoice(unittest.TestCase):
    def test_it_rests_far_below_the_market(self):
        price = choose_price(instrument(), bid=D("95.9675"))
        self.assertLess(price, D("95.9675"))
        self.assertGreaterEqual(price, D("93.0800"))

    def test_it_never_goes_below_the_circuit_floor(self):
        """Below the floor the exchange rejects it, which teaches nothing."""
        price = choose_price(instrument(), bid=D("93.5000"))
        self.assertGreaterEqual(price, D("93.0800"))

    def test_it_sits_on_the_tick_grid(self):
        price = choose_price(instrument(), bid=D("95.9675"))
        self.assertEqual(price % D("0.0025"), 0)

    def test_it_falls_back_to_the_floor_without_a_quote(self):
        self.assertEqual(choose_price(instrument(), bid=None), D("93.0800"))

    def test_it_refuses_without_a_circuit_limit(self):
        with self.assertRaises(ProbeError):
            choose_price(instrument(low=None), bid=D("95.96"))

    def test_it_refuses_if_the_price_would_not_be_below_the_bid(self):
        """A market already at its floor leaves nowhere safe to rest."""
        with self.assertRaises(ProbeError) as ctx:
            choose_price(instrument(), bid=D("93.0800"))
        self.assertIn("not below the bid", str(ctx.exception))

    def test_the_clearance_is_meaningful(self):
        price = choose_price(instrument(low="80"), bid=D("95.9675"))
        self.assertLessEqual(price, D("95.9675") - D("2.99"))


class TestItAsksFirst(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="probe_")
        self.cfg = RollConfig(near_token="1769", vendor_id="V",
                              api_key="K", mobile_no="9")
        self.sent = []

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def fake_broker(self):
        sent = self.sent

        class Orders:
            def place_order(self, **kw):
                sent.append(kw)
                return {"Status": "Success"}
            def get_order_book(self): return {"Response": []}
            def get_order_book_v2(self): return {"Response": []}
            def get_trade_book(self): return {"Response": []}
            def cancel_order(self, **kw): return {"Status": "Success"}

        class Client:
            orders = Orders()

        class FakeBroker:
            client = Client()
            def __init__(self, cfg, log): pass
            def build_client(self): pass
            def resume(self, path): return True
            def load_scrip_master(self, force=False): pass
            def instrument(self, token): return instrument()
            def exchange_price(self, info, price): return D("930800000")

        return FakeBroker

    def run_probe(self, answer, qty=None, capture=False):
        original = probe.Broker
        probe.Broker = self.fake_broker()
        said = io.StringIO()
        try:
            # The probe talks to the operator on stdout; keep it out of the
            # test report unless a test wants to read what it said.
            with contextlib.redirect_stdout(said):
                code = probe.run(self.cfg, StubLog(), self.dir, qty=qty,
                                 confirm=lambda _prompt: answer)
            return said.getvalue() if capture else code
        finally:
            probe.Broker = original

    def test_the_wrong_word_sends_nothing(self):
        for answer in ("yes", "y", "", "place real order", "PLACE REAL ORDERS"):
            self.sent.clear()
            code = self.run_probe(answer)
            self.assertEqual(self.sent, [], msg=answer)
            self.assertEqual(code, 1)

    def test_the_exact_phrase_sends_the_order(self):
        code = self.run_probe(CONFIRM)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(code, 0)

    def test_what_it_sends_is_a_buy_of_one_whole_lot(self):
        """Not qty 1. The broker rejects a quantity that is not a whole
        multiple of the lot, so an order of 1 never rests -- and the
        read-back and cancel this exists to test never happen."""
        self.run_probe(CONFIRM)
        order = self.sent[0]
        self.assertEqual(order["bs"], 1)              # BUY
        self.assertEqual(order["qty"], 1000)          # one lot of USDINR
        self.assertEqual(order["price"], 930800000)
        self.assertEqual(order["trigger_price"], 0)

    def test_an_explicit_quantity_is_still_honoured(self):
        self.run_probe(CONFIRM, qty=2000)
        self.assertEqual(self.sent[0]["qty"], 2000)

    def test_a_part_lot_quantity_is_warned_about_not_refused(self):
        """Choice suggest it themselves as a negative test: it proves the
        lot-multiple rule. It just cannot rest, so say so."""
        out = self.run_probe(CONFIRM, qty=1, capture=True)
        self.assertIn("not a whole multiple", out)
        self.assertEqual(self.sent[0]["qty"], 1)

    def test_it_writes_down_everything_it_saw(self):
        self.run_probe(CONFIRM)
        folder = os.path.join(self.dir, "data")
        captured = [n for n in os.listdir(folder) if n.startswith("probe-")]
        self.assertEqual(len(captured), 1)

        import json
        with open(os.path.join(folder, captured[0]), encoding="utf-8") as fh:
            record = json.load(fh)
        for key in ("contract", "sent", "place_order", "order_book",
                    "order_book_v2", "trade_book"):
            self.assertIn(key, record)
        self.assertEqual(record["sent"]["side"], "BUY")

    def test_it_stops_without_a_session(self):
        base = self.fake_broker()

        class NoSession(base):
            def resume(self, path): return False

        original = probe.Broker
        probe.Broker = NoSession
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = probe.run(self.cfg, StubLog(), self.dir,
                                 confirm=lambda _p: CONFIRM)
        finally:
            probe.Broker = original
        self.assertEqual(code, 2)
        self.assertEqual(self.sent, [])

    def test_it_stops_without_a_contract(self):
        self.cfg.near_token = ""
        code = self.run_probe(CONFIRM)
        self.assertEqual(code, 2)
        self.assertEqual(self.sent, [])


class TestTheWarning(unittest.TestCase):
    def test_it_says_plainly_that_this_is_real(self):
        text = probe.describe(instrument(), D("93.0800"), 1, D("95.9675"),
                              D("930800000"))
        self.assertIn("REAL ORDER", text)
        self.assertIn("cannot trade", text)
        self.assertIn("930800000", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestItKnowsWhichOrderIsOurs(unittest.TestCase):
    """The probe places a real order and then cancels it.

    Which means it has to be certain which row in the book is its own. It
    used to take "the most recent row on this token", which is only right on
    an account with nothing else working -- and the next thing it does with
    that row is cancel it.

    The id place_order hands back cannot help: the live capture from
    18 September returns "260918000069382" and that value appears nowhere in
    the order book row, which carries ClientOrderNo 100000014 and an empty
    ExchangeOrderNo. So ours is the row that was NOT there before, exactly
    as the execution path identifies its own orders.
    """

    def book(self, *rows):
        return {"ok": True, "response": {"Response": list(rows)}}

    def row(self, client_no, token="1769", status="PENDING"):
        return {"Token": int(token), "ClientOrderNo": client_no,
                "ExchangeOrderNo": "", "GatewayOrderNo": None,
                "OrderStatus": status, "Qty": 1, "Price": 93.06,
                "SegmentId": 13, "BS": "1"}

    def test_the_one_new_order_is_ours(self):
        before = probe._identities(self.book(self.row(1), self.row(2)))
        after = self.book(self.row(1), self.row(2), self.row(3))
        found, problem = probe._find_ours(after, "1769", before)
        self.assertIsNone(problem)
        self.assertEqual(found["ClientOrderNo"], 3)

    def test_it_is_not_simply_the_most_recent(self):
        """Someone else's order arriving after ours must not be taken."""
        before = probe._identities(self.book(self.row(1)))
        after = self.book(self.row(1), self.row(2))
        found, _ = probe._find_ours(after, "1769", before)
        self.assertEqual(found["ClientOrderNo"], 2)

    def test_two_new_orders_are_refused_rather_than_guessed(self):
        before = probe._identities(self.book(self.row(1)))
        after = self.book(self.row(1), self.row(2), self.row(3))
        found, problem = probe._find_ours(after, "1769", before)
        self.assertIsNone(found)
        self.assertIn("2 new orders", problem)
        self.assertIn("cancel by hand", problem)

    def test_no_new_order_is_reported_as_such(self):
        before = probe._identities(self.book(self.row(1)))
        found, problem = probe._find_ours(self.book(self.row(1)), "1769",
                                          before)
        self.assertIsNone(found)
        self.assertIn("no NEW order", problem)

    def test_an_order_on_another_token_is_not_ours(self):
        before = probe._identities(self.book(self.row(1)))
        after = self.book(self.row(1), self.row(9, token="1584"))
        found, problem = probe._find_ours(after, "1769", before)
        self.assertIsNone(found)
        self.assertIn("no NEW order", problem)

    def test_an_empty_book_says_it_never_appeared(self):
        found, problem = probe._find_ours(self.book(), "1769", set())
        self.assertIsNone(found)
        self.assertIn("never appeared", problem)

    def test_an_unreadable_book_is_not_treated_as_empty(self):
        found, problem = probe._find_ours({"ok": False, "error": "boom"},
                                          "1769", set())
        self.assertIsNone(found)

    def test_the_identities_of_an_unreadable_book_are_empty(self):
        """And the run refuses to place anything in that case -- an empty
        set would make every order in the book look new."""
        self.assertEqual(probe._identities({"ok": False}), set())


class TestItWillNotPlaceWhatItCannotIdentify(unittest.TestCase):
    def test_the_run_refuses_when_the_book_cannot_be_read(self):
        import inspect
        source = inspect.getsource(probe.run)
        self.assertIn("order_book_before", source)
        self.assertIn("Nothing was sent.", source)

    def test_and_it_reads_the_book_before_it_places(self):
        import inspect
        source = inspect.getsource(probe.run)
        self.assertLess(source.index("order_book_before"),
                        source.index("place_order"))
