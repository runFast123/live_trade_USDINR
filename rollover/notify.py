"""Getting a person's attention.

The operator checks every fifteen to thirty minutes. A red banner is no use to
somebody who is not looking at the screen, and the two things worth interrupting
for — a halt, and the cost finally clearing the limit — are both time-critical:
one leaves a position in an unknown state, the other is a window that may last
seconds.

Sound is the minimum. It is deliberately not silent-by-default, and it is
deliberately not a single soft ping for a halt.
"""
from __future__ import annotations

import threading

try:                                    # Windows only, which is the target
    import winsound
except ImportError:                     # pragma: no cover - not the target OS
    winsound = None

# (frequency Hz, duration ms) pairs. The halt pattern is lower, repeated and
# longer, because it means something is wrong rather than something is ready.
GOOD = ((880, 120), (1175, 180))
ALARM = ((700, 260), (520, 260), (700, 260), (520, 420))

_lock = threading.Lock()


def _play(pattern) -> None:
    if winsound is None:
        return
    # One at a time: overlapping alarms sound like noise, not an alarm.
    if not _lock.acquire(blocking=False):
        return
    try:
        for frequency, duration in pattern:
            winsound.Beep(frequency, duration)
    except Exception:
        pass
    finally:
        _lock.release()


def _sound(pattern) -> None:
    """Play without holding up the caller. Never raises."""
    try:
        threading.Thread(target=_play, args=(pattern,), daemon=True).start()
    except Exception:
        pass


def alarm() -> None:
    """Something is wrong and needs a person."""
    _sound(ALARM)


def chime() -> None:
    """Something the operator has been waiting for has happened."""
    _sound(GOOD)


def for_level(level: str) -> None:
    """Sound whatever suits a log level, or nothing."""
    if level in ("ALERT", "ERROR"):
        alarm()


def available() -> bool:
    return winsound is not None
