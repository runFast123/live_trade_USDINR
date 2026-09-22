"""Check the live order path without sending anything.

Phase 3 starts by putting a real order in front of the exchange. Before that,
everything the order depends on should be confirmed against the broker as it
stands right now, not against a fixture and not against what was true last
week. That is all this does: it reads, it prints what the app WOULD send, and
it sends nothing.

What it checks, in the order a clip would need them:

  session      the broker still accepts it for data
  market       open, and inside the app's own trading window
  contracts    today's scrip master, lot, tick, price scale, circuit band
  position     what the account holds in the near leg, in units
  books        the order book and trade book can be read, and what is working
  margin       what one clip needs against what the account has
  the order    both legs, priced off the live book, each field checked
               against the exchange's own rules
  gates        every gate, evaluated now
  settings     the ones that decide how much a mistake costs

Nothing here places, modifies or cancels an order. The only thing it can
change is the session file, and only by refreshing a session already saved.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal
from typing import List

from . import gates as gatelib
from . import limits as limitlib
from . import margin as marginlib
from . import rule
from .broker import BUY, SELL, Broker, BrokerError
from .money import money

PASS, FAIL, WARN, INFO = "PASS", "FAIL", "WARN", "INFO"


class Report:
    """Lines printed as they are found, and a verdict at the end."""

    def __init__(self):
        self.rows: List[tuple] = []

    def add(self, mark: str, name: str, detail: str) -> None:
        self.rows.append((mark, name, detail))
        print(f"  [{mark}] {name}: {detail}", flush=True)

    def section(self, title: str) -> None:
        print(f"\n{title}\n{'-' * len(title)}", flush=True)

    @property
    def failures(self) -> List[tuple]:
        return [r for r in self.rows if r[0] == FAIL]

    @property
    def warnings(self) -> List[tuple]:
        return [r for r in self.rows if r[0] == WARN]


def _leg_order_check(report: Report, broker: Broker, info, side: int,
                     qty: int, price: Decimal, role: str) -> None:
    """Everything place_leg would do to this leg, short of sending it.

    The three rules the exchange enforces and the app must not break: the
    quantity is a whole number of lots, the price sits exactly on the tick
    grid, and the price is inside today's circuit band. Each is checked here
    against the broker's own numbers rather than against a constant.
    """
    word = "SELL" if side == SELL else "BUY"
    lot = info.lot_size or 0
    label = f"{role} leg ({word} {info.label()})"

    if lot and qty % lot == 0:
        report.add(PASS, f"{label} quantity",
                   f"{qty:,} units = {qty // lot} lot(s) of {lot:,}")
    elif lot:
        report.add(FAIL, f"{label} quantity",
                   f"{qty:,} units is not a whole multiple of the lot {lot:,}; "
                   "the exchange refuses that")
    else:
        report.add(FAIL, f"{label} quantity",
                   "the scrip master gives no lot size for this contract")

    try:
        units = broker.exchange_price(info, price)
        report.add(PASS, f"{label} price",
                   f"{money(price)} sent as {units} "
                   f"(divisor {info.price_divisor}, tick {info.tick})")
    except BrokerError as exc:
        report.add(FAIL, f"{label} price", str(exc))
        return

    low, high = info.low_range, info.high_range
    if low is None or high is None:
        report.add(WARN, f"{label} circuit band",
                   "the scrip master gives no band, so the price cannot be "
                   "checked against it")
    elif low <= price <= high:
        report.add(PASS, f"{label} circuit band",
                   f"{money(price)} is inside {money(low)}..{money(high)}")
    else:
        report.add(FAIL, f"{label} circuit band",
                   f"{money(price)} is OUTSIDE {money(low)}..{money(high)}; "
                   "the exchange would reject it")


def run(cfg, log, base_dir: str) -> int:
    """Read-only preflight. Returns 0 when nothing is blocking Phase 3."""
    report = Report()
    print("Live order preflight. Nothing is sent, nothing is cancelled.\n")
    print(f"Account {cfg.vendor_id} at {cfg.base_url}, segment {cfg.segment_id}")

    # ---- session ---------------------------------------------------------
    report.section("Session")
    broker = Broker(cfg, log)
    session_path = os.path.join(base_dir, "session.json")
    try:
        broker.build_client()
        resumed = broker.resume(session_path)
    except Exception as exc:
        report.add(FAIL, "login", f"could not build a client: {exc}")
        return _verdict(report)

    if not resumed:
        report.add(FAIL, "saved session",
                   "there is no usable session saved today. Open the app and "
                   "log in, then run this again.")
        return _verdict(report)

    ok, why = broker.verify_session()
    if ok is True:
        report.add(PASS, "session", why)
    elif ok is False:
        report.add(FAIL, "session",
                   f"the broker refuses it: {why}. Nothing can trade.")
        return _verdict(report)
    else:
        report.add(WARN, "session", f"could not be checked: {why}")

    # ---- market ----------------------------------------------------------
    report.section("Market")
    open_now = broker.market_open()
    if open_now is True:
        report.add(PASS, "exchange", "the currency segment is open")
    elif open_now is False:
        report.add(FAIL, "exchange", "the currency segment is closed")
    else:
        report.add(WARN, "exchange", "the status could not be read")

    now = datetime.now()
    inside = cfg.window_open_t <= now.time() <= cfg.window_close_t
    report.add(PASS if inside else FAIL, "app window",
               f"{now:%H:%M:%S} against {cfg.window_open} to {cfg.window_close}")

    # ---- contracts -------------------------------------------------------
    report.section("Contracts")
    try:
        broker.load_scrip_master()
    except Exception as exc:
        report.add(FAIL, "scrip master", f"could not be loaded: {exc}")
        return _verdict(report)

    stamp = broker.scrip_file_date
    report.add(PASS if stamp == date.today() else WARN, "scrip master",
               f"file dated {stamp}")

    legs = {}
    for role, token in (("near", cfg.near_token), ("far", cfg.far_token)):
        try:
            info = broker.instrument(token)
        except Exception as exc:
            report.add(FAIL, f"{role} contract", f"token {token}: {exc}")
            continue
        legs[role] = info
        report.add(PASS, f"{role} contract",
                   f"{info.label()} ({token}) expiry {info.expiry} "
                   f"lot {info.lot_size} tick {info.tick} "
                   f"divisor {info.price_divisor}")
    if len(legs) != 2:
        return _verdict(report)

    # ---- position --------------------------------------------------------
    report.section("Position")
    held = broker.long_qty(cfg.near_token)
    lot = legs["near"].lot_size or 1
    if held is None:
        report.add(WARN, "near leg", "the position book could not be read")
    elif held <= 0:
        report.add(FAIL if cfg.require_position else WARN, "near leg",
                   "nothing is held, so there is nothing to roll")
    else:
        report.add(PASS, "near leg",
                   f"long {held:,} units = {held / lot:g} lot(s)")
    far_held = broker.net_qty(cfg.far_token)
    report.add(INFO, "far leg",
               "unknown" if far_held is None else f"net {far_held:,} units")

    # ---- books -----------------------------------------------------------
    report.section("Order and trade books")
    working = broker.working_orders([cfg.near_token, cfg.far_token])
    if working is None:
        report.add(FAIL, "order book",
                   "could not be read. The app identifies its own fills "
                   "through it, so a roll must not start blind.")
    elif working:
        report.add(WARN, "order book",
                   f"{len(working)} order(s) already working on these two "
                   "contracts. Clear them before a live test, or the app "
                   "cannot tell its own fill from theirs.")
    else:
        report.add(PASS, "order book", "readable, nothing working")

    trades = broker._trade_snapshot()
    report.add(FAIL if trades is None else PASS, "trade book",
               "could not be read" if trades is None
               else f"readable, {len(trades)} trade(s) so far today")

    # ---- quotes ----------------------------------------------------------
    report.section("Prices")
    from .quotes import QuoteError, QuoteReader

    reader = QuoteReader(broker.client, cfg)
    reader.set_instruments({cfg.near_token: legs["near"],
                            cfg.far_token: legs["far"]})
    try:
        quotes = reader.fetch([cfg.near_token, cfg.far_token])
    except QuoteError as exc:
        report.add(FAIL, "touchline", str(exc))
        return _verdict(report)

    near_q = quotes.get(cfg.near_token)
    far_q = quotes.get(cfg.far_token)
    if near_q is None or far_q is None:
        report.add(FAIL, "touchline",
                   f"only {sorted(quotes)} came back; "
                   f"{reader.problems}")
        return _verdict(report)
    for role, quote in (("near", near_q), ("far", far_q)):
        report.add(PASS, f"{role} book",
                   f"bid {money(quote.bid)} x {quote.bid_qty}, "
                   f"ask {money(quote.ask)} x {quote.ask_qty}")

    # ---- the order itself -------------------------------------------------
    report.section("The order that would be sent, at these prices")
    section = _first_section(cfg)
    leg_cfg = section.cfg if section else cfg
    # The tenor decides which basis-point limit applies. Without it the rule
    # refuses rather than borrowing another tenor's number, so it is worked
    # out here the same way the engine works it out.
    days = limitlib.tenor_days(legs["near"].expiry, legs["far"].expiry)
    decision = rule.compute(near_q, far_q, leg_cfg, days=days,
                            ladder=getattr(section, "ladder", None),
                            progress=getattr(section, "ladder_progress", None))
    print(f"  cost {decision.roll_cost} "
          f"({decision.cost_bps:.1f} bps) against a limit of "
          f"{decision.limit} ({decision.limit_bps} bps); "
          f"{'QUALIFIES' if decision.qualifies else 'does not qualify'}")
    if decision.qty <= 0:
        report.add(WARN, "clip size",
                   "the ladder sizes this clip at nothing, so no order can "
                   "be described. The checks below use one lot instead.")
        qty = lot
    else:
        qty = decision.qty

    _leg_order_check(report, broker, legs["near"], SELL, qty,
                     decision.sell_limit, "near")
    _leg_order_check(report, broker, legs["far"], BUY, qty,
                     decision.buy_limit, "far")
    report.add(INFO, "order fields",
               f"order_type={cfg.order_type} product_type={cfg.product_type} "
               f"validity={cfg.validity} (Day, cancelled after "
               f"{cfg.fill_timeout_sec:g}s to make it immediate-or-cancel)")

    # ---- margin ----------------------------------------------------------
    report.section("Margin")
    estimate = marginlib.estimate(broker, cfg.near_token, cfg.far_token, qty)
    if estimate.affordable is True:
        report.add(PASS, "margin", estimate.describe())
    elif estimate.affordable is False:
        report.add(FAIL, "margin", estimate.describe())
    else:
        report.add(WARN, "margin", estimate.detail)

    # ---- gates -----------------------------------------------------------
    report.section("Gates, as they stand now")
    if section is None:
        report.add(WARN, "gates", "no section is configured")
    else:
        section.logged_in = True
        section.market_open = open_now
        section.near, section.far = legs["near"], legs["far"]
        section.near_position_qty = held
        section.scrip_file_date = stamp
        section.margin = estimate
        blocked = []
        for gate in gatelib.evaluate(section.cfg, section, quotes,
                                     decision).gates:
            print(f"  {'ok  ' if gate.ok else 'BLOCK'} {gate.name}: "
                  f"{gate.detail}")
            if not gate.ok:
                blocked.append(gate.name)
        report.add(PASS if not blocked else INFO, "gates",
                   "all clear" if not blocked
                   else f"{len(blocked)} blocking: {', '.join(blocked)}")

    # ---- the settings that decide what a mistake costs -------------------
    report.section("Settings that decide the size of a mistake")
    report.add(INFO, "mode",
               "DRY RUN -- nothing would reach the exchange" if cfg.dry_run
               else "LIVE -- orders would be real")
    report.add(INFO, "clips today", f"at most {cfg.max_clips_per_day} per roll")
    report.add(INFO, "arming", f"expires after {cfg.arm_timeout_sec}s")
    report.add(INFO, "leg order", cfg.leg_order)
    report.add(INFO, "unwind on leg 2 failure",
               "on" if cfg.auto_unwind_on_leg2_failure else "off -- a failed "
               "second leg halts and waits for a person")
    for name in ("require_market_status", "require_fresh_scrip",
                 "require_position", "require_margin", "require_touch_size"):
        on = getattr(cfg, name)
        report.add(INFO if on else WARN, name,
                   "on" if on else "OFF -- this check is not being made")

    report.add(WARN, "square off",
               "this app cannot flatten a position. It can cancel working "
               "orders, and in one failure case buy back the near leg. "
               "Anything a live test leaves open must be closed by hand in "
               "the broker terminal.")

    return _verdict(report)


def _first_section(cfg):
    """A Section built the way the engine builds one, for the gates."""
    from . import sections as sectionlib
    from . import ladder as ladderlib

    specs = cfg.section_specs()
    if not specs:
        return None
    account = sectionlib.AccountState()
    section = sectionlib.Section(specs[0], account, cfg)
    try:
        section.ladder = ladderlib.parse(
            section.cfg.limit_ladder, lot_size=section.cfg.expected_lot_size)
    except Exception:
        section.ladder = ladderlib.Ladder([])
    return section


def _verdict(report: Report) -> int:
    print("\n" + "=" * 70)
    if report.failures:
        print(f"{len(report.failures)} thing(s) block a live order:")
        for _, name, detail in report.failures:
            print(f"  - {name}: {detail}")
        print("\nPhase 3 should not start until these are cleared.")
        return 1
    if report.warnings:
        print(f"Nothing blocks a live order. {len(report.warnings)} thing(s) "
              "worth reading first:")
        for _, name, detail in report.warnings:
            print(f"  - {name}: {detail}")
    else:
        print("Nothing blocks a live order.")
    print("\nThis checked the path. It did not send anything, and a clean "
          "preflight is not permission -- Phase 3.1 (--probe) is still the "
          "first thing that puts an order in front of the exchange.")
    return 0
