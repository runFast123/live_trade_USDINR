"""Audit log.

Every decision, every gate failure and every order goes to a dated file next to
the executable as well as to the screen. If a roll goes wrong, this file is the
record of what the app saw and what it did.
"""
from __future__ import annotations

import os
import re
import threading
from collections import deque
from datetime import datetime
from typing import Deque, List, Tuple

_HTML_START = re.compile(r"<!doctype html|<html\b|<head\b|<body\b|<title>", re.I)
_TITLE = re.compile(r"<title[^>]*>\s*(.*?)\s*</title>", re.I | re.S)
_HEADING = re.compile(r"<h1[^>]*>\s*(.*?)\s*</h1>", re.I | re.S)


def without_html(message: str) -> str:
    """A broker error page, reduced to the words on it.

    The vendor client puts the whole response body into its exceptions, and
    when one of Choice's gateways fails the body is an HTML page. Fifteen of
    them on 28 Sep wrote their stylesheets into this file and into the log
    pane, where they pushed the lines that mattered out of view. What came
    before the page -- the call, the status code, the URL -- is kept.
    """
    found = _HTML_START.search(message)
    if not found:
        return message
    page = message[found.start():]
    head = " ".join(message[:found.start()].split())
    head = re.sub(r"\s*Response:\s*$", "", head)
    words = [m.group(1).strip() for m in (_TITLE.search(page),
                                          _HEADING.search(page)) if m]
    said = "; ".join(w for w in words if w) or "nothing readable on it"
    return f"{head} [the broker sent an HTML error page: {said}]".strip()


class Logbook:
    def __init__(self, directory: str, keep: int = 500):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self.path = os.path.join(directory, f"rollover-{datetime.now():%Y-%m-%d}.log")
        self._lock = threading.Lock()
        self._recent: Deque[Tuple[str, str, str]] = deque(maxlen=keep)
        # Counts of everything ever written, not of what is still in the
        # deque. The window used to ask "how many lines are there?" to decide
        # whether to redraw, and the answer stops changing once the buffer is
        # full -- so from the 300th line of a session the pane froze and no
        # alert ever sounded again. These only go up.
        self._seq = 0
        self._alert_seq = 0
        self._last_alert = ""

    def _write(self, level: str, message: str) -> None:
        message = without_html(str(message))
        stamp = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
        line = f"{stamp} [{level:5}] {message}"
        with self._lock:
            self._recent.append((stamp, level, message))
            self._seq += 1
            if level == "ALERT":
                self._alert_seq += 1
                self._last_alert = message
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass  # never let logging stop the trading loop
        try:
            print(line, flush=True)
        except Exception:
            pass  # a windowed build has no console attached

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def warn(self, message: str) -> None:
        self._write("WARN", message)

    def error(self, message: str) -> None:
        self._write("ERROR", message)

    def alert(self, message: str) -> None:
        """Something that needs a human right now."""
        self._write("ALERT", message)

    def recent(self, count: int = 200) -> List[Tuple[str, str, str]]:
        with self._lock:
            return list(self._recent)[-count:]

    @property
    def sequence(self) -> int:
        """How many lines have ever been written. Never goes down."""
        with self._lock:
            return self._seq

    @property
    def alert_sequence(self) -> int:
        """How many ALERTs have ever been written. Never goes down."""
        with self._lock:
            return self._alert_seq

    @property
    def last_alert(self) -> str:
        """The most recent alert, still readable after it scrolls out of the
        buffer -- which is when the sound matters most."""
        with self._lock:
            return self._last_alert
