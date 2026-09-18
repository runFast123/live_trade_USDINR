"""A record that stands up afterwards.

The log is prose meant for a person reading the screen. It is not something you
can answer questions with: "what limit was in force when that order went out?",
"what did the broker actually say?", "which rung was that clip charged to?" The
answers existed -- OrderOutcome carries `order_ref` and the raw response, and
the decision carries the limit and how it was arrived at -- and none of them
were ever written down.

So: one JSON object per line, one file per day, appended and flushed as things
happen. A crash loses at most the line being written, and every line before it
is already on disk and readable.

**It never interferes.** Every failure here is swallowed. A journal that could
stop a roll, or worse stop a halt from being recorded, would be a liability
rather than a record.

**Decimals become strings, not floats.** The whole program keeps prices exact;
writing 0.2873 as a float would put a number in the record that is not the
number the decision was made on.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Optional

VERSION = 1


def _plain(value: Any) -> Any:
    """Anything the journal might be handed, as something JSON can hold."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    return str(value)


class Journal:
    """Appends one line per event to data/journal-<date>.jsonl."""

    def __init__(self, directory: str, enabled: bool = True):
        self.directory = directory
        self.enabled = enabled
        self._lock = threading.Lock()
        self._day: Optional[str] = None
        self._path: Optional[str] = None
        self.failed = False          # set once, so it complains only the once

    def path_for(self, when: Optional[date] = None) -> str:
        stamp = (when or date.today()).isoformat()
        return os.path.join(self.directory, f"journal-{stamp}.jsonl")

    def write(self, kind: str, **fields: Any) -> bool:
        """Record one event. Returns whether it was written. Never raises."""
        if not self.enabled:
            return False
        try:
            now = datetime.now().astimezone()
            entry: Dict[str, Any] = {
                "at": now.isoformat(timespec="milliseconds"),
                "kind": str(kind),
                "v": VERSION,
            }
            entry.update({k: _plain(v) for k, v in fields.items()})

            with self._lock:
                path = self.path_for(now.date())
                if path != self._path:
                    os.makedirs(self.directory, exist_ok=True)
                    self._path = path
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    fh.flush()
            return True
        except Exception:
            self.failed = True
            return False

    # -------------------------------------------------- the events themselves
    def decision(self, decision, dry_run: bool, armed: bool) -> bool:
        """What the rule concluded, and the limit it concluded it against."""
        limit = getattr(decision, "limit_detail", None)
        rung = getattr(decision, "active_rung", None)
        return self.write(
            "decision",
            dry_run=dry_run,
            armed=armed,
            near_bid=decision.near_bid,
            far_ask=decision.far_ask,
            roll_cost=decision.roll_cost,
            cost_bps=decision.cost_bps,
            limit=decision.limit,
            limit_bps=decision.limit_bps,
            limit_mode=getattr(limit, "mode", None),
            tenor_days=getattr(limit, "tenor_days", None),
            reference=decision.reference,
            sell_limit=decision.sell_limit,
            buy_limit=decision.buy_limit,
            worst_case=decision.worst_case,
            qty=decision.qty,
            qualifies=decision.qualifies,
            rung_bps=getattr(getattr(rung, "rung", None), "bps", None),
            blockers=list(decision.blockers),
        )

    def order(self, leg: str, side: int, token: str, qty: int, price,
              outcome) -> bool:
        """One leg, with the broker's own reference and its raw answer."""
        return self.write(
            "order",
            leg=leg,
            side="SELL" if side == 2 else "BUY",
            token=token,
            requested_qty=qty,
            limit_price=price,
            sent=getattr(outcome, "sent", None),
            filled_qty=getattr(outcome, "filled_qty", None),
            certain=getattr(outcome, "certain", None),
            order_ref=getattr(outcome, "order_ref", None),
            detail=getattr(outcome, "detail", None),
            raw=getattr(outcome, "raw", None),
        )

    def reconciliation(self, result) -> bool:
        return self.write(
            "reconcile",
            agreed=getattr(result, "agreed", None),
            checked=getattr(result, "checked", None),
            expected=getattr(result, "expected", None),
            near_moved=getattr(result, "near_moved", None),
            far_moved=getattr(result, "far_moved", None),
            before=_positions(getattr(result, "before", None)),
            after=_positions(getattr(result, "after", None)),
            detail=getattr(result, "detail", None),
        )

    def margin(self, estimate, qty: int) -> bool:
        return self.write(
            "margin",
            qty=qty,
            required=getattr(estimate, "required", None),
            available=getattr(estimate, "available", None),
            affordable=getattr(estimate, "affordable", None),
            detail=getattr(estimate, "detail", None),
        )

    def halt(self, reason: str) -> bool:
        return self.write("halt", reason=reason)

    def note(self, kind: str, message: str, **fields: Any) -> bool:
        return self.write(kind, message=message, **fields)


def _positions(value) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    return {"near": getattr(value, "near", None),
            "far": getattr(value, "far", None)}


def read(path: str):
    """Every entry in a journal file, skipping anything unreadable.

    A half-written final line from a crash must not make the whole record
    unreadable, which is the entire reason this is one object per line.
    """
    entries = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return entries
