"""Safety gates.

Every gate must pass before a single order is sent. A gate that cannot be
evaluated counts as a failure, never as a pass, because the cost of not rolling
today is recoverable and the cost of a wrong roll is not.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional

from .money import money


@dataclass
class Gate:
    name: str
    ok: bool
    detail: str

    def __str__(self) -> str:
        return f"[{'PASS' if self.ok else 'FAIL'}] {self.name}: {self.detail}"


@dataclass
class GateReport:
    gates: List[Gate]

    @property
    def ok(self) -> bool:
        return all(g.ok for g in self.gates)

    @property
    def failures(self) -> List[Gate]:
        return [g for g in self.gates if not g.ok]

    def __str__(self) -> str:
        return "\n".join(str(g) for g in self.gates)


def evaluate(cfg, session, quotes, decision, now: Optional[datetime] = None) -> GateReport:
    """Run every gate. `session` carries what was learned at startup and during the day.

    session is expected to provide:
        logged_in            bool
        near, far            InstrumentInfo with token, sec_desc, expiry (date), lot_size
        market_open          bool or None when unknown
        near_position_qty    int, long units held in the near contract, or None when unknown
        clips_done_today     int
        in_flight            bool
        halted_reason        str or None
    """
    now = now or datetime.now()
    today = now.date()
    gates: List[Gate] = []

    def add(name: str, ok: bool, detail: str) -> None:
        gates.append(Gate(name, bool(ok), detail))

    # What is actually about to be sent, which is not always a whole clip. A
    # ladder rung with 300 units left sends 300, and checking the position and
    # the depth against the full clip blocked that order although there was
    # ample of both -- so the tail of every rung was untradeable.
    #
    # Zero means nothing qualifies and no order is coming, so the nominal clip
    # is shown instead: a size gate that passes because it is checking against
    # nothing would be worse than useless.
    clip = getattr(decision, "qty", 0) or cfg.clip_qty

    # ---- nothing is broken -------------------------------------------------
    add("not halted",
        session.halted_reason is None,
        "clear" if session.halted_reason is None
        else f"halted: {session.halted_reason}")

    add("no order in flight",
        not session.in_flight,
        "idle" if not session.in_flight else "an order is already working")

    # ---- connection --------------------------------------------------------
    add("session", session.logged_in,
        "logged in" if session.logged_in else "not logged in")

    if session.market_open is None:
        if cfg.require_market_status:
            add("market open", False,
                "market status could not be read from the API; set require_market_status "
                "to false in config.json to rely on the trading window instead")
        else:
            add("market open", True,
                "market status not checked, relying on the trading window")
    else:
        add("market open", session.market_open,
            "open" if session.market_open else "closed for this segment")

    # ---- instruments -------------------------------------------------------
    near_info, far_info = session.near, session.far
    if near_info is None or far_info is None:
        add("contracts resolved", False, "near or far contract not resolved yet")
        return GateReport(gates)

    add("contracts resolved", True,
        f"near {near_info.sec_desc} ({near_info.token}), "
        f"far {far_info.sec_desc} ({far_info.token})")

    add("distinct contracts",
        near_info.token != far_info.token,
        "two different contracts" if near_info.token != far_info.token
        else "both legs point at the same contract")

    if near_info.expiry and far_info.expiry:
        add("expiry order",
            far_info.expiry > near_info.expiry,
            f"near {near_info.expiry} then far {far_info.expiry}"
            if far_info.expiry > near_info.expiry
            else f"far {far_info.expiry} does not come after near {near_info.expiry}")
        add("near not expired",
            near_info.expiry >= today,
            f"{(near_info.expiry - today).days} day(s) to near expiry"
            if near_info.expiry >= today
            else f"near contract expired on {near_info.expiry}")
        if near_info.expiry == today:
            add("expiry day cutoff",
                now.time() < cfg.expiry_cutoff_t,
                f"before the {cfg.expiry_day_cutoff} cutoff on expiry day"
                if now.time() < cfg.expiry_cutoff_t
                else f"past the {cfg.expiry_day_cutoff} cutoff on expiry day")
    else:
        add("expiry known", False,
            "expiry date could not be read from the scrip master; "
            "set expiry_field in config.json")

    _expiry_matches(add, "near expiry matches config", cfg.near_expiry, near_info.expiry)
    _expiry_matches(add, "far expiry matches config", cfg.far_expiry, far_info.expiry)

    for role, info in (("near", near_info), ("far", far_info)):
        add(f"{role} is a future",
            info.is_futures,
            f"{info.instrument or 'unknown instrument type'}"
            if info.is_futures
            else f"{info.instrument or 'unknown'} is not a futures contract; "
                 "an option cannot be a leg of this roll")

        if info.tick is None:
            add(f"{role} tick", False,
                "the scrip master did not give a PriceTick for this contract")
        else:
            add(f"{role} tick", info.tick == cfg.tick_d,
                f"{info.tick} as configured" if info.tick == cfg.tick_d
                else f"the exchange tick is {info.tick} but config says {cfg.tick_d}")

        add(f"{role} price scale",
            info.price_divisor is not None,
            f"PriceDivisor {info.price_divisor} declared by the exchange"
            if info.price_divisor is not None
            else "the scrip master did not declare a PriceDivisor, so the quote "
                 "scale would have to be inferred")

    add("lot size",
        near_info.lot_size == cfg.expected_lot_size == far_info.lot_size,
        f"{near_info.lot_size} units per lot"
        if near_info.lot_size == cfg.expected_lot_size == far_info.lot_size
        else f"near {near_info.lot_size}, far {far_info.lot_size}, "
             f"config expects {cfg.expected_lot_size}")

    # ---- is the contract data current? -------------------------------------
    file_date = getattr(session, "scrip_file_date", None)
    if not cfg.require_fresh_scrip:
        add("scrip master fresh", True, "freshness check disabled in config")
    elif file_date is None:
        add("scrip master fresh", False,
            "cannot tell which day's scrip master is loaded")
    else:
        add("scrip master fresh", file_date == today,
            f"today's file, {file_date}" if file_date == today
            else f"loaded file is from {file_date}, not {today}; expiries and "
                 "circuit limits may be a day behind")

    # ---- trading window ----------------------------------------------------
    in_window = cfg.window_open_t <= now.time() <= cfg.window_close_t
    add("trading window", in_window,
        f"{now:%H:%M:%S} inside {cfg.window_open}-{cfg.window_close}" if in_window
        else f"{now:%H:%M:%S} outside {cfg.window_open}-{cfg.window_close}")

    # ---- daily budget ------------------------------------------------------
    add("clips remaining",
        session.clips_done_today < cfg.max_clips_per_day,
        f"{session.clips_done_today} of {cfg.max_clips_per_day} done today")

    # ---- position ----------------------------------------------------------
    if cfg.require_position:
        qty = session.near_position_qty
        if qty is None:
            add("position", False,
                "net position unknown; cannot confirm you hold the near contract")
        else:
            need = clip
            if qty >= need:
                # How many days of clips the whole position would take matters
                # when expiry is close: one clip a day cannot roll ten lots in
                # a week.
                clips = -(-qty // need)          # ceiling division
                days_left = ((near_info.expiry - today).days
                             if near_info.expiry else None)
                detail = f"long {qty:,} units, {clips} clip(s) to roll it all"
                if days_left is not None and clips > max(days_left, 0) * cfg.max_clips_per_day:
                    detail += (f" but only {days_left} day(s) and "
                               f"{cfg.max_clips_per_day} clip(s) a day left")
                add("position", True, detail)
            else:
                add("position", False,
                    f"long {qty:,} units, but the clip needs {need:,}")
    else:
        add("position", True, "position check disabled in config")

    # ---- quotes ------------------------------------------------------------
    near_q = quotes.get(near_info.token) if quotes else None
    far_q = quotes.get(far_info.token) if quotes else None
    if not near_q or not far_q:
        add("quotes", False, "no live quote for one or both legs")
        return GateReport(gates)

    add("quotes", True, f"both legs quoted at scale /{near_q.divisor}")

    # ---- is there enough resting at the touch to fill the clip? ------------
    # Selling the near leg in full and then finding only a handful of units on
    # the far offer is how a roll ends up half done, which is the one outcome
    # this app must not produce.
    if cfg.require_touch_size:
        need = clip
        for label, quote, side, size in (("near", near_q, "bid", near_q.bid_qty),
                                         ("far", far_q, "ask", far_q.ask_qty)):
            if size is None:
                add(f"{label} touch size", False,
                    "the feed did not give a size at the top of book")
            else:
                add(f"{label} touch size", size >= need,
                    f"{size} resting at the {side}, clip needs {need}")
    else:
        add("touch size", True, "top of book size check disabled in config")

    for label, quote in (("near", near_q), ("far", far_q)):
        age = quote.age()
        add(f"{label} quote fresh",
            age <= cfg.max_quote_age_sec,
            f"{age:.1f}s old (max {cfg.max_quote_age_sec:.1f}s)")
        add(f"{label} leg spread",
            quote.spread <= cfg.max_leg_spread_d,
            f"{money(quote.spread)} (max {money(cfg.max_leg_spread_d)})")

    # ---- the numbers themselves -------------------------------------------
    if decision is None:
        add("rule evaluated", False, "no decision computed")
        return GateReport(gates)

    # ---- which limit is in force, and why ----------------------------------
    detail = getattr(decision, "limit_detail", None)
    if detail is None:
        add("limit in force", False,
            "; ".join(decision.blockers) or "the limit could not be determined")
    else:
        add("limit in force", True, detail.describe())

        if cfg.limit_mode == "bps" and detail.tenor_days is not None:
            # The schedule is keyed by tenor, so a pair that matched a tenor
            # only loosely is worth saying out loud.
            nominal = float(detail.tenor_months) * 30.44
            drift = abs(detail.tenor_days - nominal)
            add("tenor matches the limit", drift <= cfg.tenor_tolerance_days,
                f"{detail.tenor_days} days is {drift:.0f} day(s) from a "
                f"{detail.tenor_months} month roll")

    in_band = (cfg.roll_cost_band_low_d <= decision.roll_cost <= cfg.roll_cost_band_high_d)
    add("roll cost plausible", in_band,
        f"{money(decision.roll_cost)} inside {money(cfg.roll_cost_band_low_d)}"
        f"..{money(cfg.roll_cost_band_high_d)}" if in_band
        else f"{money(decision.roll_cost)} outside the plausible band "
             f"{money(cfg.roll_cost_band_low_d)}..{money(cfg.roll_cost_band_high_d)}; "
             "treating this as bad data")

    if getattr(cfg, "require_margin", False) and not cfg.dry_run:
        estimate = getattr(session, "margin", None)
        if estimate is None:
            add("margin", False, "not yet checked")
        elif estimate.affordable is None:
            # Unknown is not affordable. Guessing optimistically here is how a
            # rejected far leg leaves a naked short in the expiring month.
            add("margin", False, estimate.detail)
        else:
            add("margin", estimate.affordable, estimate.describe())
    else:
        add("margin", True,
            "not checked in dry run" if cfg.dry_run else "check disabled")

    add("roll cost below limit",
        decision.qualifies,
        f"{money(decision.roll_cost)} < {money(decision.limit)}" if decision.qualifies
        else "; ".join(decision.blockers))

    return GateReport(gates)


def _expiry_matches(add, name: str, expected: str, actual: Optional[date]) -> None:
    """Compare the expiry read from the scrip master with the one in config.

    An unset config value is not a silent pass: it is reported so the operator
    can see that this cross-check is not running.
    """
    if not expected:
        add(name, True, "not configured, cross-check skipped")
        return
    if actual is None:
        add(name, False, f"config expects {expected} but the contract expiry is unknown")
        return
    try:
        want = datetime.strptime(expected.strip(), "%Y-%m-%d").date()
    except ValueError:
        add(name, False, f"config value {expected!r} is not a YYYY-MM-DD date")
        return
    add(name, want == actual,
        f"{actual} as configured" if want == actual
        else f"scrip master says {actual}, config says {want}")
