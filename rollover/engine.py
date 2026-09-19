"""The watching and execution loop.

The engine owns one background thread. It reads quotes, evaluates the rule and
the gates, and publishes a Snapshot that the screen reads. Orders are only ever
sent from this thread, and only while the operator has armed it.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Optional, Tuple

from . import gates as gatelib
from . import ladder as ladderlib
from . import journal as journallib
from . import margin as marginlib
from . import book
from . import reconcile
from . import sections as sectionlib
from . import sequencing
from . import limits as limitlib
from . import rule
from .broker import BUY, SELL, Broker, BrokerError, InstrumentInfo
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
class SectionView:
    """One section, as the screen needs it. Never mutated in place."""
    key: str
    name: str
    index: int
    enabled: bool
    near: Optional[InstrumentInfo]
    far: Optional[InstrumentInfo]
    near_quote: Optional[Quote]
    far_quote: Optional[Quote]
    decision: Optional[rule.RollDecision]
    report: Optional[gatelib.GateReport]
    sequence: Optional[object] = None
    clips_done: int = 0
    max_clips: int = 1
    allocated: Optional[int] = None
    done: int = 0
    may_sell: Optional[int] = None
    halted_reason: Optional[str] = None
    note: str = ""

    @property
    def outstanding(self) -> Optional[int]:
        if self.allocated is None:
            return None
        return max(0, self.allocated - self.done)


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

    # Every section, in configured order. The five fields above describe the
    # first of them and are kept so that everything written before sections
    # existed goes on working.
    sections: List[SectionView] = field(default_factory=list)
    allocation: str = ""
    allocation_refusal: Optional[str] = None
    # The section that would roll next if one fired now. Decided by book.pick
    # in the engine, not worked out again in the window, so the row marked as
    # next and the section actually chosen cannot drift apart.
    next_key: Optional[str] = None


class _Leg:
    """One side of a roll: which contract, which way, at what price."""

    __slots__ = ("role", "info", "side", "price", "token")

    def __init__(self, role, info, side, price, token):
        self.role = role          # "near" or "far"
        self.info = info
        self.side = side          # BUY or SELL
        self.price = price
        self.token = token


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
        # One recorder per section, built below. Sep into Oct and Sep into
        # Nov are different costs against different limits, so a single file
        # would produce a "closest approach" belonging to neither roll.
        self._recording = bool(cfg.record_market)
        self.journal = journallib.Journal(os.path.join(base_dir, "data"),
                                          enabled=cfg.journal)
        # Whether the cost was clearing the limit on the previous tick,
        # so the crossing can be announced once rather than every tick.
        self._was_qualifying = False

        # What every section shares, and one Section per configured roll. The
        # gates read a Section exactly as they read the old session object, so
        # not a line of gate code changed.
        self.account = sectionlib.AccountState()
        self.sections = [sectionlib.Section(spec, self.account, cfg)
                         for spec in cfg.section_specs()]

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._armed_until: Optional[float] = None
        self._lock = threading.Lock()
        self._state = STARTING
        self._snapshot: Optional[Snapshot] = None
        self._note = "not started"
        self._last_slow_refresh = 0.0
        self._last_complaint = ("", 0.0)
        self.near_positions: dict = {}
        self.pools: dict = {}
        # Which section would roll next, for the window. Never a decision.
        self._next_key: Optional[str] = None

        # The day's budget and any halt have to outlive the process: the
        # updater restarts it, and so does a crash. Loaded last, so it can
        # overwrite the defaults set above.
        self.store = StateStore(base_dir)
        saved = self.store.load()
        saved = self.store.for_campaign(saved, self.campaign_key())
        self.saved = saved

        self._equip_sections(saved)

        # A version 1 file had no sections, so its figures were migrated under
        # the campaign key. Where that key is not one of ours -- the operator
        # changed contracts between runs -- the flat figures are still the only
        # record of today, and the first section adopts them rather than
        # starting the day again from nothing.
        if self.sections and not saved.sections and saved.clips_done:
            self.sections[0].clips_done_today = saved.clips_done
            self.sections[0].lots_rolled = saved.lots_rolled
        if saved.halted_reason and not any(s.own_halt for s in self.sections):
            self.account.halted_reason = saved.halted_reason
            self.account.halted_at = saved.halted_at

        if self.halted_reason:
            self._state = HALTED
            self._note = self.halted_reason
            log.alert(f"Resumed with a halt still in force: {self.halted_reason}")
        elif self.sections and self.sections[0].clips_done_today:
            log.info(f"Resumed: {self.sections[0].clips_done_today} "
                     "clip(s) already done today.")

    def _equip_sections(self, saved=None, load_keys=None) -> None:
        """Give every section its ladder, its counters and its own files.

        `load_keys` limits which sections take their counters off disk. On a
        reload the sections that were already running carry their own figures
        across in memory, and the file is a snapshot from earlier -- reading
        it back over them would throw away the progress made since.
        """
        cfg = self.cfg
        solo = len(self.sections) < 2 and not cfg.sections
        for section in self.sections:
            section.ladder = self._build_ladder(section)
            section.watch_limits = self._build_watch(section)
            if saved is not None and (load_keys is None
                                      or section.key in load_keys):
                section.load_from(saved.section(section.key))
            # One roll keeps the plain market-<date>.csv it has always had;
            # several get a file each, named after the section.
            section.recorder = (
                Recorder(os.path.join(self.base_dir, "data"),
                         cfg.record_interval_sec,
                         name="" if solo else (section.name or section.key))
                if self._recording else None)
            # One file, one lock, but every line from this roll says so. With
            # one roll there is nothing to distinguish, and the record reads
            # exactly as it always did.
            section.journal = (self.journal if solo else
                               self.journal.for_section(section.key,
                                                        section.name))

    def reload_sections(self) -> bool:
        """Rebuild the sections from the configuration, keeping their progress.

        Adding, removing, re-legging or enabling a section changes
        config.json. Without this the engine went on running the list it was
        constructed with, and the window went on showing it -- so every button
        acted on a section that was no longer there.

        That is not a cosmetic fault. An operator who added a section, saw the
        old row still alone on screen and pressed Remove deleted the roll they
        already had, because the row on screen was the stale one. That is
        exactly what happened.

        Contracts already resolved from the scrip master are carried across by
        key, so a reload does not blank the screen waiting for a refresh, and
        so does progress: a roll that has done 6,000 has still done 6,000.
        """
        if self.account.in_flight:
            # A clip in flight holds one of the CURRENT section objects and
            # credits its fill to it. Rebuild underneath it and that credit
            # lands on an orphan: rolled at the exchange, absent from the
            # record, and rolled again later. The window refuses this before
            # it writes anything; this is the backstop for any other caller.
            self.log.warn("Not reloading the sections: an order is working. "
                          "The change will take effect once it finishes.")
            return False

        before = {section.key: section for section in self.sections}
        # Whatever was about to roll belonged to the old list. Left alone it
        # could mark a row that is no longer there, or the wrong one.
        self._next_key = None
        with self._lock:
            self.sections = [sectionlib.Section(spec, self.account, self.cfg)
                             for spec in self.cfg.section_specs()]
            for section in self.sections:
                old = before.get(section.key)
                if old is None:
                    continue
                # Everything the scrip master and the day have established.
                section.near = old.near
                section.far = old.far
                section.clips_done_today = old.clips_done_today
                section.lots_rolled = old.lots_rolled
                section.ladder_progress = dict(old.ladder_progress)
                section.own_halt = old.own_halt
                section.decision = old.decision
                section.report = old.report
                section.quotes = old.quotes
            # Only the sections that were not already running read their
            # counters off disk; the rest kept theirs above.
            self._equip_sections(
                self.saved,
                load_keys={s.key for s in self.sections} - set(before))

        kept = sorted(set(before) & {s.key for s in self.sections})
        gone = sorted(set(before) - {s.key for s in self.sections})
        fresh = sorted({s.key for s in self.sections} - set(before))
        self.log.info(
            f"Sections reloaded: {len(self.sections)} configured"
            + (f", new: {', '.join(fresh)}" if fresh else "")
            + (f", removed: {', '.join(gone)}" if gone else "")
            + (f", kept: {', '.join(kept)}" if kept else ""))
        self._share_out()
        self._persist()
        self._republish()
        return True

    def _republish(self) -> None:
        """Re-issue the snapshot from what the sections already hold.

        Without this the window would draw the NEW sections against the OLD
        snapshot until the next tick, and anything that reads the snapshot to
        decide which section is on screen -- the limit cards above all -- would
        edit the wrong one. That is not cosmetic: the cards are what the Set
        limits button reads, so a stale card wrote one section's limits into
        another's ladder.
        """
        first = self.sections[0] if self.sections else None
        quotes = getattr(first, "quotes", None) if first else None
        self._publish(quotes[0] if quotes else None,
                      quotes[1] if quotes else None,
                      first.decision if first else None,
                      self._note,
                      first.report if first else None)

    # ------------------------------------------------- the single-roll view
    # Everything that predates sections reads `engine.session`, `engine.ladder`
    # and `engine.ladder_progress`. While there is one section those mean the
    # first section, so the whole existing surface keeps working unchanged and
    # the refactor can be proved before any new behaviour exists.
    @property
    def section(self) -> "sectionlib.Section":
        return self.sections[0]

    @property
    def halted_reason(self) -> Optional[str]:
        """The account halt, or the first section's, whichever is in force."""
        if self.account.halted_reason:
            return self.account.halted_reason
        for section in self.sections:
            if section.own_halt:
                return section.own_halt
        return None

    @property
    def session(self) -> "sectionlib.Section":
        return self.sections[0]

    @property
    def ladder(self):
        return self.sections[0].ladder

    @ladder.setter
    def ladder(self, value) -> None:
        self.sections[0].ladder = value

    @property
    def ladder_progress(self) -> dict:
        return self.sections[0].ladder_progress

    @ladder_progress.setter
    def ladder_progress(self, value: dict) -> None:
        self.sections[0].ladder_progress = dict(value or {})

    @property
    def watch_limits(self) -> list:
        return self.sections[0].watch_limits

    @watch_limits.setter
    def watch_limits(self, value: list) -> None:
        self.sections[0].watch_limits = list(value or [])

    # --------------------------------------------------------------- controls
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="roll-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for section in self.sections:
            recorder = getattr(section, "recorder", None)
            if recorder is None:
                continue
            # Each section is summarised against its OWN limit. Measuring one
            # roll's cost against another's limit is how you get told a roll
            # was never in the money when it was.
            limit_bps = None
            if section.decision is not None:
                limit_bps = section.decision.limit_bps
            for line in recorder.summary(limit_bps).splitlines():
                self.log.info(line)
        self.feed.stop()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def recorder(self):
        """The first section's, the way .session and .ladder are."""
        return getattr(self.sections[0], "recorder", None) if self.sections else None

    def restart(self) -> None:
        """Re-read the configured contracts and start watching again.

        Used when the operator changes the legs. The day's clip count is kept,
        so switching contracts cannot be used to get around the daily budget.
        """
        if self.halted_reason:
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
        """Allow the next qualifying quote to trade. Expires on its own.

        With more than one section this is also where the allocation is
        settled. The sections sell the same near month, so what each may roll
        has to fit inside what is actually held -- and that is established
        before anything is armed rather than discovered when the second order
        is rejected.
        """
        refusal = self.allocation_refusal()
        if refusal:
            self.log.alert("Cannot arm: " + refusal)
            return

        with self._lock:
            if self.halted_reason:
                self.log.warn("Cannot arm while halted.")
                return
            self._armed_until = time.monotonic() + self.cfg.arm_timeout_sec

        working = [s for s in self.sections if s.enabled]
        self.log.info(
            f"ARMED for {self.cfg.arm_timeout_sec}s across {len(working)} "
            f"section(s) "
            f"({'dry run, nothing will be sent' if self.cfg.dry_run else 'LIVE ORDERS'})"
        )
        for token, pool in (self.pools or {}).items():
            if len(pool.claims) > 1:
                self.log.info(f"  near {token}: {pool.describe()}")

    def disarm(self, reason: str = "operator") -> None:
        with self._lock:
            was_armed = self._armed_until is not None
            self._armed_until = None
        if was_armed:
            self.log.info(f"Disarmed ({reason}).")

    def halt(self, reason: str) -> None:
        """Stop everything. A half rolled position is an account-level fact.

        It is not a property of one campaign: every section sells the same near
        month into the same uncertainty, so none of them may carry on while the
        exposure is unknown.
        """
        with self._lock:
            self.account.halted_reason = reason
            self.account.halted_at = (
                datetime.now().astimezone().isoformat(timespec="seconds"))
            self._armed_until = None
            self._state = HALTED
            self._note = reason
        self.log.alert(f"HALTED: {reason}")
        self.journal.halt(reason)
        self._persist()

    def clear_halt(self) -> None:
        with self._lock:
            self.account.halted_reason = None
            self.account.halted_at = None
            for section in self.sections:
                section.own_halt = None
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

        known = {}
        for section in self.sections:
            cfg = section.cfg
            section.near = self.broker.instrument(cfg.near_token)
            section.far = self.broker.instrument(cfg.far_token)
            known[section.near.token] = section.near
            known[section.far.token] = section.far

        # The scrip master states the price scale and the tick for each
        # contract, so the quote reader checks a declared scale instead of
        # guessing one. Sections that share a leg share one entry.
        self.reader.set_instruments(known)

        self.account.scrip_file_date = self.broker.scrip_file_date

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
                known = {}
                for section in self.sections:
                    cfg = section.cfg
                    section.near = self.broker.instrument(cfg.near_token)
                    section.far = self.broker.instrument(cfg.far_token)
                    known[section.near.token] = section.near
                    known[section.far.token] = section.far
                self.reader.set_instruments(known)
                self.account.scrip_file_date = self.broker.scrip_file_date
                self.log.info("Contracts re-read from the new scrip master.")
        except Exception as exc:
            self.log.error(f"Could not refresh the scrip master: {exc}")

        self.account.market_open = self.broker.market_open()
        self._read_positions()

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

    def _count_clip(self, qty: int, section=None) -> None:
        """Record a completed clip, on disk as well as in memory."""
        section = section if section is not None else self.sections[0]
        section.clips_done_today += 1
        section.lots_rolled += int(qty or 0)
        self._persist()

    def _persist(self) -> None:
        """Write the day's state out. Never raises."""
        from .state import DayState

        try:
            state = DayState(
                trading_date=date.today().isoformat(),
                halted_reason=self.account.halted_reason,
                halted_at=self.account.halted_at,
                ladder_campaign=self.campaign_key(),
                ladder_done=dict(getattr(self, "ladder_progress", {}) or {}),
            )
            for section in self.sections:
                section.save_into(state.section(section.key))
            if not self.store.save(state):
                self.log.warn("Could not write the state file; a restart would "
                              "forget today's clips and any halt.")
        except Exception as exc:
            self.log.warn(f"Could not write the state file: {exc}")

    def _announce_crossing(self, decision, section=None) -> None:
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
        """The first section's pair, for the version 1 state file only."""
        return ladderlib.campaign_key(self.cfg.near_token, self.cfg.far_token)

    def _build_ladder(self, section=None) -> "ladderlib.Ladder":
        """Parse the configured ladder, capped at the limit for this tenor.

        A rung looser than the tenor limit would quietly spend more than the
        client's instruction allows, so it is refused. Refusing means running
        without a ladder, on the single limit, which is the safe direction.
        """
        cfg = section.cfg if section is not None else self.cfg
        where = f"{section.label()}: " if section is not None else ""
        raw = getattr(cfg, "limit_ladder", None)
        if not raw:
            return ladderlib.Ladder([])
        try:
            ceiling = self._tenor_ceiling_bps(cfg, section)
            built = ladderlib.parse(raw, lot_size=cfg.expected_lot_size,
                                    ceiling_bps=ceiling)
        except Exception as exc:
            self.log.error(
                f"{where}the ladder in config.json cannot be used ({exc}). "
                "Running on the single tenor limit instead.")
            return ladderlib.Ladder([])

        if built:
            self.log.info(
                f"{where}ladder: "
                f"{', '.join(r.describe(cfg.expected_lot_size) for r in built.rungs)}"
                f"  (total {built.total_qty:,})")
        return built

    def _build_watch(self, section=None):
        """Limits priced for comparison only. Never traded, never a ceiling."""
        cfg = section.cfg if section is not None else self.cfg
        try:
            return ladderlib.parse_watch(getattr(cfg, "watch_limits", None))
        except Exception as exc:
            self.log.warn(f"watch_limits in config.json is unusable ({exc}); "
                          "ignoring it.")
            return []

    def _tenor_ceiling_bps(self, cfg=None, section=None):
        """The bps limit for this pair of contracts, if it can be determined.

        `cfg` lets a caller ask what the ceiling *would* be under a proposed
        configuration, which is how a change to the limit can be checked
        against the ladder before it is accepted.
        """
        cfg = cfg or self.cfg
        if cfg.limit_mode != "bps":
            return None
        try:
            from .limits import match_tenor, parse_schedule
            days = self.tenor_days(section)
            if days is None:
                return None
            _, bps = match_tenor(days, parse_schedule(cfg.limit_bps_schedule),
                                 cfg.tenor_tolerance_days)
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

    def _credit_rung(self, decision, qty: int, section=None) -> None:
        """Record a fill against the rung that was being worked."""
        section = section if section is not None else self.sections[0]
        rung = getattr(decision, "active_rung", None)
        if rung is None or not section.ladder or qty <= 0:
            return
        section.ladder_progress = section.ladder.credit(
            section.ladder_progress, rung.rung, qty)
        # Write it now. _count_clip persists just before this runs, so without
        # a save here a credit only reached disk on the *next* clip -- and a
        # restart forgot the last one completed, which means rolling that
        # quantity a second time.
        self._persist()
        done = section.ladder.done_total(section.ladder_progress)
        self.log.info(
            f"{section.label()}: {qty:,} credited to the "
            f"{money(rung.rung.bps, 0)} bps rung; {done:,} of "
            f"{section.ladder.total_qty:,} rolled, "
            f"{section.ladder.remaining_total(section.ladder_progress):,} left.")

    def cancel_all(self) -> Tuple[int, int, Optional[str]]:
        """Cancel every order still live on either leg.

        Returns (cancelled, failed, problem). `problem` is set when the book
        could not be read at all, which is the case a caller must not read as
        "nothing was working".
        """
        if self.cfg.dry_run:
            return 0, 0, None

        # Every contract any section trades, because cancel-all means all.
        tokens = sorted({t for section in self.sections
                         for t in section.tokens()})
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

    def rebuild_ladder(self, section=None) -> None:
        """Re-read a section's ladder after the operator has edited it.

        Progress is kept where a rung still exists at the same limit, and
        dropped where it does not: a rung that has been deleted or repriced is
        a different commitment, and carrying its history onto a new number
        would report a fresh rung as already part done.

        `section` matters. This used to take no argument, read the TOP-LEVEL
        limit_ladder and write the result to sections[0] -- so editing the
        limits of any section saved them to config.json and then rebuilt a
        different section from a different source. The limits were written and
        then vanished off the screen, which is exactly what an operator
        reported.
        """
        section = section if section is not None else self.sections[0]
        old = dict(section.ladder_progress)
        section.ladder = self._build_ladder(section)
        section.watch_limits = self._build_watch(section)
        keys = {r.key for r in section.ladder.rungs}
        section.ladder_progress = {k: v for k, v in old.items() if k in keys}

        dropped = sorted(set(old) - keys)
        if dropped:
            self.log.warn(
                f"{section.label()}: ladder progress dropped for rung(s) no "
                "longer in the ladder: "
                + ", ".join(f"{k} bps ({old[k]:,})" for k in dropped))
        self._persist()

    def reset_ladder(self) -> None:
        """Start the campaign again. The operator's decision, never the app's."""
        self.ladder_progress = {}
        self._persist()
        self.log.warn("Ladder progress reset. The whole campaign is outstanding again.")

    def tenor_days(self, section=None) -> Optional[int]:
        """How far apart the two contracts expire.

        The limit depends on this, so changing contracts changes the limit.
        That is the point: a one month roll and a two month roll are not the
        same trade and must not share a number.
        """
        section = section if section is not None else self.sections[0]
        near, far = section.near, section.far
        if near is not None and far is not None:
            return limitlib.tenor_days(near.expiry, far.expiry)

        # Before the contracts have been read, fall back to the expiries in
        # config.json. Those are cross-checked against the scrip master by a
        # gate, so they are not authoritative -- but they are good enough to
        # know whether a ladder rung is inside the limit for this tenor, and
        # the alternative is no ceiling at all.
        cfg = section.cfg
        return limitlib.tenor_days(_expiry(cfg.near_expiry),
                                   _expiry(cfg.far_expiry))

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

    def _read_positions(self) -> None:
        """The near-leg position, once per distinct contract.

        Sections that sell the same month share one reading. Asking twice
        would cost two calls and, worse, could return two different numbers a
        moment apart -- and the allocation arithmetic is only sound if every
        section is dividing up the same figure.
        """
        self.near_positions = {}
        if not self.cfg.require_position:
            return
        for token in {s.cfg.near_token for s in self.sections if s.cfg.near_token}:
            try:
                self.near_positions[token] = self.broker.long_qty(token)
            except Exception as exc:
                self.log.warn(f"Could not read the position in {token}: {exc}")
                self.near_positions[token] = None

    def _share_out(self) -> None:
        """Give each section what it may sell, after its siblings' claims.

        This is what makes the existing position gate do the right thing. It
        already refuses a clip larger than the position it is handed; hand it
        what is left once the other sections' allocations are set aside, and it
        refuses to sell into those too -- without the gate knowing sections
        exist.
        """
        # Only the sections that can actually trade take a share. A disabled
        # one sells nothing, so claiming for it took the position away from
        # the section that was still working -- and an uncapped disabled
        # section took all of it and blocked arming outright.
        #
        # self.sections is read ONCE into a local. It used to be read twice,
        # with the second pass matching sections to claims by list position:
        # reload the sections between the two reads and that raises, killing
        # the tick and leaving some sections without a share at all. Rare,
        # and reproducible in seconds under a soak.
        sections = list(self.sections)
        claims = [s.claim() for s in sections if s.enabled]
        mine = {id(s): c for s, c in zip([s for s in sections if s.enabled],
                                         claims)}
        self.pools = book.pools(claims, self.near_positions)
        for section in sections:
            claim = mine.get(id(section))
            if claim is None or not self.cfg.require_position:
                section.near_position_qty = None
                continue
            section.near_position_qty = book.sellable(
                claims, claim, self.near_positions.get(claim.near_token))

    def allocation_refusal(self) -> Optional[str]:
        """Why the sections cannot be armed together, or None."""
        for token, pool in (self.pools or {}).items():
            why = pool.refusal()
            if why:
                return f"near contract {token}: {why}"
        return None

    def _expiry_day(self, section=None) -> bool:
        """True when the near contract of any section expires today.

        Asked of the account rather than one section, because the sections in
        contention are the ones selling the same near month, and it is that
        month running out that changes what matters.
        """
        today = date.today()
        wanted = self.sections if section is None else [section]
        for candidate in wanted:
            near = candidate.near
            if near is not None and near.expiry == today:
                return True
        return False

    def _tick(self, tokens: List[str]) -> None:
        now = time.monotonic()
        if now - self._last_slow_refresh > 15:
            self._slow_refresh()
            self._last_slow_refresh = now

        quotes = self._read_quotes(tokens)
        self._last_complaint = ("", 0.0)
        self._share_out()
        # Published to the account BEFORE the sections are gated, so the gate
        # below sees this tick's figure rather than the previous one.
        self.account.clips_left = self._account_clips_left()

        expiry_day = self._expiry_day()
        candidates = []
        for section in self.sections:
            if section.near is None or section.far is None:
                section.decision, section.report = None, None
                section.note = "contracts not resolved"
                continue

            near_q = quotes.get(section.near.token)
            far_q = quotes.get(section.far.token)
            if near_q is None or far_q is None:
                section.decision, section.report = None, None
                section.note = "no quote"
                continue

            cfg = section.cfg
            # Priced whether or not it is switched on. A section is added to
            # find out what a roll would cost, and a switched-off one that
            # showed nothing could not answer that -- so the operator had to
            # enable it, which is the one thing that lets it sell, to see a
            # number. Switched off now means it is never a candidate, which is
            # where the safety actually is, not that it is never priced.
            decision = rule.compute(near_q, far_q, cfg,
                                    days=self.tenor_days(section),
                                    ladder=section.ladder,
                                    progress=section.ladder_progress,
                                    watch=section.watch_limits)
            section.decision = decision
            section.quotes = (near_q, far_q)

            if not section.enabled:
                # No gates: they are about whether this may trade, and it may
                # not. Running them would fill the row with reasons that are
                # beside the point.
                section.report, section.sequence = None, None
                section.note = "disabled"
                continue

            # The leg order is decided BEFORE the gates, and the same object
            # is handed to both, so the gate and the order that follows cannot
            # disagree. Without that the far touch-size gate would refuse
            # exactly the market that selects far-first, and no far-first order
            # could ever be sent.
            sequence = self._choose_sequence(decision, near_q, far_q, section)
            report = gatelib.evaluate(cfg, section, quotes, decision,
                                      sequence=sequence)
            section.report = report
            section.sequence = sequence

            candidates.append(book.Candidate(
                name=section.label(),
                cost_bps=decision.cost_bps,
                limit_bps=decision.limit_bps,
                qualifies=bool(decision.qualifies and report.ok),
                payload=section,
                outstanding=section.claim().outstanding))

        self._set_account_state()
        # Who would go first if a roll fired right now. Worked out whether or
        # not anything is armed, so the window can say which section is next
        # instead of leaving the operator to work it out from the numbers --
        # and worked out HERE rather than in the window, so the row marked as
        # next and the section actually chosen cannot disagree.
        chosen = book.pick(candidates, expiry_day=expiry_day)
        self._next_key = getattr(getattr(chosen, "payload", None), "key", None)

        first = self.sections[0] if self.sections else None
        near_q = far_q = None
        if first is not None and getattr(first, "quotes", None):
            near_q, far_q = first.quotes
        self._publish(near_q, far_q,
                      first.decision if first else None, self._note,
                      first.report if first else None)

        for section in self.sections:
            pair = getattr(section, "quotes", None)
            if section.recorder is not None and pair:
                section.recorder.sample(pair[0], pair[1], section.decision,
                                        section.report, self.quote_source)
            if section.decision is not None:
                self._announce_crossing(section.decision, section)

        if not self.armed:
            return
        if chosen is None:
            return

        if len(candidates) > 1:
            self.log.info("Sections:")
            for line in book.explain(candidates, chosen,
                                     expiry_day=expiry_day).splitlines():
                self.log.info(line)

        section = chosen.payload
        self.disarm("condition met, firing")
        near_q, far_q = section.quotes
        self._execute(section.decision, near_q, far_q, section=section,
                      sequence=section.sequence)

    def _set_account_state(self) -> None:
        """One state for the window, from however many sections there are."""
        if self.halted_reason:
            self._set_state(HALTED, self.halted_reason)
            return
        if self.armed:
            self._set_state(ARMED, "armed, waiting for a qualifying quote")
            return

        working = [s for s in self.sections if s.enabled]
        if working and all(s.clips_done_today >= s.cfg.max_clips_per_day
                           for s in working):
            self._set_state(DONE, "the day's clips are done")
            return
        left = self._account_clips_left()
        if left is not None and left <= 0:
            self._set_state(DONE, "the account's clips for the day are done")
            return
        self._set_state(WATCHING, "watching both legs" if len(working) < 2
                        else f"watching {len(working)} sections")

    def _account_clips_left(self) -> Optional[int]:
        """How many clips the account may still do today, across all sections.

        Two sections each allowed one clip a day means the ACCOUNT does two
        where it used to do one. That follows from asking for two sections that
        both trade, but it does not leap off the page, so it can be capped.

        None means uncapped. It used to return 1 for that, a sentinel meaning
        "not exhausted" -- and since the only caller compared it against zero
        to label the screen, the cap labelled the screen and stopped nothing.
        A cap of 1 let two sections send a clip each.
        """
        cap = self.cfg.max_clips_per_day_account
        if cap is None:
            return None
        done = sum(s.clips_done_today for s in self.sections)
        return max(0, int(cap) - done)

    # -------------------------------------------------------------- execution
    def _choose_sequence(self, decision, near_q, far_q, section):
        """Which leg goes out first for this clip."""
        cfg = section.cfg
        return sequencing.choose(
            decision.qty or cfg.clip_qty,
            near_bid_qty=getattr(near_q, "bid_qty", None),
            far_ask_qty=getattr(far_q, "ask_qty", None),
            expiry_day=self._expiry_day(section),
            mode=cfg.leg_order,
            near_depth_multiple=cfg.far_first_near_depth_multiple)

    def _execute(self, decision: rule.RollDecision, near_q: Quote, far_q: Quote,
                 section=None, sequence=None) -> None:
        """Send both legs, in whichever order this clip calls for.

        Near first is right when both legs fill trivially: if the second fails
        the account is left flat, which can be corrected at leisure. It inverts
        when the far leg is thin, because then the second leg is the one likely
        to come up short and a half roll becomes the expected outcome rather
        than the exception. See rollover/sequencing.py for the choice.

        Whichever way round, the SECOND leg is sized from what the first
        actually filled, never from what was asked for. That is what keeps the
        two sides equal when a leg fills partially.
        """
        section = section if section is not None else self.sections[0]
        cfg = section.cfg
        # Every line this clip writes says which roll it belongs to.
        journal = section.journal
        if sequence is None:
            sequence = self._choose_sequence(decision, near_q, far_q, section)

        self.account.in_flight = True
        self.log.info(f"--- ROLL: {section.label()} ---")
        self.log.info(decision.describe().replace("\n", " | "))
        self.log.info(f"Order of legs: {sequence.describe()}")
        journal.decision(decision, dry_run=cfg.dry_run, armed=True)
        journal.note("sequence", sequence.describe(),
                          section=section.label(), order=sequence.order,
                          near_bid_qty=getattr(near_q, "bid_qty", None),
                          far_ask_qty=getattr(far_q, "ask_qty", None),
                          clip=decision.qty)

        # Read before anything is sent. Both are the baseline for afterwards:
        # the position book says what the account held, the trade book says
        # what had already executed today, and without the "before" neither
        # can tell our fills apart from everyone else's.
        before_positions = None
        before_trades = None
        if not cfg.dry_run:
            before_positions = reconcile.capture(
                self.broker, cfg.near_token, cfg.far_token)
            before_trades = self.broker._trade_snapshot()
            self.log.info(f"Before the roll: {before_positions.describe()}")

        # The gate's copy is up to a slow beat old and was sized on the
        # configured clip. This one is taken now, for the quantity actually
        # about to be sent, and is what stops the order.
        if cfg.require_margin and not cfg.dry_run:
            estimate = marginlib.estimate(
                self.broker, cfg.near_token, cfg.far_token, decision.qty)
            self.account.margin = estimate
            journal.margin(estimate, decision.qty)
            if estimate.affordable is not True:
                self.account.in_flight = False
                self.disarm("margin")
                self.log.alert(
                    "NOT SENDING: " + (estimate.describe() if estimate.known
                                       else estimate.detail)
                    + ". Nothing was sent, and the app has disarmed.")
                self._set_state(WATCHING, "margin")
                return

        if sequence.far_first:
            first = _Leg("far", section.far, BUY, decision.buy_limit,
                         cfg.far_token)
            second = _Leg("near", section.near, SELL, decision.sell_limit,
                          cfg.near_token)
        else:
            first = _Leg("near", section.near, SELL, decision.sell_limit,
                         cfg.near_token)
            second = _Leg("far", section.far, BUY, decision.buy_limit,
                          cfg.far_token)

        try:
            self._set_state(WORKING, f"{section.label()}: sending the {first.role} leg")
            leg1 = self.broker.place_leg(first.info, first.side, decision.qty,
                                         first.price, f"leg 1 {first.role} "
                                         f"{'BUY' if first.side == BUY else 'SELL'}")
            self.log.info(f"leg 1 result: {leg1.detail}")
            journal.order(first.role, first.side, first.token, decision.qty,
                               first.price, leg1)

            if cfg.dry_run:
                self.log.info(
                    f"DRY RUN, not sent -- leg 2 {second.role} "
                    f"{'BUY' if second.side == BUY else 'SELL'} {decision.qty} "
                    f"of {second.info.token} at {money(second.price)}")
                self.log.info("DRY RUN complete. No orders reached the exchange.")
                self._count_clip(decision.qty, section)
                self._credit_rung(decision, decision.qty, section)
                return

            if not leg1.certain:
                self.halt(
                    f"{section.label()}: the {first.role} leg was sent but its "
                    f"fill could not be confirmed: {leg1.detail}. Check the "
                    "terminal before doing anything else.")
                return

            if leg1.filled_qty == 0:
                self.log.info(
                    f"The {first.role} leg did not fill and was cancelled. "
                    "No exposure changed. Back to watching.")
                return

            # The second leg is sized from what the FIRST actually filled. Ask
            # for what was wanted and a partial first leg leaves the two sides
            # unequal, which is the half roll this whole arrangement exists to
            # avoid.
            self._set_state(WORKING, f"{section.label()}: sending the {second.role} leg")
            leg2 = self.broker.place_leg(second.info, second.side,
                                         leg1.filled_qty, second.price,
                                         f"leg 2 {second.role} "
                                         f"{'BUY' if second.side == BUY else 'SELL'}")
            self.log.info(f"leg 2 result: {leg2.detail}")
            journal.order(second.role, second.side, second.token,
                               leg1.filled_qty, second.price, leg2)

            if leg2.fully_filled:
                near_leg = leg1 if first.role == "near" else leg2
                far_leg = leg2 if first.role == "near" else leg1
                self._confirm_trades(near_leg, far_leg, before_trades)

                # The order book is the broker's summary of what it believes.
                # The position book is what the account actually holds. Saying
                # ROLL COMPLETE without comparing them takes one on trust.
                verdict = reconcile.check(
                    self.broker, cfg.near_token, cfg.far_token,
                    before_positions or reconcile.Positions(None, None),
                    leg2.filled_qty, wait=cfg.reconcile_wait_sec)
                journal.reconciliation(verdict)
                if verdict.contradicted:
                    # Count the clip before halting: whatever else is wrong,
                    # this much was sent, and the daily budget must reflect it.
                    self._count_clip(leg2.filled_qty, section)
                    self.halt(f"{section.label()}: BOTH LEGS REPORTED FILLED, "
                              "BUT " + verdict.detail)
                    return
                if verdict.checked:
                    self.log.info(verdict.detail)
                else:
                    self.log.warn(verdict.detail)

                self._count_clip(leg2.filled_qty, section)
                self._credit_rung(decision, leg2.filled_qty, section)
                self.log.info(
                    f"{section.label()}: ROLL COMPLETE: sold "
                    f"{near_leg.filled_qty} near, bought {far_leg.filled_qty} "
                    f"far, at a booked cost near {money(decision.roll_cost)} "
                    "per unit.")
                journal.note(
                    "complete", "roll complete", section=section.label(),
                    near_filled=near_leg.filled_qty,
                    far_filled=far_leg.filled_qty,
                    order=sequence.order,
                    roll_cost=decision.roll_cost, cost_bps=decision.cost_bps,
                    limit_bps=decision.limit_bps,
                    clips_done=section.clips_done_today,
                    ladder_done=dict(section.ladder_progress))
                self._slow_refresh()
                return

            short_by = leg1.filled_qty - leg2.filled_qty
            self._handle_second_leg_failure(leg1, leg2, short_by, near_q, far_q,
                                            section, first, second)

        except Exception as exc:
            # Anything unexpected in here can have left a leg filled. Without
            # this the exception escaped to the watch loop, which logged it and
            # carried on: no halt, every gate passing again, and the operator
            # able to arm on top of a position that is already half moved.
            self.halt(
                f"{section.label()}: {type(exc).__name__} during execution: "
                f"{exc}. A leg may have filled. Check the terminal and the "
                "position book before doing anything else.")
            self.log.error(f"Execution failed after the roll had started: {exc!r}")

        finally:
            self.account.in_flight = False

    def _handle_second_leg_failure(self, leg1, leg2, short_by, near_q, far_q,
                                   section, first, second) -> None:
        """One leg filled and the other did not. What that leaves depends.

        Near first leaves the account SHORT the near month: sold and not
        replaced. Far first leaves it LONG BOTH months: bought and not paid for
        by a sale. They are not the same problem and they are not undone the
        same way.

        The far-first case is the more dangerous one to correct automatically.
        Undoing it means selling the far leg back into the same thin book that
        made far-first the right choice in the first place, and sweeping a thin
        book in a hurry is how a small legging cost becomes a large one. So it
        is never done without being asked for, and the default is to stop and
        say so.
        """
        far_first = first.role == "far"
        cfg = section.cfg

        if far_first:
            message = (
                f"{section.label()}: HALF ROLLED THE OTHER WAY. The far leg "
                f"bought {leg1.filled_qty} units but the near leg only sold "
                f"{leg2.filled_qty}, so the account is long both months by "
                f"{short_by} units. Near leg detail: {leg2.detail}")
        else:
            message = (
                f"{section.label()}: HALF ROLLED. The near leg sold "
                f"{leg1.filled_qty} units but the far leg only bought "
                f"{leg2.filled_qty}. You are short {short_by} units of the "
                f"intended position. Far leg detail: {leg2.detail}")

        if section.ladder:
            # Crediting a rung from a half-rolled clip would be a guess in one
            # direction or the other, and the position has to be reconciled by
            # hand regardless. Say so rather than quietly picking a number.
            message += (
                f" The ladder has NOT been credited with the {leg2.filled_qty} "
                "that did roll; check the position book and use Reset ladder "
                "if the progress shown no longer matches it.")
        self.log.alert(message)

        if not cfg.auto_unwind_on_leg2_failure:
            self.halt(message + " Auto unwind is off, so this needs you at the "
                                "terminal now.")
            return

        if far_first:
            # Selling the far leg back into the book that was too thin to fill
            # it is the one action most likely to turn a small problem into a
            # large one.
            self.halt(
                message + " Undoing this means selling the far leg back into "
                "the same thin book that made far-first the right choice, so "
                "it is not done automatically whatever auto unwind says. This "
                "needs you at the terminal now.")
            return

        # Buy the near contract back so the account returns to where it started.
        unwind_price = ceil_tick(near_q.ask + cfg.tick_d * 4, cfg.tick_d)
        self.log.alert(f"Auto unwind: buying back {short_by} of the near contract "
                       f"at {money(unwind_price)}")
        undo = self.broker.place_leg(
            section.near, BUY, short_by, unwind_price, "unwind near BUY")
        if undo.fully_filled:
            self.halt(message + " The near leg was bought back, so the position is "
                                "roughly where it started. No roll happened.")
        else:
            self.halt(message + f" The unwind also failed ({undo.detail}). "
                                "Go to the terminal immediately.")

    # ------------------------------------------------------------- publishing
    def _view(self, section) -> SectionView:
        """One section, flattened for the screen."""
        near_q = far_q = None
        if getattr(section, "quotes", None):
            near_q, far_q = section.quotes
        return SectionView(
            key=section.key, name=section.label(), index=section.index,
            enabled=section.enabled,
            near=section.near, far=section.far,
            near_quote=near_q, far_quote=far_q,
            decision=section.decision, report=section.report,
            sequence=section.sequence,
            clips_done=section.clips_done_today,
            max_clips=section.cfg.max_clips_per_day,
            allocated=section.allocated(), done=section.done(),
            may_sell=section.near_position_qty,
            halted_reason=section.halted_reason,
            note=section.note)

    def _allocation_line(self) -> str:
        """One line per contended contract, or nothing when none is."""
        parts = []
        for token, pool in (self.pools or {}).items():
            if len(pool.claims) > 1:
                parts.append(f"{token}: {pool.describe()}")
        return "   ".join(parts)

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
            halted_reason=self.halted_reason,
            quote_source=self.quote_source,
            sections=[self._view(s) for s in self.sections],
            next_key=self._next_key,
            allocation=self._allocation_line(),
            allocation_refusal=self.allocation_refusal(),
        )
        with self._lock:
            self._snapshot = snap
