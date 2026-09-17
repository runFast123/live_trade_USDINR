"""Audit log.

Every decision, every gate failure and every order goes to a dated file next to
the executable as well as to the screen. If a roll goes wrong, this file is the
record of what the app saw and what it did.
"""
from __future__ import annotations

import os
import threading
from collections import deque
from datetime import datetime
from typing import Deque, List, Tuple


class Logbook:
    def __init__(self, directory: str, keep: int = 500):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self.path = os.path.join(directory, f"rollover-{datetime.now():%Y-%m-%d}.log")
        self._lock = threading.Lock()
        self._recent: Deque[Tuple[str, str, str]] = deque(maxlen=keep)

    def _write(self, level: str, message: str) -> None:
        stamp = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
        line = f"{stamp} [{level:5}] {message}"
        with self._lock:
            self._recent.append((stamp, level, message))
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
