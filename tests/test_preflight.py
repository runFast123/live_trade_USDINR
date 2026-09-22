"""The read-only check of the live order path.

Phase 3 begins by putting a real order in front of the exchange. --preflight
is what confirms, beforehand and against the live broker, that every field
that order depends on is right: the quantity is a whole number of lots, the
price lands on the exchange's tick grid and inside today's circuit band, the
books can be read, and the margin is there.

Two things it must never do, both asserted here: send anything, and pass a
run that has a blocker in it.
"""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal

from rollover import preflight
from rollover.broker import BUY, SELL, BrokerError, InstrumentInfo
from rollover.money import D


def contract(token="1284", lot=1000, divisor="10000000", tick="0.0025",
             low="93.2425", high="99.0075"):
    return InstrumentInfo(
        token=token, symbol="USDINR", sec_desc="USDINR26OCTFUT", segment="13",
        lot_size=lot, expiry=date(2026, 10, 28), instrument="FUTCUR",
        price_divisor=D(divisor), tick=D(tick), tick_units=D("25000"),
        low_range=D(low), high_range=D(high))


class FakeBroker:
    """Only the one method the leg check calls."""

    def __init__(self, error=None):
        self.error = error

    def exchange_price(self, info, rupees: Decimal) -> Decimal:
        if self.error:
            raise BrokerError(self.error)
        return (rupees * info.price_divisor).to_integral_value()


def check(qty=1000, price="96.0825", side=SELL, info=None, broker=None):
    report = preflight.Report()
    with redirect_stdout(io.StringIO()):
        preflight._leg_order_check(report, broker or FakeBroker(),
                                   info or contract(), side, qty,
                                   D(price), "near")
    return report


def marks(report, contains):
    return [r[0] for r in report.rows if contains in r[1]]


class TestTheQuantityIsAWholeNumberOfLots(unittest.TestCase):
    def test_a_whole_lot_passes(self):
        self.assertEqual(marks(check(qty=1000), "quantity"), ["PASS"])

    def test_several_whole_lots_pass(self):
        self.assertEqual(marks(check(qty=4000), "quantity"), ["PASS"])

    def test_a_part_lot_fails(self):
        """The exchange refuses it, so the app must not be the one to find
        that out by sending it."""
        self.assertEqual(marks(check(qty=1500), "quantity"), ["FAIL"])

    def test_a_contract_with_no_lot_size_fails(self):
        self.assertEqual(marks(check(info=contract(lot=0)), "quantity"),
                         ["FAIL"])

    def test_the_line_says_how_many_lots_that_is(self):
        report = check(qty=4000)
        detail = [r[2] for r in report.rows if "quantity" in r[1]][0]
        self.assertIn("4 lot(s)", detail)


class TestThePriceIsOneTheExchangeWillTake(unittest.TestCase):
    def test_a_price_on_the_tick_grid_passes(self):
        self.assertEqual(marks(check(price="96.0825"), "price"), ["PASS"])

    def test_a_price_off_the_grid_fails(self):
        """exchange_price refuses rather than rounding, and so does this."""
        report = check(broker=FakeBroker(error="not a multiple of the tick"))
        self.assertEqual(marks(report, "price"), ["FAIL"])

    def test_a_refused_price_stops_the_band_check(self):
        """There is no price to check against the band."""
        report = check(broker=FakeBroker(error="no PriceDivisor"))
        self.assertEqual(marks(report, "circuit band"), [])

    def test_the_line_shows_the_integer_that_would_be_sent(self):
        report = check(price="96.0825")
        detail = [r[2] for r in report.rows if "price" in r[1]][0]
        self.assertIn("960825000", detail)


class TestThePriceIsInsideTodaysCircuitBand(unittest.TestCase):
    def test_a_price_inside_the_band_passes(self):
        self.assertEqual(marks(check(price="96.0825"), "circuit band"),
                         ["PASS"])

    def test_a_price_above_the_band_fails(self):
        self.assertEqual(marks(check(price="99.5000"), "circuit band"),
                         ["FAIL"])

    def test_a_price_below_the_band_fails(self):
        self.assertEqual(marks(check(price="90.0000"), "circuit band"),
                         ["FAIL"])

    def test_a_contract_with_no_band_warns_rather_than_passing(self):
        info = contract()
        info.low_range = info.high_range = None
        self.assertEqual(marks(check(info=info), "circuit band"), ["WARN"])

    def test_both_sides_are_checked_the_same_way(self):
        self.assertEqual(marks(check(side=BUY, price="90.0"), "circuit band"),
                         ["FAIL"])
        self.assertEqual(marks(check(side=BUY, price="96.0825"),
                               "circuit band"), ["PASS"])

    def test_the_label_says_which_way_the_leg_goes(self):
        names = [r[1] for r in check(side=BUY).rows]
        self.assertTrue(all("BUY" in n for n in names), names)


class TestTheVerdict(unittest.TestCase):
    def verdict(self, report):
        out = io.StringIO()
        with redirect_stdout(out):
            code = preflight._verdict(report)
        return code, out.getvalue()

    def test_a_clean_run_returns_zero(self):
        report = preflight.Report()
        with redirect_stdout(io.StringIO()):
            report.add(preflight.PASS, "session", "fine")
        code, text = self.verdict(report)
        self.assertEqual(code, 0)
        self.assertIn("Nothing blocks a live order", text)

    def test_any_failure_returns_one(self):
        report = preflight.Report()
        with redirect_stdout(io.StringIO()):
            report.add(preflight.PASS, "session", "fine")
            report.add(preflight.FAIL, "margin", "not enough")
        code, text = self.verdict(report)
        self.assertEqual(code, 1)
        self.assertIn("margin", text)
        self.assertIn("Phase 3 should not start", text)

    def test_warnings_alone_still_pass_but_are_listed(self):
        report = preflight.Report()
        with redirect_stdout(io.StringIO()):
            report.add(preflight.WARN, "square off", "cannot flatten")
        code, text = self.verdict(report)
        self.assertEqual(code, 0)
        self.assertIn("square off", text)

    def test_a_clean_run_does_not_read_as_permission(self):
        """A preflight that sent nothing cannot authorise anything."""
        report = preflight.Report()
        _, text = self.verdict(report)
        self.assertIn("not permission", text)

    def test_info_lines_are_neither_failures_nor_warnings(self):
        report = preflight.Report()
        with redirect_stdout(io.StringIO()):
            report.add(preflight.INFO, "leg order", "auto")
        self.assertEqual(report.failures, [])
        self.assertEqual(report.warnings, [])


class TestItSendsNothing(unittest.TestCase):
    """The whole point of running this before Phase 3."""

    def test_no_order_call_appears_anywhere_in_the_module(self):
        import inspect

        source = inspect.getsource(preflight)
        # Call forms, not mentions: the module is allowed to say in prose
        # what place_leg would do, it is not allowed to call it.
        for forbidden in ("place_order(", "place_leg(", "cancel_record(",
                          "cancel_all(", "modify_order(", "cancel_order("):
            self.assertNotIn(forbidden, source, forbidden)

    def test_it_says_so_before_it_starts(self):
        self.assertIn("Nothing is sent", inspect_run_text())

    def test_the_inability_to_square_off_is_always_stated(self):
        self.assertIn("cannot flatten", inspect_run_text())


def inspect_run_text() -> str:
    import inspect

    return inspect.getsource(preflight.run)


class TestTheConfigFolderIsTheAppFolder(unittest.TestCase):
    """--config pointed at another folder used to leave the session, the logs
    and the state file next to the executable, so the preflight read a
    session belonging to a different account -- or none -- and said so in a
    way that sent you looking in the wrong place."""

    def test_the_base_directory_follows_the_config_file(self):
        import inspect

        import roll_app
        source = inspect.getsource(roll_app.main)
        self.assertIn("base = os.path.dirname(config_path)", source)
        self.assertIn('log_dir = os.path.join(base, "logs")', source)

    def test_preflight_is_a_command(self):
        import inspect

        import roll_app
        source = inspect.getsource(roll_app.main)
        self.assertIn('"--preflight"', source)
        self.assertIn("preflight.run(cfg, log, base)", source)

    def test_it_counts_as_a_command_line_run(self):
        """Without this the windowed build has nowhere to print."""
        import inspect

        import roll_app
        source = inspect.getsource(roll_app.main)
        line = source[source.index("on_command_line"):]
        self.assertIn("args.preflight", line[:200])


class TestAMissingSessionSaysSo(unittest.TestCase):
    """It used to report a missing file as a session belonging to another
    account, which is a different problem with a different remedy."""

    def broker(self, tmp):
        import os

        from rollover.broker import Broker
        from rollover.config import RollConfig

        class Log:
            def __init__(self): self.lines = []
            def info(self, m): self.lines.append(m)
            warn = error = alert = info

        cfg = RollConfig(vendor_id="V1", api_key="k", mobile_no="9",
                         base_url="https://example.invalid")
        log = Log()
        broker = Broker(cfg, log)
        broker.client = object()          # resume must not need a real one
        return broker, log, os.path.join(tmp, "session.json")

    def test_no_file_at_all(self):
        import shutil
        import tempfile

        tmp = tempfile.mkdtemp(prefix="sess_")
        self.addCleanup(shutil.rmtree, tmp, True)
        broker, log, path = self.broker(tmp)
        self.assertFalse(broker.resume(path))
        joined = " ".join(log.lines)
        self.assertIn("No session saved here yet", joined)
        self.assertNotIn("does not belong to the account", joined)

    def test_a_session_from_another_account_still_says_that(self):
        import json
        import shutil
        import tempfile

        tmp = tempfile.mkdtemp(prefix="sess_")
        self.addCleanup(shutil.rmtree, tmp, True)
        broker, log, path = self.broker(tmp)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"account": "someone else"}, handle)
        self.assertFalse(broker.resume(path))
        self.assertIn("does not belong to the account", " ".join(log.lines))


if __name__ == "__main__":
    unittest.main(verbosity=2)
