"""What has to survive the process.

Two pieces of state were held only in memory, and both are dangerous there.

The day's clip count: close the app after a completed roll, reopen it, and the
counter is zero again, so one press of ARM rolls a second time. Nobody would do
that deliberately, but the auto-updater restarts the process, and so does a
crash.

The halt: if the app halts half way through a roll, closing and reopening erased
the halt entirely. Every gate passed again and the operator could arm on top of
a position that was already half moved.

So both live in a small file next to the configuration.

**A halt outlives the trading day.** Daily counters reset when the date turns
over; a halt does not, because a half-rolled position does not repair itself
overnight. Only a person clears it.

**A file that cannot be read is not the same as no file.** If it is missing this
is a first run and starting fresh is right. If it is present but unreadable we
do not know whether today's roll already happened, and the honest answer to that
is to halt and let somebody look.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional

VERSION = 1


@dataclass
class DayState:
    """The part of the engine's state that must outlive the process."""
    version: int = VERSION
    trading_date: str = ""
    clips_done: int = 0
    lots_rolled: int = 0
    halted_reason: Optional[str] = None
    halted_at: Optional[str] = None
    working_orders: List[Dict[str, Any]] = field(default_factory=list)

    # How much of each ladder rung has been rolled, keyed by the rung's basis
    # points. This is campaign progress, not daily: rolling thirty thousand
    # takes as long as it takes, and resetting it overnight would roll the
    # whole position again every morning. It resets when the contracts change,
    # because that is a different roll.
    ladder_campaign: str = ""
    ladder_done: Dict[str, int] = field(default_factory=dict)

    @property
    def halted(self) -> bool:
        return bool(self.halted_reason)


class StateStore:
    """Reads and writes the state file. Every change is written immediately."""

    def __init__(self, directory: str, filename: str = "state.json"):
        self.directory = directory
        self.path = os.path.join(directory, filename)
        self._lock = threading.Lock()
        self.load_error: Optional[str] = None

    # ------------------------------------------------------------------ read
    def load(self, today: Optional[date] = None) -> DayState:
        """Load the state, rolling the day over if the date has changed."""
        today = today or date.today()
        stamp = today.isoformat()

        if not os.path.exists(self.path):
            return DayState(trading_date=stamp)

        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                raise ValueError("not an object")
            known = set(DayState.__dataclass_fields__)
            state = DayState(**{k: v for k, v in raw.items() if k in known})
        except Exception as exc:
            # Present but unreadable. We cannot tell whether today's roll
            # already happened, so say so rather than guess.
            self.load_error = f"{type(exc).__name__}: {exc}"
            return DayState(
                trading_date=stamp,
                halted_reason=(
                    f"the saved state file at {self.path} could not be read "
                    f"({self.load_error}). It is not known whether a roll has "
                    "already happened today. Check the position book, then "
                    "clear this halt."),
                halted_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            )

        if state.trading_date != stamp:
            # A new day: the daily budget resets. The halt does not, and
            # neither does ladder progress, which belongs to the campaign.
            state.trading_date = stamp
            state.clips_done = 0
            state.lots_rolled = 0
            state.working_orders = []
        return state

    def for_campaign(self, state: DayState, campaign: str) -> DayState:
        """Clear ladder progress if this is a different roll from last time.

        Changing contracts starts a new campaign. Carrying the old one over
        would report a fresh roll as already part done.
        """
        if campaign and state.ladder_campaign != campaign:
            state.ladder_campaign = campaign
            state.ladder_done = {}
        return state

    # ----------------------------------------------------------------- write
    def save(self, state: DayState) -> bool:
        """Write atomically, so a crash mid-write cannot corrupt the file."""
        with self._lock:
            try:
                os.makedirs(self.directory, exist_ok=True)
                handle, temporary = tempfile.mkstemp(
                    dir=self.directory, prefix=".state-", suffix=".tmp")
                try:
                    with os.fdopen(handle, "w", encoding="utf-8") as fh:
                        json.dump(asdict(state), fh, indent=2)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(temporary, self.path)
                except BaseException:
                    try:
                        os.remove(temporary)
                    except OSError:
                        pass
                    raise
                return True
            except Exception:
                return False
