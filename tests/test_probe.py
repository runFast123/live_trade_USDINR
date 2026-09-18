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

    def run_probe(self, answer):
        original = probe.Broker
        probe.Broker = self.fake_broker()
        try:
            # The probe talks to the operator on stdout; keep it out of the
            # test report.
            with contextlib.redirect_stdout(io.StringIO()):
                return probe.run(self.cfg, StubLog(), self.dir,
                                 confirm=lambda _prompt: answer)
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

    def test_what_it_sends_is_a_buy_of_the_asked_quantity(self):
        self.run_probe(CONFIRM)
        order = self.sent[0]
        self.assertEqual(order["bs"], 1)              # BUY
        self.assertEqual(order["qty"], 1)
        self.assertEqual(order["price"], 930800000)
        self.assertEqual(order["trigger_price"], 0)

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
