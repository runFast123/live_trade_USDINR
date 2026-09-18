"""The live-mode dialog, actually built.

Everything else about live mode is tested without a display, which is the right
way round. But the dialog is the part an operator touches, and the failures it
can have are Tk failures: a colour constant that does not exist, a helper called
with the wrong arguments, a button wired to a method that was renamed. None of
those show up in the logic tests, and all of them are the sort of thing that
only appears the moment somebody clicks the button.

Skipped where there is no display, so a headless CI run stays green.
"""
from __future__ import annotations

import unittest
from datetime import date

try:
    import tkinter as tk
    _root = tk.Tk()
    _root.withdraw()
    HAVE_TK = True
except Exception:                       # pragma: no cover - headless
    _root = None
    HAVE_TK = False

from rollover.config import RollConfig
from rollover.livemode import CONFIRM
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


class StubQuote:
    def __init__(self, ask):
        self.ask = ask


class StubSnapshot:
    def __init__(self, ask=None):
        self.near_quote = StubQuote(ask) if ask else None
        self.far_quote = None


class StubInstrument:
    lot_size = 1000


class StubSession:
    def __init__(self, halted=None):
        self.halted_reason = halted
        self.near = StubInstrument()
        self.far = StubInstrument()


class StubBroker:
    logged_in = True
    scrip_file_date = date.today()


class StubEngine:
    def __init__(self, halted=None, ask=D("95.78")):
        self.broker = StubBroker()
        self.session = StubSession(halted)
        self.feed = None
        self.disarmed = []
        self._ask = ask

    def snapshot(self):
        return StubSnapshot(self._ask)

    def disarm(self, reason="operator"):
        self.disarmed.append(reason)


def config(**kw):
    kw.setdefault("near_token", "1769")
    kw.setdefault("far_token", "1584")
    kw.setdefault("dry_run", True)
    return RollConfig(**kw)


@unittest.skipUnless(HAVE_TK, "no display")
class DialogCase(unittest.TestCase):
    def build(self, cfg=None, engine=None, feed_ok=True):
        from rollover import livemode, theme
        from rollover.ui import LiveModeDialog

        self.cfg = cfg or config()
        self.engine = engine or StubEngine()
        self.log = StubLog()

        # The feed check needs a websocket; stand in for it so the dialog can
        # be built in both the blocked and the clear state.
        self.real_feed_check = livemode._feed_is_healthy
        livemode._feed_is_healthy = lambda _engine: feed_ok
        self.addCleanup(setattr, livemode, "_feed_is_healthy",
                        self.real_feed_check)

        frame = tk.Toplevel(_root)
        frame.withdraw()
        self.addCleanup(frame.destroy)

        dialog = LiveModeDialog(frame, self.cfg, self.engine,
                                theme.Fonts(), self.log)
        dialog.withdraw()
        try:
            dialog.grab_release()
        except tk.TclError:
            pass
        self.addCleanup(dialog.destroy)
        return dialog


class TestItBuildsAtAll(DialogCase):
    def test_a_blocked_dialog_builds(self):
        """Every theme colour and helper it names has to exist."""
        dialog = self.build(config(quantity_unit_confirmed=False))
        self.assertTrue(dialog.blocked)

    def test_a_clear_dialog_builds(self):
        dialog = self.build(config(quantity_unit_confirmed=True))
        self.assertFalse(dialog.blocked)

    def test_it_builds_without_a_price(self):
        dialog = self.build(config(quantity_unit_confirmed=True),
                            StubEngine(ask=None))
        self.assertIsNone(dialog.exposure.price)


class TestTheBlockedState(DialogCase):
    def setUp(self):
        self.dialog = self.build(config(quantity_unit_confirmed=False))

    def test_there_is_no_way_to_confirm(self):
        self.assertFalse(hasattr(self.dialog, "go"))
        self.assertFalse(hasattr(self.dialog, "entry"))

    def test_confirming_anyway_does_nothing(self):
        self.dialog._confirm()
        self.assertTrue(self.cfg.dry_run)


class TestTheConfirmation(DialogCase):
    def setUp(self):
        self.dialog = self.build(config(quantity_unit_confirmed=True))

    def type(self, text):
        self.dialog.entry.delete(0, "end")
        self.dialog.entry.insert(0, text)
        self.dialog._retest()

    def test_the_button_starts_disabled(self):
        self.assertFalse(self.dialog.go._enabled)

    def test_the_exact_phrase_enables_it(self):
        self.type(CONFIRM)
        self.assertTrue(self.dialog.go._enabled)

    def test_anything_else_leaves_it_disabled(self):
        for text in ("", "go live", "GO", "GOLIVE", "Go Live"):
            self.type(text)
            self.assertFalse(self.dialog.go._enabled, msg=text)

    def test_deleting_it_again_disables_it(self):
        self.type(CONFIRM)
        self.type("GO LIV")
        self.assertFalse(self.dialog.go._enabled)

    def test_confirming_switches_the_running_config(self):
        self.type(CONFIRM)
        self.dialog._confirm()
        self.assertFalse(self.cfg.dry_run)

    def test_it_says_so_loudly_in_the_log(self):
        self.type(CONFIRM)
        self.dialog._confirm()
        self.assertIn("LIVE ORDERS ARE ENABLED", self.log.text())
        self.assertIn("ALERT", self.log.text())

    def test_it_disarms_on_the_way_in(self):
        self.type(CONFIRM)
        self.dialog._confirm()
        self.assertEqual(self.engine.disarmed, ["switched to live"])


class TestTheStateIsRecheckedAtTheLastMoment(DialogCase):
    def test_a_halt_arriving_while_the_dialog_is_open_stops_it(self):
        """The checks drawn on screen are a minute old by the time it is read."""
        dialog = self.build(config(quantity_unit_confirmed=True))
        dialog.entry.insert(0, CONFIRM)

        self.engine.session.halted_reason = "HALF ROLLED, short 600"

        import tkinter.messagebox as mb
        seen = []
        real = mb.showwarning
        mb.showwarning = lambda *a, **kw: seen.append(a)
        try:
            dialog._confirm()
        finally:
            mb.showwarning = real

        self.assertTrue(self.cfg.dry_run, "went live despite a fresh halt")
        self.assertEqual(len(seen), 1)
        self.assertIn("Live mode refused", self.log.text())

    def test_the_feed_dropping_while_it_is_open_stops_it(self):
        from rollover import livemode
        dialog = self.build(config(quantity_unit_confirmed=True))
        dialog.entry.insert(0, CONFIRM)

        livemode._feed_is_healthy = lambda _engine: False

        import tkinter.messagebox as mb
        real = mb.showwarning
        mb.showwarning = lambda *a, **kw: None
        try:
            dialog._confirm()
        finally:
            mb.showwarning = real

        self.assertTrue(self.cfg.dry_run, "went live on a dead feed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
