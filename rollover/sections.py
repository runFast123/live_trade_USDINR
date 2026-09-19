"""One roll among several, and what the account shares between them.

The engine used to hold one pair of contracts, one ladder and one set of
counters. Several rolls at once means splitting that in two: what belongs to a
particular roll, and what belongs to the account no matter how many rolls there
are.

**Per roll:** its two contracts, its ladder and the progress against it, how
many clips it has done today, and whether it in particular is stopped.

**Per account:** whether we are signed in, whether the market is open, which
day's contract list is loaded, the margin, and whether an order is in flight.
That last one is shared on purpose -- one order at a time across every section,
because they compete for the same inventory and the same account.

A Section presents both together, so the safety gates read it exactly as they
read the single session object before: same attribute names, same meanings, and
not one line of gate code had to change.

**A section's config is derived, never stored.** `dry_run` is the reason. Go
Live flips it on the parent config while the app runs, and a section holding a
copy taken at startup would go on reporting the old value -- which for the
margin gate means reporting "not checked in dry run", which is to say passing.
Live orders with the margin check silently disabled. So `cfg` re-derives on
every read, and nothing is cached that could go stale.

**An account halt stops every section; a section halt stops only its own.** A
half-rolled position is a fact about the account's exposure, not about one
campaign, and the other sections sell the same near month into the same
uncertainty.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

from . import book, ladder as ladderlib


@dataclass
class AccountState:
    """What every section shares, whatever it is rolling."""
    logged_in: bool = False
    market_open: Optional[bool] = None
    scrip_file_date: Optional[date] = None
    quote_source: str = "starting"
    margin: Optional[Any] = None

    # One order at a time across every section. They compete for the same
    # inventory and the same margin, so two in flight could each be sound on
    # its own and wrong together.
    in_flight: bool = False

    # A halt that concerns the account rather than one campaign: a half rolled
    # position, an unreadable state file, a fill that could not be confirmed.
    halted_reason: Optional[str] = None
    halted_at: Optional[str] = None

    @property
    def halted(self) -> bool:
        return bool(self.halted_reason)


class Section:
    """One roll, presented so the gates can read it like the old session."""

    def __init__(self, spec, account: AccountState, parent_cfg):
        self.key = spec.key
        self.name = spec.name
        self.index = spec.index
        self.enabled = spec.enabled
        self.account = account

        # The parent, so the config can be re-derived rather than held.
        self._parent = parent_cfg

        # Its own contracts, resolved from the scrip master at startup.
        self.near = None
        self.far = None
        self.near_position_qty: Optional[int] = None

        # Its own day, and its own campaign.
        self.clips_done_today = 0
        self.lots_rolled = 0
        self.own_halt: Optional[str] = None
        self.ladder = ladderlib.Ladder([])
        self.ladder_progress: Dict[str, int] = {}
        self.watch_limits: List[Any] = []

        # What it last decided, for the screen -- and the leg order that went
        # with it, so the gates and the orders that follow cannot disagree.
        self.decision = None
        self.report = None
        self.sequence = None
        self.quotes = None
        self.note = ""

        # Its own evidence file, and its own view of the shared journal.
        # Both are set by the engine: each roll is a different cost against a
        # different limit, so the evidence is not pooled, and every order line
        # says which roll asked for it.
        self.recorder = None
        self.journal = None

    # ------------------------------------------------------- the config
    @property
    def cfg(self):
        """This section's complete configuration, derived now.

        Deliberately not stored. See the module docstring: a cached copy would
        keep saying dry_run after Go Live, and a margin gate that believes it
        is in dry run reports itself as passing.
        """
        for spec in self._parent.section_specs():
            if spec.key == self.key:
                return spec.cfg
        # The section has been edited out from under us. Its own settings are
        # gone, so the parent is the closest honest answer, and `enabled` below
        # is what actually stops it trading.
        return self._parent

    @property
    def still_configured(self) -> bool:
        return any(spec.key == self.key
                   for spec in self._parent.section_specs())

    # ------------------------------- what the gates read from the account
    #
    # Readable and writable through any section, because they describe the
    # account rather than the roll. Writing through one section is writing for
    # all of them, which is the point: signing in, the market opening, the
    # margin and an order being in flight are one fact each, not one per
    # campaign.
    @property
    def logged_in(self) -> bool:
        return self.account.logged_in

    @logged_in.setter
    def logged_in(self, value: bool) -> None:
        self.account.logged_in = bool(value)

    @property
    def market_open(self) -> Optional[bool]:
        return self.account.market_open

    @market_open.setter
    def market_open(self, value: Optional[bool]) -> None:
        self.account.market_open = value

    @property
    def scrip_file_date(self):
        return self.account.scrip_file_date

    @scrip_file_date.setter
    def scrip_file_date(self, value) -> None:
        self.account.scrip_file_date = value

    @property
    def quote_source(self) -> str:
        return self.account.quote_source

    @quote_source.setter
    def quote_source(self, value: str) -> None:
        self.account.quote_source = value

    @property
    def margin(self):
        return self.account.margin

    @margin.setter
    def margin(self, value) -> None:
        self.account.margin = value

    @property
    def in_flight(self) -> bool:
        return self.account.in_flight

    @in_flight.setter
    def in_flight(self, value: bool) -> None:
        # Shared: one order at a time across every section.
        self.account.in_flight = bool(value)

    # --------------------------------------------------------- halting
    @property
    def halted_reason(self) -> Optional[str]:
        """An account halt stops everything; a section halt stops only this.

        The account's comes first, because a half rolled position is a fact
        about the exposure rather than about one campaign, and every section
        sells the same near month into that same uncertainty.
        """
        return self.account.halted_reason or self.own_halt

    @halted_reason.setter
    def halted_reason(self, value: Optional[str]) -> None:
        """Assigning here halts this section alone.

        The engine halts the ACCOUNT for anything reachable from execution;
        this exists for the narrower case where one campaign is in trouble and
        the others are not, and for the tests that predate sections.
        """
        self.own_halt = value

    @property
    def halted(self) -> bool:
        return bool(self.halted_reason)

    # ----------------------------------------------------------- claims
    def allocated(self) -> Optional[int]:
        """What this section may roll in all, or None when nothing caps it.

        None is not zero, and the difference is the whole safety argument. A
        section without a ladder has no campaign cap: it rolls a clip at a time
        for as long as the market allows. Reporting that as nothing is what
        would let two sections sum to less than the position while one of them
        consumed all of it.
        """
        if not self.ladder or not self.ladder.rungs:
            return None
        return self.ladder.total_qty

    def done(self) -> int:
        if not self.ladder or not self.ladder.rungs:
            return 0
        return self.ladder.done_total(self.ladder_progress)

    def claim(self) -> book.Claim:
        return book.Claim(
            name=self.name,
            near_token=str(self.cfg.near_token or ""),
            far_token=str(self.cfg.far_token or ""),
            total=self.allocated(),
            done=self.done(),
            halted=self.halted,
        )

    # ------------------------------------------------------------ state
    def load_from(self, saved) -> None:
        """Take this section's counters and progress out of the state file."""
        self.clips_done_today = int(saved.clips_done or 0)
        self.lots_rolled = int(saved.lots_rolled or 0)
        self.own_halt = saved.halted_reason
        self.ladder_progress = dict(saved.ladder_done or {})

    def save_into(self, saved) -> None:
        saved.name = self.name
        saved.clips_done = self.clips_done_today
        saved.lots_rolled = self.lots_rolled
        saved.halted_reason = self.own_halt
        saved.ladder_done = dict(self.ladder_progress)

    def tokens(self) -> List[str]:
        return [t for t in (self.cfg.near_token, self.cfg.far_token) if t]

    def label(self) -> str:
        return self.name or self.key

    def __repr__(self) -> str:                      # pragma: no cover
        return f"<Section {self.key} {self.name!r}>"
