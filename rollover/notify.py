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


# ------------------------------------------------------------------ Telegram
_telegram_cfg = {
    "token": None,
    "chat_id": None,
    "enabled": False,
}
_last_opportunity_sent = 0.0


def configure_telegram(token: str | None, chat_id: str | None, enabled: bool = True) -> None:
    """Store Telegram bot credentials and enable state."""
    _telegram_cfg["token"] = str(token).strip() if token else None
    _telegram_cfg["chat_id"] = str(chat_id).strip() if chat_id else None
    _telegram_cfg["enabled"] = bool(_telegram_cfg["token"] and _telegram_cfg["chat_id"] and enabled)


def send_telegram(text: str, is_opportunity: bool = False, min_interval_sec: float = 45.0) -> None:
    """Send a notification to Telegram in a background daemon thread.

    Never blocks, never raises. Opportunity crossings are debounced to avoid spam.
    """
    if not _telegram_cfg.get("enabled"):
        return
    token = _telegram_cfg.get("token")
    chat_id = _telegram_cfg.get("chat_id")
    if not token or not chat_id:
        return

    global _last_opportunity_sent
    import time
    now = time.monotonic()
    if is_opportunity:
        if now - _last_opportunity_sent < min_interval_sec:
            return
        _last_opportunity_sent = now

    def _post():
        import json
        import urllib.request
        chat_ids = [c.strip() for c in str(chat_id).split(",") if c.strip()]
        for cid in chat_ids:
            try:
                payload = json.dumps({
                    "chat_id": cid,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }).encode("utf-8")
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=6) as resp:
                    pass
            except Exception:
                pass

    threading.Thread(target=_post, daemon=True).start()

