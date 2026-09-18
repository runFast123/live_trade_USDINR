"""Tests for the two-leg order sequence.

This is the code that actually places orders, so it is worth being exact about
what each outcome must do. Nothing here touches a network: the broker is a stub
that returns whatever fill the test wants.

The cases that matter are the unhappy ones. A near leg that half fills must not
produce an oversized far leg. A far leg that fails must halt rather than retry.
A fill that cannot be read must halt rather than be assumed.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import date

from rollover import rule
from rollover.broker import BUY, SELL, InstrumentInfo, OrderOutcome
from rollover.config import RollConfig
from rollover.engine import HALTED, RollEngine
from rollover.money import D
from rollover.quotes import Quote


class StubLog:
    def __init__(self):
        self.lines = []

    def _add(self, level, message):
        self.lines.append((level, message))

    def info(self, m): self._add("INFO", m)
    def warn(self, m): self._add("WARN", m)
    def error(self, m): self._add("ERROR", m)
    def alert(self, m): self._add("ALERT", m)

    def text(self):
        return "\n".join(f"{lvl} {msg}" for lvl, msg in self.lines)


class StubMarginAPI:
    """Stands in for choice_api's orders/funds, so the real parsing runs."""

    def __init__(self, margin=50000, funds=10000000):
        self.margin, self.funds = margin, funds
        self.asked = []

    def get_margin(self, **kw):
        self.asked.append(kw)
        if self.margin is None:
            return {"Status": "Fail", "Response": "no", "Reason": "Error"}
        return {"Status": "Success", "Response": {"TotalMargin": self.margin},
                "Reason": ""}

    def get_funds_view(self):
        if self.funds is None:
            return {"Status": "Fail", "Response": "no", "Reason": "Error"}
        return {"Status": "Success",
                "Response": {"AvailableMargin": self.funds}, "Reason": ""}


class StubBroker:
    """Returns scripted outcomes and records exactly what was asked of it.

    It mirrors the real broker's contract, including that the dry-run guard
    lives in place_leg rather than in the engine. That placement is deliberate:
    it means any future call site is safe by default, so the stub has to honour
    it or the dry-run tests would be checking the wrong thing.
    """

    def __init__(self, outcomes, cfg=None, positions=None, traded=None,
                 margin=50000, funds=10000000):
        self.outcomes = list(outcomes)
        self.cfg = cfg
        self.calls = []          # every request the engine made
        self.sent = []           # the subset that would reach the exchange
        self.logged_in = True
        self.scrip_file_date = date.today()
        # The real broker always has one, and the margin reader uses it.
        self.log = StubLog()

        # Reconciliation reads these. By default the account moves exactly as
        # the fills say it did, so a correct roll reconciles and the tests that
        # are about something else are not disturbed by it.
        #
        # `positions` is a list of books, one per reading: the first is what
        # the account held before the roll, the last is what it holds after.
        # A single dict means it never moves, which is a mismatch.
        self.positions = ([positions] if isinstance(positions, dict)
                          else list(positions or []))
        self.traded = traded                 # None = the trade book is unreadable
        self._auto_positions = not self.positions
        self._reads = 0

        # An account with room by default, so tests about anything else are
        # not stopped by the margin gate. None on either means unreadable.
        self.margin_api = StubMarginAPI(margin, funds)
        self.client = type("C", (), {"orders": self.margin_api,
                                     "funds": self.margin_api})()

    def place_leg(self, info, side, qty, limit_price, label):
        self.calls.append({"token": info.token, "side": side, "qty": qty,
                           "price": limit_price, "label": label})
        if self.cfg is not None and self.cfg.dry_run:
            return OrderOutcome(sent=False, filled_qty=0, requested_qty=qty,
                                order_ref=None, certain=True, detail="dry run")
        self.sent.append(self.calls[-1])
        if not self.outcomes:
            raise AssertionError(f"unexpected extra order: {label}")
        result = self.outcomes.pop(0)
        # The position moves by what filled, never by what was asked for. A
        # stub that moved by the request would make every partial fill look
        # like a reconciliation failure.
        self.calls[-1]["filled"] = result.filled_qty
        return result

    def market_open(self): return True
    def long_qty(self, token): return 1000

    def net_qty(self, token):
        if self._auto_positions:
            # Follow whatever actually got sent, so the position book agrees
            # with the orders by construction.
            moved = 0
            for call in self.sent:
                if call["token"] != token:
                    continue
                filled = call.get("filled", 0)
                moved += -filled if call["side"] == SELL else filled
            return 1000 + moved

        # capture() reads both legs, so two calls make one reading.
        index = min(self._reads // 2, len(self.positions) - 1)
        self._reads += 1
        return self.positions[index].get(token)

    def _trade_snapshot(self):
        return set() if self.traded is not None else None

    def traded_since(self, before, token, side):
        if self.traded is None:
            return None, "the trade book could not be read"
        return self.traded.get((token, side), 0), "stubbed"
    def refresh_scrip_master_if_stale(self): return False
    def load_scrip_master(self, force=False): return None
    def instrument(self, token):
        return instrument(token, "USDINR" + token)


def outcome(filled, requested, certain=True, sent=True, detail="") -> OrderOutcome:
    return OrderOutcome(sent=sent, filled_qty=filled, requested_qty=requested,
                        order_ref="X1", certain=certain,
                        detail=detail or f"filled {filled} of {requested}")


def instrument(token, desc):
    return InstrumentInfo(token=token, symbol="USDINR", sec_desc=desc,
                          segment="13", lot_size=1000, expiry=date(2026, 9, 28),
                          instrument="FUTCUR", price_divisor=D("10000000"),
                          tick=D("0.0025"), tick_units=D("25000"),
                          low_range=D("93"), high_range=D("99"))


class ExecutionCase(unittest.TestCase):
    def setUp(self):
        # Each engine persists its clip count and any halt, so every test needs
        # its own directory or they would inherit each other's halts.
        self.state_dir = tempfile.mkdtemp(prefix="engine_")

    def tearDown(self):
        shutil.rmtree(self.state_dir, ignore_errors=True)

    def build(self, outcomes, positions=None, traded=None, margin=50000,
              funds=10000000, **cfg_kw):
        cfg_kw.setdefault("dry_run", False)
        cfg_kw.setdefault("use_live_feed", False)
        cfg_kw.setdefault("limit_mode", "absolute")
        # Reconciliation waits for the broker's book to catch up. In a test
        # there is nothing to wait for, and waiting would only slow the suite.
        cfg_kw.setdefault("reconcile_wait_sec", 0.0)
        self.cfg = RollConfig(near_token="1769", far_token="1584", **cfg_kw)
        self.log = StubLog()
        self.broker = StubBroker(outcomes, self.cfg, positions=positions,
                                 traded=traded, margin=margin, funds=funds)

        engine = RollEngine(self.cfg, self.log, self.state_dir,
                            broker=self.broker)
        engine.session.near = instrument("1769", "USDINR26SEPFUT")
        engine.session.far = instrument("1584", "USDINR26NOVFUT")

        self.near_q = Quote("1769", D("95.9400"), D("95.9450"), 0.0, D("1"),
                            bid_qty=5000, ask_qty=5000)
        self.far_q = Quote("1584", D("96.2370"), D("96.2375"), 0.0, D("1"),
                           bid_qty=5000, ask_qty=5000)
        self.decision = rule.compute(self.near_q, self.far_q, self.cfg)
        return engine


class TestHappyPath(ExecutionCase):
    def test_both_legs_fill_and_the_clip_is_counted(self):
        engine = self.build([outcome(1000, 1000), outcome(1000, 1000)])
        engine._execute(self.decision, self.near_q, self.far_q)

        self.assertEqual(len(self.broker.calls), 2)
        near, far = self.broker.calls
        self.assertEqual((near["token"], near["side"], near["qty"]),
                         ("1769", SELL, 1000))
        self.assertEqual((far["token"], far["side"], far["qty"]),
                         ("1584", BUY, 1000))
        self.assertEqual(engine.session.clips_done_today, 1)
        self.assertIsNone(engine.session.halted_reason)
        self.assertIn("ROLL COMPLETE", self.log.text())

    def test_the_near_leg_is_sold_before_the_far_leg_is_bought(self):
        """Near first leaves a failed roll flat, not long two contracts."""
        engine = self.build([outcome(1000, 1000), outcome(1000, 1000)])
        engine._execute(self.decision, self.near_q, self.far_q)
        self.assertEqual(self.broker.calls[0]["side"], SELL)
        self.assertEqual(self.broker.calls[1]["side"], BUY)

    def test_the_limit_prices_from_the_rule_are_the_ones_sent(self):
        engine = self.build([outcome(1000, 1000), outcome(1000, 1000)])
        engine._execute(self.decision, self.near_q, self.far_q)
        self.assertEqual(self.broker.calls[0]["price"], self.decision.sell_limit)
        self.assertEqual(self.broker.calls[1]["price"], self.decision.buy_limit)

    def test_in_flight_is_cleared_afterwards(self):
        engine = self.build([outcome(1000, 1000), outcome(1000, 1000)])
        engine._execute(self.decision, self.near_q, self.far_q)
        self.assertFalse(engine.session.in_flight)


class TestNearLegOutcomes(ExecutionCase):
    def test_a_near_leg_that_does_not_fill_stops_there(self):
        engine = self.build([outcome(0, 1000)])
        engine._execute(self.decision, self.near_q, self.far_q)

        self.assertEqual(len(self.broker.calls), 1)     # no far leg
        self.assertIsNone(engine.session.halted_reason)
        self.assertEqual(engine.session.clips_done_today, 0)
        self.assertIn("No exposure changed", self.log.text())

    def test_a_partial_near_fill_sizes_the_far_leg_to_what_filled(self):
        """The far leg must follow the fill, not the request."""
        engine = self.build([outcome(300, 1000), outcome(300, 300)])
        engine._execute(self.decision, self.near_q, self.far_q)

        self.assertEqual(self.broker.calls[0]["qty"], 1000)
        self.assertEqual(self.broker.calls[1]["qty"], 300)
        self.assertEqual(engine.session.clips_done_today, 1)
        self.assertIsNone(engine.session.halted_reason)

    def test_an_unconfirmable_near_fill_halts(self):
        engine = self.build([outcome(0, 1000, certain=False,
                                     detail="never appeared in the order book")])
        engine._execute(self.decision, self.near_q, self.far_q)

        self.assertEqual(len(self.broker.calls), 1)
        self.assertIsNotNone(engine.session.halted_reason)
        self.assertIn("could not be confirmed", engine.session.halted_reason)

    def test_a_rejected_near_leg_does_not_send_the_far_leg(self):
        engine = self.build([outcome(0, 1000, sent=False,
                                     detail="rejected before reaching the exchange")])
        engine._execute(self.decision, self.near_q, self.far_q)
        self.assertEqual(len(self.broker.calls), 1)
        self.assertEqual(engine.session.clips_done_today, 0)


class TestFarLegFailure(ExecutionCase):
    def test_a_far_leg_that_does_not_fill_halts_and_alerts(self):
        engine = self.build([outcome(1000, 1000), outcome(0, 1000)])
        engine._execute(self.decision, self.near_q, self.far_q)

        self.assertIsNotNone(engine.session.halted_reason)
        self.assertIn("HALF ROLLED", engine.session.halted_reason)
        self.assertIn("ALERT", self.log.text())
        self.assertEqual(engine.session.clips_done_today, 0)

    def test_a_partly_filled_far_leg_also_halts(self):
        engine = self.build([outcome(1000, 1000), outcome(400, 1000)])
        engine._execute(self.decision, self.near_q, self.far_q)

        self.assertIsNotNone(engine.session.halted_reason)
        self.assertIn("short 600", engine.session.halted_reason)

    def test_it_does_not_retry_the_far_leg(self):
        engine = self.build([outcome(1000, 1000), outcome(0, 1000)])
        engine._execute(self.decision, self.near_q, self.far_q)
        self.assertEqual(len(self.broker.calls), 2)     # no third order

    def test_auto_unwind_buys_the_near_leg_back(self):
        engine = self.build([outcome(1000, 1000), outcome(0, 1000),
                             outcome(1000, 1000)],
                            auto_unwind_on_leg2_failure=True)
        engine._execute(self.decision, self.near_q, self.far_q)

        self.assertEqual(len(self.broker.calls), 3)
        unwind = self.broker.calls[2]
        self.assertEqual((unwind["token"], unwind["side"], unwind["qty"]),
                         ("1769", BUY, 1000))
        self.assertIn("bought back", engine.session.halted_reason)

    def test_a_failed_unwind_says_so_loudly(self):
        engine = self.build([outcome(1000, 1000), outcome(0, 1000),
                             outcome(0, 1000)],
                            auto_unwind_on_leg2_failure=True)
        engine._execute(self.decision, self.near_q, self.far_q)
        self.assertIn("unwind also failed", engine.session.halted_reason)
        self.assertIn("terminal immediately", engine.session.halted_reason)

    def test_the_unwind_price_is_above_the_offer(self):
        """Buying back has to cross, so it is priced through the ask."""
        engine = self.build([outcome(1000, 1000), outcome(0, 1000),
                             outcome(1000, 1000)],
                            auto_unwind_on_leg2_failure=True)
        engine._execute(self.decision, self.near_q, self.far_q)
        self.assertGreater(self.broker.calls[2]["price"], self.near_q.ask)


class TestDryRun(ExecutionCase):
    def test_no_order_reaches_the_exchange(self):
        engine = self.build([], dry_run=True)
        engine._execute(self.decision, self.near_q, self.far_q)
        self.assertEqual(self.broker.sent, [])

    def test_the_guard_is_in_the_broker_so_every_call_site_is_covered(self):
        """place_leg is still reached; it is place_leg that refuses to send."""
        engine = self.build([], dry_run=True)
        engine._execute(self.decision, self.near_q, self.far_q)
        self.assertTrue(self.broker.calls)
        self.assertEqual(self.broker.sent, [])

    def test_it_still_reports_what_it_would_have_done(self):
        engine = self.build([], dry_run=True)
        engine._execute(self.decision, self.near_q, self.far_q)
        text = self.log.text()
        self.assertIn("DRY RUN", text)
        self.assertIn("No orders reached the exchange", text)
        self.assertEqual(engine.session.clips_done_today, 1)


class TestHaltBehaviour(ExecutionCase):
    def test_a_halt_disarms_and_shows_the_state(self):
        engine = self.build([outcome(1000, 1000), outcome(0, 1000)])
        engine.arm()
        self.assertTrue(engine.armed)
        engine._execute(self.decision, self.near_q, self.far_q)

        self.assertFalse(engine.armed)
        self.assertEqual(engine._state, HALTED)

    def test_arming_is_refused_while_halted(self):
        engine = self.build([outcome(1000, 1000), outcome(0, 1000)])
        engine._execute(self.decision, self.near_q, self.far_q)
        engine.arm()
        self.assertFalse(engine.armed)

    def test_clearing_the_halt_lifts_it(self):
        engine = self.build([outcome(1000, 1000), outcome(0, 1000)])
        engine._execute(self.decision, self.near_q, self.far_q)
        self.assertIsNotNone(engine.session.halted_reason)

        # clear_halt restarts the watch loop, which would connect; keep it still.
        engine.start = lambda: None
        engine.clear_halt()
        self.assertIsNone(engine.session.halted_reason)


class TestNothingEscapesWithoutAHalt(ExecutionCase):
    """An exception mid-roll must halt, never return to watching.

    _execute used to be try/finally with no except. An exception after the near
    leg filled escaped to the watch loop, which logged it and carried on: no
    halt, every gate passing again, and the operator free to arm on top of a
    position that was already half moved. That was the most dangerous line in
    the program.
    """

    class Boom(RuntimeError):
        pass

    def test_a_failure_while_sending_the_far_leg_halts(self):
        engine = self.build([outcome(1000, 1000)])

        def explode(info, side, qty, price, label):
            if "leg 2" in label:
                raise self.Boom("gateway refused the far leg")
            return outcome(1000, 1000)

        engine.broker.place_leg = explode
        engine._execute(self.decision, self.near_q, self.far_q)

        self.assertIsNotNone(engine.session.halted_reason)
        self.assertIn("Boom", engine.session.halted_reason)
        self.assertEqual(engine._state, HALTED)

    def test_the_halt_says_a_leg_may_have_filled(self):
        engine = self.build([])

        def explode(*a, **k):
            raise self.Boom("network died")

        engine.broker.place_leg = explode
        engine._execute(self.decision, self.near_q, self.far_q)
        self.assertIn("may have filled", engine.session.halted_reason)

    def test_a_pricing_failure_on_the_far_leg_halts(self):
        """exchange_price raises BrokerError for an off-grid price."""
        from rollover.broker import BrokerError

        engine = self.build([outcome(1000, 1000)])

        def explode(info, side, qty, price, label):
            if "leg 2" in label:
                raise BrokerError("price is not a multiple of the tick")
            return outcome(1000, 1000)

        engine.broker.place_leg = explode
        engine._execute(self.decision, self.near_q, self.far_q)
        self.assertIsNotNone(engine.session.halted_reason)

    def test_in_flight_is_still_cleared_after_an_exception(self):
        engine = self.build([])
        engine.broker.place_leg = lambda *a, **k: (_ for _ in ()).throw(
            self.Boom("x"))
        engine._execute(self.decision, self.near_q, self.far_q)
        self.assertFalse(engine.session.in_flight)

    def test_an_exception_does_not_count_a_clip(self):
        engine = self.build([])
        engine.broker.place_leg = lambda *a, **k: (_ for _ in ()).throw(
            self.Boom("x"))
        engine._execute(self.decision, self.near_q, self.far_q)
        self.assertEqual(engine.session.clips_done_today, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
