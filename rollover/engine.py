"""The watching and execution loop.

The engine owns one background thread. It reads quotes, evaluates the rule and
the gates, and publishes a Snapshot that the screen reads. Orders are only ever
sent from this thread, and only while the operator has armed it.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from . import gates as gatelib
from . import rule
from .broker import BUY, SELL, Broker, BrokerError, InstrumentInfo, OrderOutcome
from .config import RollConfig
from .logbook import Logbook
from .money import ceil_tick, money
from .quotes import Quote, QuoteError, QuoteReader

# States the operator sees on screen.
STARTING = "STARTING"
WATCHING = "WATCHING"
ARMED = "ARMED"
WORKING = "WORKING"
DONE = "DONE"
HALTED = "HALTED"


@dataclass
class SessionState:
    """What the gates read. Updated only by the engine thread."""
    logged_in: bool = False
    near: Optional[InstrumentInfo] = None
    far: Optional[InstrumentInfo] = None
    market_open: Optional[bool] = None
    near_position_qty: Optional[int] = None
    clips_done_today: int = 0
    in_flight: bool = False
    halted_reason: Optional[str] = None


@dataclass
class Snapshot:
    """An immutable view of the world for the screen. Never mutated in place."""
    at: datetime
    state: str
    dry_run: bool
    armed_until: Optional[float]
    near: Optional[InstrumentInfo]
    far: Optional[InstrumentInfo]
    near_quote: Optional[Quote]
    far_quote: Optional[Quote]
    decision: Optional[rule.RollDecision]
    report: Optional[gatelib.GateReport]
    note: str = ""
    clips_done: int = 0
    halted_reason: Optional[str] = None


class RollEngine:
    def __init__(self, cfg: RollConfig, log: Logbook, base_dir: str,
                 broker: Optional[Broker] = None):
        self.cfg = cfg
        self.log = log
        self.base_dir = base_dir
        # The login window hands over a broker that is already signed in.
        self.broker = broker or Broker(cfg, log)
        self.reader: Optional[QuoteReader] = None
        self.session = SessionState()

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._armed_until: Optional[float] = None
        self._lock = threading.Lock()
        self._state = STARTING
        self._snapshot: Optional[Snapshot] = None
        self._note = "not started"
        self._last_slow_refresh = 0.0
        self._last_complaint = ("", 0.0)

    # --------------------------------------------------------------- controls
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="roll-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def restart(self) -> None:
        """Re-read the configured contracts and start watching again.

        Used when the operator changes the legs. The day's clip count is kept,
        so switching contracts cannot be used to get around the daily budget.
        """
        self.stop()
        with self._lock:
            self._armed_until = None
            self._snapshot = None
            self.session.near = None
            self.session.far = None
            self.session.halted_reason = None
            self.session.in_flight = False
            self._state = STARTING
            self._note = "reloading contracts"
        self._stop.clear()
        self.start()

    def arm(self) -> None:
        """Allow the next qualifying quote to trade. Expires on its own."""
        with self._lock:
            if self.session.halted_reason:
                self.log.warn("Cannot arm while halted.")
                return
            self._armed_until = time.monotonic() + self.cfg.arm_timeout_sec
        self.log.info(
            f"ARMED for {self.cfg.arm_timeout_sec}s "
            f"({'dry run, nothing will be sent' if self.cfg.dry_run else 'LIVE ORDERS'})"
        )

    def disarm(self, reason: str = "operator") -> None:
        with self._lock:
            was_armed = self._armed_until is not None
            self._armed_until = None
        if was_armed:
            self.log.info(f"Disarmed ({reason}).")

    def halt(self, reason: str) -> None:
        with self._lock:
            self.session.halted_reason = reason
            self._armed_until = None
            self._state = HALTED
            self._note = reason
        self.log.alert(f"HALTED: {reason}")

    def clear_halt(self) -> None:
        with self._lock:
            self.session.halted_reason = None
            self._state = STARTING
            self._note = "halt cleared"
        self.log.info("Halt cleared by operator.")
        # A halt during startup ends the loop thread, so clearing it has to
        # start the loop again or the button would do nothing.
        if not (self._thread and self._thread.is_alive()):
            self.log.info("Restarting the watch loop.")
            self.start()

    @property
    def armed(self) -> bool:
        with self._lock:
            return (self._armed_until is not None
                    and time.monotonic() < self._armed_until)

    def snapshot(self) -> Optional[Snapshot]:
        with self._lock:
            return self._snapshot

    # ------------------------------------------------------------- setup work
    def _connect(self) -> None:
        session_path = os.path.join(self.base_dir, "session.json")
        if self.broker.logged_in:
            self.broker.load_scrip_master()
        else:
            self.broker.connect(session_path)
        self.session.logged_in = self.broker.logged_in
        self.reader = QuoteReader(self.broker.client, self.cfg)

        if not self.cfg.near_token or not self.cfg.far_token:
            raise BrokerError(
                "near_token and far_token must be set in config.json. "
                "Run the app with --find USDINR to list the contracts and their tokens."
            )

        self.session.near = self.broker.instrument(self.cfg.near_token)
        self.session.far = self.broker.instrument(self.cfg.far_token)

        # The scrip master states the price scale and the tick for each
        # contract, so the quote reader checks a declared scale instead of
        # guessing one.
        self.reader.set_instruments({
            self.session.near.token: self.session.near,
            self.session.far.token: self.session.far,
        })

        for role, info in (("Near leg (SELL)", self.session.near),
                           ("Far leg  (BUY) ", self.session.far)):
            self.log.info(
                f"{role}: {info.label()} [{info.instrument}] expiry {info.expiry} "
                f"lot {info.lot_size} tick {info.tick} divisor {info.price_divisor} "
                f"limits {info.low_range}..{info.high_range}")

    def _slow_refresh(self) -> None:
        """Position and market status, which do not need to be read every tick."""
        self.session.market_open = self.broker.market_open()
        if self.cfg.require_position and self.session.near:
            self.session.near_position_qty = self.broker.long_qty(self.session.near.token)

    # ------------------------------------------------------------------- loop
    def _run(self) -> None:
        try:
            self._set_state(STARTING, "connecting")
            self._connect()
            self._slow_refresh()
            self._last_slow_refresh = time.monotonic()
            self._set_state(WATCHING, "watching both legs")
        except Exception as exc:
            self.log.error(f"Startup failed: {exc}")
            self.halt(f"startup failed: {exc}")
            self._publish(None, None, None, str(exc))
            return

        tokens = [self.session.near.token, self.session.far.token]

        while not self._stop.is_set():
            cycle_started = time.monotonic()
            try:
                self._tick(tokens)
            except QuoteError as exc:
                self._publish(None, None, None, f"quote problem: {exc}")
                self._complain("warn", f"Quote problem, not trading this tick: {exc}")
            except Exception as exc:
                self._publish(None, None, None, f"error: {exc}")
                self._complain("error", f"Unexpected error in the watch loop: {exc}")

            elapsed = time.monotonic() - cycle_started
            self._stop.wait(max(0.1, self.cfg.poll_interval_sec - elapsed))

    def _complain(self, level: str, message: str, every: float = 60.0) -> None:
        """Log a recurring problem once, then at most once a minute.

        The watch loop runs every second, so a problem that persists would
        otherwise write the same line thousands of times and bury the one event
        that matters. The screen still shows it continuously.
        """
        last_message, last_at = self._last_complaint
        now = time.monotonic()
        if message == last_message and now - last_at < every:
            return
        self._last_complaint = (message, now)
        getattr(self.log, level)(message)

    def _tick(self, tokens: List[str]) -> None:
        now = time.monotonic()
        if now - self._last_slow_refresh > 15:
            self._slow_refresh()
            self._last_slow_refresh = now

        quotes = self.reader.fetch(tokens)
        self._last_complaint = ("", 0.0)
        near_q = quotes[self.session.near.token]
        far_q = quotes[self.session.far.token]
        decision = rule.compute(near_q, far_q, self.cfg)
        report = gatelib.evaluate(self.cfg, self.session, quotes, decision)

        if self.session.halted_reason:
            self._set_state(HALTED, self.session.halted_reason)
        elif self.armed:
            self._set_state(ARMED, "armed, waiting for a qualifying quote")
        elif self.session.clips_done_today >= self.cfg.max_clips_per_day:
            self._set_state(DONE, "the day's clips are done")
        else:
            self._set_state(WATCHING, "watching both legs")

        self._publish(near_q, far_q, decision, self._note, report)

        if not self.armed:
            return
        if not report.ok:
            return

        self.disarm("condition met, firing")
        self._execute(decision, near_q, far_q)

    # -------------------------------------------------------------- execution
    def _execute(self, decision: rule.RollDecision, near_q: Quote, far_q: Quote) -> None:
        """Sell the near leg, confirm, then buy the far leg for exactly what filled.

        The near leg goes first on purpose. If the second leg fails after the
        first has filled, this order of operations leaves the account flat,
        which can be corrected. Buying the far leg first would leave it long
        two contracts with one of them about to expire.
        """
        self.session.in_flight = True
        self._set_state(WORKING, "sending the near leg")
        self.log.info("--- ROLL ---")
        self.log.info(decision.describe().replace("\n", " | "))

        try:
            leg1 = self.broker.place_leg(
                self.session.near, SELL, decision.qty, decision.sell_limit,
                "leg 1 near SELL")
            self.log.info(f"leg 1 result: {leg1.detail}")

            if self.cfg.dry_run:
                leg2_price = decision.buy_limit
                self.log.info(
                    f"DRY RUN, not sent -- leg 2 far BUY {decision.qty} of "
                    f"{self.session.far.token} at {money(leg2_price)}")
                self.log.info("DRY RUN complete. No orders reached the exchange.")
                self.session.clips_done_today += 1
                return

            if not leg1.certain:
                self.halt("the near leg was sent but its fill could not be confirmed: "
                          f"{leg1.detail}. Check the terminal before doing anything else.")
                return

            if leg1.filled_qty == 0:
                self.log.info("Near leg did not fill and was cancelled. "
                              "No exposure changed. Back to watching.")
                return

            self._set_state(WORKING, "sending the far leg")
            leg2 = self.broker.place_leg(
                self.session.far, BUY, leg1.filled_qty, decision.buy_limit,
                "leg 2 far BUY")
            self.log.info(f"leg 2 result: {leg2.detail}")

            if leg2.fully_filled:
                self.session.clips_done_today += 1
                self.log.info(
                    f"ROLL COMPLETE: sold {leg1.filled_qty} near, bought {leg2.filled_qty} far, "
                    f"at a booked cost near {money(decision.roll_cost)} per unit.")
                self._slow_refresh()
                return

            short_by = leg1.filled_qty - leg2.filled_qty
            self._handle_leg2_failure(leg1, leg2, short_by, near_q)

        finally:
            self.session.in_flight = False

    def _handle_leg2_failure(self, leg1: OrderOutcome, leg2: OrderOutcome,
                             short_by: int, near_q: Quote) -> None:
        message = (
            f"HALF ROLLED. The near leg sold {leg1.filled_qty} units but the far leg "
            f"only bought {leg2.filled_qty}. You are short {short_by} units of the "
            f"intended position. Far leg detail: {leg2.detail}"
        )
        self.log.alert(message)

        if not self.cfg.auto_unwind_on_leg2_failure:
            self.halt(message + " Auto unwind is off, so this needs you at the terminal now.")
            return

        # Buy the near contract back so the account returns to where it started.
        unwind_price = ceil_tick(near_q.ask + self.cfg.tick_d * 4, self.cfg.tick_d)
        self.log.alert(f"Auto unwind: buying back {short_by} of the near contract "
                       f"at {money(unwind_price)}")
        undo = self.broker.place_leg(
            self.session.near, BUY, short_by, unwind_price, "unwind near BUY")
        if undo.fully_filled:
            self.halt(message + " The near leg was bought back, so the position is "
                                "roughly where it started. No roll happened.")
        else:
            self.halt(message + f" The unwind also failed ({undo.detail}). "
                                "Go to the terminal immediately.")

    # ------------------------------------------------------------- publishing
    def _set_state(self, state: str, note: str) -> None:
        self._state = state
        self._note = note

    def _publish(self, near_q, far_q, decision, note: str,
                 report: Optional[gatelib.GateReport] = None) -> None:
        snap = Snapshot(
            at=datetime.now(),
            state=self._state,
            dry_run=self.cfg.dry_run,
            armed_until=self._armed_until,
            near=self.session.near,
            far=self.session.far,
            near_quote=near_q,
            far_quote=far_q,
            decision=decision,
            report=report,
            note=note,
            clips_done=self.session.clips_done_today,
            halted_reason=self.session.halted_reason,
        )
        with self._lock:
            self._snapshot = snap
