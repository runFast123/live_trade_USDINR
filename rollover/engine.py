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
from datetime import date, datetime
from typing import List, Optional, Tuple

from . import gates as gatelib
from . import ladder as ladderlib
from . import journal as journallib
from . import margin as marginlib
from . import reconcile
from . import limits as limitlib
from . import rule
from .broker import BUY, SELL, Broker, BrokerError, InstrumentInfo, OrderOutcome
from .config import RollConfig
from .feed import LiveFeed
from .logbook import Logbook
from .money import ceil_tick, money
from .quotes import Quote, QuoteError, QuoteReader
from .recorder import Recorder
from .state import StateStore

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
    margin: Optional[object] = None   # margin.Estimate, refreshed on the slow beat
    clips_done_today: int = 0
    lots_rolled: int = 0
    in_flight: bool = False
    halted_reason: Optional[str] = None
    scrip_file_date = None          # which day's scrip master is loaded
    quote_source: str = "starting"  # "live feed" or "polled"


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
    quote_source: str = ""


def _expiry(text):
    """A YYYY-MM-DD string from config as a date, or None."""
    try:
        return datetime.strptime(str(text).strip(), "%Y-%m-%d").date()
    except Exception:
        return None


class RollEngine:
    def __init__(self, cfg: RollConfig, log: Logbook, base_dir: str,
                 broker: Optional[Broker] = None):
        self.cfg = cfg
        self.log = log
        self.base_dir = base_dir
        # The login window hands over a broker that is already signed in.
        self.broker = broker or Broker(cfg, log)
        self.reader: Optional[QuoteReader] = None
        self.feed = LiveFeed(cfg, log)
        self.quote_source = "starting"
        self.recorder = (Recorder(os.path.join(base_dir, "data"),
                                  cfg.record_interval_sec)
                         if cfg.record_market else None)
        self.journal = journallib.Journal(os.path.join(base_dir, "data"),
                                          enabled=cfg.journal)
        # Whether the cost was clearing the limit on the previous tick,
        # so the crossing can be announced once rather than every tick.
        self._was_qualifying = False

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

        # The day's budget and any halt have to outlive the process: the
        # updater restarts it, and so does a crash. Loaded last, so it can
        # overwrite the defaults set above.
        self.store = StateStore(base_dir)
        saved = self.store.load()
        saved = self.store.for_campaign(saved, self.campaign_key())
        self.ladder = self._build_ladder()
        self.ladder_progress = dict(saved.ladder_done or {})
        self.session.clips_done_today = saved.clips_done
        self.session.lots_rolled = saved.lots_rolled
        self.session.halted_reason = saved.halted_reason
        if saved.halted:
            self._state = HALTED
            self._note = saved.halted_reason
            log.alert(f"Resumed with a halt still in force: {saved.halted_reason}")
        elif saved.clips_done:
            log.info(f"Resumed: {saved.clips_done} clip(s) already done today.")

    # --------------------------------------------------------------- controls
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="roll-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.recorder is not None:
            limit_bps = None
            snap = self._snapshot
            if snap is not None and snap.decision is not None:
                limit_bps = snap.decision.limit_bps
            for line in self.recorder.summary(limit_bps).splitlines():
                self.log.info(line)
        self.feed.stop()
        if self._thread:
            self._thread.join(timeout=5)

    def restart(self) -> None:
        """Re-read the configured contracts and start watching again.

        Used when the operator changes the legs. The day's clip count is kept,
        so switching contracts cannot be used to get around the daily budget.
        """
        if self.session.halted_reason:
            # Changing contracts is not a way to make a halt go away. Whatever
            # is wrong with the position is still wrong.
            self.log.warn("Not restarting: a halt is in force. Clear it first.")
            return

        self.stop()
        with self._lock:
            self._armed_until = None
            self._snapshot = None
            self.session.near = None
            self.session.far = None
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
        self.journal.halt(reason)
        self._persist()

    def clear_halt(self) -> None:
        with self._lock:
            self.session.halted_reason = None
            self._state = STARTING
            self._note = "halt cleared"
        self.log.info("Halt cleared by operator.")
        self._persist()
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

        self.session.scrip_file_date = self.broker.scrip_file_date

        if self.cfg.use_live_feed:
            self.feed.start(self.broker.client,
                            [self.session.near.token, self.session.far.token])

        for role, info in (("Near leg (SELL)", self.session.near),
                           ("Far leg  (BUY) ", self.session.far)):
            self.log.info(
                f"{role}: {info.label()} [{info.instrument}] expiry {info.expiry} "
                f"lot {info.lot_size} tick {info.tick} divisor {info.price_divisor} "
                f"limits {info.low_range}..{info.high_range}")

    def _slow_refresh(self) -> None:
        """Position, market status, and the day's scrip master."""
        try:
            if self.broker.refresh_scrip_master_if_stale():
                # New day, new file: the contracts must be read again, since
                # their expiries and circuit limits have all moved.
                self.session.near = self.broker.instrument(self.cfg.near_token)
                self.session.far = self.broker.instrument(self.cfg.far_token)
                self.reader.set_instruments({
                    self.session.near.token: self.session.near,
                    self.session.far.token: self.session.far,
                })
                self.session.scrip_file_date = self.broker.scrip_file_date
                self.log.info("Contracts re-read from the new scrip master.")
        except Exception as exc:
            self.log.error(f"Could not refresh the scrip master: {exc}")

        self.session.market_open = self.broker.market_open()
        if self.cfg.require_position and self.session.near:
            self.session.near_position_qty = self.broker.long_qty(self.session.near.token)

        # Margin moves with the market, so it is refreshed on the slow beat
        # rather than every tick. This is the screen's copy; the one that
        # actually stops an order is taken immediately before leg 1.
        if self.cfg.require_margin and not self.cfg.dry_run:
            self.session.margin = marginlib.estimate(
                self.broker, self.cfg.near_token, self.cfg.far_token,
                self.cfg.clip_qty)

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

    def _count_clip(self, qty: int) -> None:
        """Record a completed clip, on disk as well as in memory."""
        self.session.clips_done_today += 1
        self.session.lots_rolled += int(qty or 0)
        self._persist()

    def _persist(self) -> None:
        """Write the day's state out. Never raises."""
        from .state import DayState

        try:
            state = DayState(
                trading_date=date.today().isoformat(),
                clips_done=self.session.clips_done_today,
                lots_rolled=self.session.lots_rolled,
                halted_reason=self.session.halted_reason,
                halted_at=(datetime.now().astimezone().isoformat(timespec="seconds")
                           if self.session.halted_reason else None),
                ladder_campaign=self.campaign_key(),
                ladder_done=dict(getattr(self, "ladder_progress", {}) or {}),
            )
            if not self.store.save(state):
                self.log.warn("Could not write the state file; a restart would "
                              "forget today's clips and any halt.")
        except Exception as exc:
            self.log.warn(f"Could not write the state file: {exc}")

    def _announce_crossing(self, decision) -> None:
        """Say something the first time the cost clears the limit.

        With a tight limit the qualifying windows are rare and brief, and the
        operator is not necessarily watching. Announced on the transition only,
        so it is a signal rather than a stream.
        """
        if not self.cfg.alert_on_qualify or decision is None:
            return

        now_qualifying = bool(decision.qualifies)
        if now_qualifying and not self._was_qualifying:
            bps = decision.cost_bps
            self.log.alert(
                "The roll cost has come below the limit: "
                + (f"{money(bps, 1)} bps against {money(decision.limit_bps, 1)}"
                   if bps is not None and decision.limit_bps is not None
                   else f"{money(decision.roll_cost)} against {money(decision.limit)}"))
        elif self._was_qualifying and not now_qualifying:
            self.log.info("The roll cost has gone back above the limit.")
        self._was_qualifying = now_qualifying

    def _read_quotes(self, tokens: List[str]):
        """Take the websocket when it is live, and poll only when it is not.

        The REST touchline is a cached snapshot and has been seen many minutes
        behind, so it is the fallback rather than the source.
        """
        if self.cfg.use_live_feed and self.feed.healthy(
                tokens, self.cfg.feed_max_silence):
            quotes = self.reader.from_feed(self.feed.ticks(), tokens)
            self._set_source("live feed")
            return quotes

        quotes = self.reader.fetch(tokens)
        self._set_source("polled" if not self.feed.connected
                         else "polled, feed stale")
        return quotes

    # ---- the ladder -------------------------------------------------------
    def campaign_key(self) -> str:
        return ladderlib.campaign_key(self.cfg.near_token, self.cfg.far_token)

    def _build_ladder(self) -> "ladderlib.Ladder":
        """Parse the configured ladder, capped at the limit for this tenor.

        A rung looser than the tenor limit would quietly spend more than the
        client's instruction allows, so it is refused. Refusing means running
        without a ladder, on the single limit, which is the safe direction.
        """
        raw = getattr(self.cfg, "limit_ladder", None)
        if not raw:
            return ladderlib.Ladder([])
        try:
            ceiling = self._tenor_ceiling_bps()
            built = ladderlib.parse(raw, lot_size=self.cfg.expected_lot_size,
                                    ceiling_bps=ceiling)
        except Exception as exc:
            self.log.error(
                f"The ladder in config.json cannot be used ({exc}). Running on "
                "the single tenor limit instead.")
            return ladderlib.Ladder([])

        if built:
            self.log.info(
                f"Ladder: {', '.join(r.describe(self.cfg.expected_lot_size) for r in built.rungs)}"
                f"  (total {built.total_qty:,})")
        return built

    def _tenor_ceiling_bps(self):
        """The bps limit for this pair of contracts, if it can be determined."""
        if self.cfg.limit_mode != "bps":
            return None
        try:
            from .limits import match_tenor, parse_schedule
            days = self.tenor_days()
            if days is None:
                return None
            _, bps = match_tenor(days, parse_schedule(self.cfg.limit_bps_schedule),
                                 self.cfg.tenor_tolerance_days)
            return bps
        except Exception:
            return None

    def _confirm_trades(self, leg1, leg2, before_trades) -> None:
        """Compare the order book's fills against the exchange's trade record.

        Two independent accounts of the same event. The order book is what the
        app has always read; the trade book is what actually executed. This
        does not halt on its own -- a trade book whose row shape is not yet
        known would then halt every roll -- but a disagreement is written down
        loudly, because it means one of the two is wrong and the roll is about
        to be declared complete on the strength of it.
        """
        if before_trades is None:
            self.log.warn("The trade book could not be read before the roll, so "
                          "the fills rest on the order book alone.")
            return

        for leg, side, outcome in (("near", SELL, leg1), ("far", BUY, leg2)):
            token = (self.cfg.near_token if leg == "near" else self.cfg.far_token)
            traded, detail = self.broker.traded_since(before_trades, token, side)
            if traded is None:
                self.log.warn(f"Trade book, {leg} leg: {detail}")
            elif traded != outcome.filled_qty:
                self.log.alert(
                    f"TRADE BOOK DISAGREES on the {leg} leg: the order book says "
                    f"{outcome.filled_qty} filled, the trade book shows {traded} "
                    f"({detail}). One of them is wrong; check the terminal.")
            else:
                self.log.info(f"Trade book confirms the {leg} leg: {traded}.")

    def _credit_rung(self, decision, qty: int) -> None:
        """Record a fill against the rung that was being worked."""
        rung = getattr(decision, "active_rung", None)
        if rung is None or not self.ladder or qty <= 0:
            return
        self.ladder_progress = self.ladder.credit(
            self.ladder_progress, rung.rung, qty)
        done = self.ladder.done_total(self.ladder_progress)
        self.log.info(
            f"Ladder: {qty:,} credited to the {money(rung.rung.bps, 0)} bps rung; "
            f"{done:,} of {self.ladder.total_qty:,} rolled, "
            f"{self.ladder.remaining_total(self.ladder_progress):,} left.")

    def cancel_all(self) -> Tuple[int, int, Optional[str]]:
        """Cancel every order still live on either leg.

        Returns (cancelled, failed, problem). `problem` is set when the book
        could not be read at all, which is the case a caller must not read as
        "nothing was working".
        """
        if self.cfg.dry_run:
            return 0, 0, None

        tokens = [t for t in (self.cfg.near_token, self.cfg.far_token) if t]
        live = self.broker.working_orders(tokens)
        if live is None:
            return 0, 0, ("the order book could not be read, so it is not known "
                          "whether anything is still working at the exchange")
        if not live:
            return 0, 0, None

        self.log.warn(f"Cancelling {len(live)} working order(s).")
        cancelled = failed = 0
        for record in live:
            if self.broker.cancel_record(record):
                cancelled += 1
            else:
                failed += 1
        return cancelled, failed, None

    def shutdown(self) -> Optional[str]:
        """Close down without leaving orders behind.

        A Day order outlives this process at the exchange, and the synthetic
        IOC that would have cancelled it dies with the process. Closing the
        window therefore has to cancel, not merely stop watching.

        Returns a message when something was left in doubt, so the window can
        show it rather than closing over the top of it.
        """
        self.disarm("window closing")

        problem = None
        try:
            cancelled, failed, unreadable = self.cancel_all()
            if unreadable:
                problem = unreadable + ". Check the terminal."
            elif failed:
                problem = (f"{failed} order(s) could not be cancelled and may "
                           "still be live at the exchange. Check the terminal.")
            elif cancelled:
                self.log.info(f"Cancelled {cancelled} working order(s) on the way out.")
        except Exception as exc:
            problem = f"cancelling on the way out failed: {exc}. Check the terminal."

        if problem:
            self.log.alert(problem)
        self.stop()
        return problem

    def rebuild_ladder(self) -> None:
        """Re-read the ladder after the operator has edited it.

        Progress is kept where a rung still exists at the same limit, and
        dropped where it does not: a rung that has been deleted or repriced is
        a different commitment, and carrying its history onto a new number
        would report a fresh rung as already part done.
        """
        old = dict(self.ladder_progress)
        self.ladder = self._build_ladder()
        keys = {r.key for r in self.ladder.rungs}
        self.ladder_progress = {k: v for k, v in old.items() if k in keys}

        dropped = sorted(set(old) - keys)
        if dropped:
            self.log.warn(
                "Ladder progress dropped for rung(s) no longer in the ladder: "
                + ", ".join(f"{k} bps ({old[k]:,})" for k in dropped))
        self._persist()

    def reset_ladder(self) -> None:
        """Start the campaign again. The operator's decision, never the app's."""
        self.ladder_progress = {}
        self._persist()
        self.log.warn("Ladder progress reset. The whole campaign is outstanding again.")

    def tenor_days(self) -> Optional[int]:
        """How far apart the two contracts expire.

        The limit depends on this, so changing contracts changes the limit.
        That is the point: a one month roll and a two month roll are not the
        same trade and must not share a number.
        """
        near, far = self.session.near, self.session.far
        if near is not None and far is not None:
            return limitlib.tenor_days(near.expiry, far.expiry)

        # Before the contracts have been read, fall back to the expiries in
        # config.json. Those are cross-checked against the scrip master by a
        # gate, so they are not authoritative -- but they are good enough to
        # know whether a ladder rung is inside the limit for this tenor, and
        # the alternative is no ceiling at all.
        return limitlib.tenor_days(_expiry(self.cfg.near_expiry),
                                   _expiry(self.cfg.far_expiry))

    def _set_source(self, source: str) -> None:
        if source != self.quote_source:
            self.log.info(f"Quotes now coming from the {source}.")
        self.quote_source = source
        self.session.quote_source = source

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

        quotes = self._read_quotes(tokens)
        self._last_complaint = ("", 0.0)
        near_q = quotes[self.session.near.token]
        far_q = quotes[self.session.far.token]
        decision = rule.compute(near_q, far_q, self.cfg, days=self.tenor_days(),
                                ladder=self.ladder,
                                progress=self.ladder_progress)
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

        if self.recorder is not None:
            self.recorder.sample(near_q, far_q, decision, report, self.quote_source)
        self._announce_crossing(decision)

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
        self.journal.decision(decision, dry_run=self.cfg.dry_run, armed=True)

        # Read before anything is sent. Both are the baseline for afterwards:
        # the position book says what the account held, the trade book says
        # what had already executed today, and without the "before" neither
        # can tell our fills apart from everyone else's.
        before_positions = None
        before_trades = None
        if not self.cfg.dry_run:
            before_positions = reconcile.capture(
                self.broker, self.cfg.near_token, self.cfg.far_token)
            before_trades = self.broker._trade_snapshot()
            self.log.info(f"Before the roll: {before_positions.describe()}")

        # The gate's copy is up to a slow beat old and was sized on the
        # configured clip. This one is taken now, for the quantity actually
        # about to be sent, and is what stops the order.
        if self.cfg.require_margin and not self.cfg.dry_run:
            estimate = marginlib.estimate(
                self.broker, self.cfg.near_token, self.cfg.far_token,
                decision.qty)
            self.session.margin = estimate
            self.journal.margin(estimate, decision.qty)
            if estimate.affordable is not True:
                self.session.in_flight = False
                self.disarm("margin")
                self.log.alert(
                    "NOT SENDING: " + (estimate.describe() if estimate.known
                                       else estimate.detail)
                    + ". Nothing was sent, and the app has disarmed.")
                self._set_state(WATCHING, "margin")
                return

        try:
            leg1 = self.broker.place_leg(
                self.session.near, SELL, decision.qty, decision.sell_limit,
                "leg 1 near SELL")
            self.log.info(f"leg 1 result: {leg1.detail}")
            self.journal.order("near", SELL, self.cfg.near_token, decision.qty,
                               decision.sell_limit, leg1)

            if self.cfg.dry_run:
                leg2_price = decision.buy_limit
                self.log.info(
                    f"DRY RUN, not sent -- leg 2 far BUY {decision.qty} of "
                    f"{self.session.far.token} at {money(leg2_price)}")
                self.log.info("DRY RUN complete. No orders reached the exchange.")
                self._count_clip(decision.qty)
                self._credit_rung(decision, decision.qty)
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
            self.journal.order("far", BUY, self.cfg.far_token, leg1.filled_qty,
                               decision.buy_limit, leg2)

            if leg2.fully_filled:
                self._confirm_trades(leg1, leg2, before_trades)

                # The order book is the broker's summary of what it believes.
                # The position book is what the account actually holds. Saying
                # ROLL COMPLETE without comparing them takes one on trust.
                verdict = reconcile.check(
                    self.broker, self.cfg.near_token, self.cfg.far_token,
                    before_positions or reconcile.Positions(None, None),
                    leg2.filled_qty, wait=self.cfg.reconcile_wait_sec)
                self.journal.reconciliation(verdict)
                if verdict.contradicted:
                    # Count the clip before halting: whatever else is wrong,
                    # this much was sent, and the daily budget must reflect it.
                    self._count_clip(leg2.filled_qty)
                    self.halt("BOTH LEGS REPORTED FILLED, BUT " + verdict.detail)
                    return
                if verdict.checked:
                    self.log.info(verdict.detail)
                else:
                    self.log.warn(verdict.detail)

                self._count_clip(leg2.filled_qty)
                self._credit_rung(decision, leg2.filled_qty)
                self.log.info(
                    f"ROLL COMPLETE: sold {leg1.filled_qty} near, bought {leg2.filled_qty} far, "
                    f"at a booked cost near {money(decision.roll_cost)} per unit.")
                self.journal.note(
                    "complete", "roll complete",
                    near_filled=leg1.filled_qty, far_filled=leg2.filled_qty,
                    roll_cost=decision.roll_cost, cost_bps=decision.cost_bps,
                    limit_bps=decision.limit_bps,
                    clips_done=self.session.clips_done_today,
                    ladder_done=dict(self.ladder_progress))
                self._slow_refresh()
                return

            short_by = leg1.filled_qty - leg2.filled_qty
            self._handle_leg2_failure(leg1, leg2, short_by, near_q)

        except Exception as exc:
            # Anything unexpected in here can have left a leg filled. Without
            # this the exception escaped to the watch loop, which logged it and
            # carried on: no halt, every gate passing again, and the operator
            # able to arm on top of a position that is already half moved.
            self.halt(
                f"{type(exc).__name__} during execution: {exc}. A leg may have "
                "filled. Check the terminal and the position book before doing "
                "anything else.")
            self.log.error(f"Execution failed after the roll had started: {exc!r}")

        finally:
            self.session.in_flight = False

    def _handle_leg2_failure(self, leg1: OrderOutcome, leg2: OrderOutcome,
                             short_by: int, near_q: Quote) -> None:
        message = (
            f"HALF ROLLED. The near leg sold {leg1.filled_qty} units but the far leg "
            f"only bought {leg2.filled_qty}. You are short {short_by} units of the "
            f"intended position. Far leg detail: {leg2.detail}"
        )
        if self.ladder:
            # Crediting a rung from a half-rolled clip would be a guess in one
            # direction or the other, and the position has to be reconciled by
            # hand regardless. Say so rather than quietly picking a number.
            message += (
                f" The ladder has NOT been credited with the {leg2.filled_qty} that did "
                "roll; check the position book and use Reset ladder if the "
                "progress shown no longer matches it.")
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
            quote_source=self.quote_source,
        )
        with self._lock:
            self._snapshot = snap
