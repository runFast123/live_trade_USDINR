"""Configuration for the rollover app.

Everything that could differ between runs lives here, loaded from config.json
next to the executable. Nothing about the contract, the limit or the size is
hard-coded in the trading logic.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import time as dtime
from decimal import Decimal
from typing import Optional

from .money import D, PriceError

# Order validity, as swagger.json documents it.
DAY = 1
IOC = 4


def app_dir() -> str:
    """Directory holding config.json: next to the .exe when frozen, else the project."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConfigError(ValueError):
    pass


def _parse_hhmm(text: str, label: str) -> dtime:
    try:
        hh, mm = str(text).strip().split(":")
        return dtime(int(hh), int(mm))
    except Exception as exc:
        raise ConfigError(f"{label} must look like 09:05, got {text!r}") from exc


@dataclass
class RollConfig:
    # --- credentials -------------------------------------------------------
    vendor_id: str = ""
    api_key: str = ""
    mobile_no: str = ""
    base_url: str = "https://finxomne.choiceindia.com"

    # --- the two legs ------------------------------------------------------
    segment_id: int = 13                 # 13 = NSE currency derivatives
    near_token: str = ""                 # September contract, the leg we SELL
    far_token: str = ""                  # November contract, the leg we BUY
    near_expiry: str = ""                # expected, e.g. 2026-09-28; verified, never assumed
    far_expiry: str = ""                 # expected, e.g. 2026-11-26
    expected_lot_size: int = 1000        # USDINR futures
    underlying: str = "USDINR"

    # --- the rule ----------------------------------------------------------
    #
    # A roll's cost scales with tenor, so the limit does too. In "bps" mode the
    # limit comes from the schedule below, chosen by how far apart the two
    # contracts actually expire, and converted to rupees against the live
    # price. In "absolute" mode roll_limit is used as a fixed rupee figure and
    # the tenor is ignored, which is only ever right for one pair.
    limit_mode: str = "bps"              # "bps" or "absolute"
    limit_bps_schedule: dict = field(     # months between expiries -> basis points
        default_factory=lambda: {"1": "30", "2": "50"})
    tenor_tolerance_days: int = 10       # how far from a nominal month still counts
    roll_limit: str = "0.30"             # used only when limit_mode is "absolute"
    lots: int = 1                        # one clip
    allowance_ticks: int = 0             # price give on each leg; 0 is strictest
    tick: str = "0.0025"

    # --- data quality gates ------------------------------------------------
    max_quote_age_sec: float = 3.0
    max_leg_spread: str = "0.0500"       # reject a leg whose own bid/ask spread is wider
    price_band_low: str = "70"           # a USDINR price outside this band is bad data
    price_band_high: str = "130"
    roll_cost_band_low: str = "-2.00"    # a roll cost outside this band is bad data
    roll_cost_band_high: str = "5.00"
    price_divisor: Optional[str] = None  # None = detect 1 or 100 from the price band
    bid_field: Optional[str] = None      # None = discover the field name in the touchline
    ask_field: Optional[str] = None
    expiry_field: Optional[str] = None   # None = discover in the scrip master row

    # --- timing ------------------------------------------------------------
    poll_interval_sec: float = 1.0
    use_live_feed: bool = True           # websocket first, polling as fallback
    feed_max_silence: float = 120.0      # silence past this and the feed is doubted
    window_open: str = "09:05"
    window_close: str = "16:55"
    expiry_day_cutoff: str = "12:25"     # near leg stops trading 12:30 on its expiry day
    arm_timeout_sec: int = 120

    # --- execution ---------------------------------------------------------
    # RL_MKT and SL_MKT exist, but a market order is the one thing this
    # strategy must never send: the whole instruction is a price limit.
    order_type: str = "RL_LIMIT"
    product_type: str = "D"              # carry forward

    # 1 = Day, 4 = immediate-or-cancel. swagger.json documents both; the app
    # was written believing only Day existed and builds IOC by hand -- place,
    # poll, cancel the remainder -- which is what creates the cancel race and
    # the window where a partial fill sits working while the second leg is
    # priced. Real IOC removes all of it.
    #
    # Still defaulted to Day because it has not been exercised against the
    # exchange yet. Switch once a live order can be placed at all.
    validity: int = DAY
    fill_timeout_sec: float = 2.0        # unfilled remainder is cancelled after this
    max_clips_per_day: int = 1
    require_market_status: bool = True   # False = trust window_open/window_close instead
    require_fresh_scrip: bool = True     # refuse if the scrip master is not today's
    require_position: bool = True        # refuse to sell a near leg you do not hold
    require_touch_size: bool = True      # refuse unless both touches can fill the clip
    auto_unwind_on_leg2_failure: bool = False
    dry_run: bool = True                 # nothing is sent to the exchange while true

    # Whether an order quantity reaches the exchange as contracts or as units
    # of the underlying has never been established, and the app sends
    # lots x MarketLot. If the exchange counts contracts, that is a
    # thousandfold over-order. The live probe of 18 Sep 2026 could not settle
    # it: the order was refused on account entitlement before the exchange
    # validated anything. See LIVE-FINDINGS.md.
    #
    # Set to true only once Choice have confirmed it, or once an accepted order
    # has proved it. Live mode refuses to engage while it is false, which is
    # the app being honest about what it does not know rather than a limit on
    # what the operator may decide.
    quantity_unit_confirmed: bool = False

    # --- observing -----------------------------------------------------------
    record_market: bool = True           # write a market sample CSV
    record_interval_sec: float = 5.0
    alert_on_qualify: bool = True        # say something when the cost first clears

    # --- updates -----------------------------------------------------------
    update_check: bool = True            # ask GitHub once at startup
    update_repo: str = "runFast123/live_trade_USDINR"

    # ---------------------------------------------------------------- loaders
    @classmethod
    def load(cls, path: str) -> "RollConfig":
        if not os.path.exists(path):
            raise ConfigError(
                f"config file not found: {path}\n"
                "Copy config.example.json to config.json and fill it in."
            )
        with open(path, "r", encoding="utf-8") as fh:
            try:
                raw = json.load(fh)
            except json.JSONDecodeError as exc:
                raise ConfigError(f"config.json is not valid JSON: {exc}") from exc

        if not isinstance(raw, dict):
            raise ConfigError("config.json must contain a JSON object")

        known = set(cls.__dataclass_fields__)
        unknown = set(raw) - known
        if unknown:
            raise ConfigError(
                "config.json has keys this app does not understand: "
                + ", ".join(sorted(unknown))
            )
        cfg = cls(**raw)
        cfg.validate()
        return cfg

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)

    # ------------------------------------------------------------- validation
    def validate(self) -> None:
        """Reject a configuration that could produce a wrong trade.

        This runs before any connection is made. A configuration error must stop
        the app at startup, not halfway through a roll.
        """
        errors = []

        for name in ("roll_limit", "tick", "max_leg_spread",
                     "price_band_low", "price_band_high",
                     "roll_cost_band_low", "roll_cost_band_high"):
            try:
                D(getattr(self, name))
            except PriceError as exc:
                errors.append(f"{name}: {exc}")

        if self.limit_mode not in ("bps", "absolute"):
            errors.append('limit_mode must be "bps" or "absolute"')

        if self.limit_mode == "bps":
            from .limits import LimitError, parse_schedule
            try:
                schedule = parse_schedule(self.limit_bps_schedule)
                if not schedule:
                    errors.append("limit_mode is bps but limit_bps_schedule is empty")
            except LimitError as exc:
                errors.append(str(exc))
            if self.tenor_tolerance_days < 1:
                errors.append("tenor_tolerance_days must be at least 1")
            elif self.tenor_tolerance_days > 15:
                # Beyond a fortnight, neighbouring tenors start to overlap and a
                # two month roll could be matched to the one month limit.
                errors.append("tenor_tolerance_days above 15 would let one tenor "
                              "be mistaken for another")

        if not errors:
            if self.roll_limit_d <= 0:
                errors.append("roll_limit must be positive")
            if self.tick_d <= 0:
                errors.append("tick must be positive")
            if self.price_band_low_d >= self.price_band_high_d:
                errors.append("price_band_low must be below price_band_high")
            if self.roll_cost_band_low_d >= self.roll_cost_band_high_d:
                errors.append("roll_cost_band_low must be below roll_cost_band_high")
            if (self.limit_mode == "absolute"
                    and self.roll_limit_d > self.roll_cost_band_high_d):
                errors.append("roll_limit sits above roll_cost_band_high, so the sanity "
                              "band would never let a qualifying quote through")

        if self.lots < 1:
            errors.append("lots must be at least 1")
        if self.allowance_ticks < 0:
            errors.append("allowance_ticks cannot be negative")
        if self.expected_lot_size < 1:
            errors.append("expected_lot_size must be at least 1")
        if self.segment_id <= 0:
            errors.append("segment_id must be positive")
        if self.max_clips_per_day < 1:
            errors.append("max_clips_per_day must be at least 1")
        if self.poll_interval_sec <= 0:
            errors.append("poll_interval_sec must be positive")
        if self.max_quote_age_sec <= 0:
            errors.append("max_quote_age_sec must be positive")
        if self.fill_timeout_sec <= 0:
            errors.append("fill_timeout_sec must be positive")
        if self.order_type not in ("RL_LIMIT", "SL_LIMIT"):
            errors.append("order_type must be RL_LIMIT or SL_LIMIT; a market order "
                          "would ignore the price limit that is the whole rule")
        if self.validity not in (DAY, IOC):
            errors.append(f"validity must be {DAY} (day) or {IOC} "
                          "(immediate or cancel)")
        if self.product_type not in ("D", "M"):
            errors.append("product_type must be D (carry forward) or M (intraday)")

        if self.near_token and self.far_token and self.near_token == self.far_token:
            errors.append("near_token and far_token are the same contract")

        if self.price_divisor is not None:
            try:
                if D(self.price_divisor) <= 0:
                    errors.append("price_divisor must be positive")
            except PriceError as exc:
                errors.append(f"price_divisor: {exc}")

        try:
            if _parse_hhmm(self.window_open, "window_open") >= _parse_hhmm(
                    self.window_close, "window_close"):
                errors.append("window_open must be before window_close")
        except ConfigError as exc:
            errors.append(str(exc))
        try:
            _parse_hhmm(self.expiry_day_cutoff, "expiry_day_cutoff")
        except ConfigError as exc:
            errors.append(str(exc))

        if errors:
            raise ConfigError("Configuration is not safe to run:\n  - " + "\n  - ".join(errors))

    # ------------------------------------------------------------- accessors
    @property
    def roll_limit_d(self) -> Decimal:
        return D(self.roll_limit)

    @property
    def tick_d(self) -> Decimal:
        return D(self.tick)

    @property
    def allowance(self) -> Decimal:
        return self.tick_d * self.allowance_ticks

    @property
    def max_leg_spread_d(self) -> Decimal:
        return D(self.max_leg_spread)

    @property
    def price_band_low_d(self) -> Decimal:
        return D(self.price_band_low)

    @property
    def price_band_high_d(self) -> Decimal:
        return D(self.price_band_high)

    @property
    def roll_cost_band_low_d(self) -> Decimal:
        return D(self.roll_cost_band_low)

    @property
    def roll_cost_band_high_d(self) -> Decimal:
        return D(self.roll_cost_band_high)

    @property
    def window_open_t(self) -> dtime:
        return _parse_hhmm(self.window_open, "window_open")

    @property
    def window_close_t(self) -> dtime:
        return _parse_hhmm(self.window_close, "window_close")

    @property
    def expiry_cutoff_t(self) -> dtime:
        return _parse_hhmm(self.expiry_day_cutoff, "expiry_day_cutoff")

    @property
    def clip_qty(self) -> int:
        """Order quantity in units, which is what the API expects, not lots."""
        return self.lots * self.expected_lot_size

    def redacted(self) -> dict:
        """Config as a dict with the secrets masked, safe to write to the log."""
        data = asdict(self)
        if data.get("api_key"):
            data["api_key"] = "***" + str(data["api_key"])[-4:]
        if data.get("mobile_no"):
            data["mobile_no"] = "******" + str(data["mobile_no"])[-4:]
        return data
