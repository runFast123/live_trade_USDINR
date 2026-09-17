"""Updating in place from GitHub releases.

The point is that nobody has to go and download a new build by hand. The app
asks GitHub once at startup whether there is a newer release, and if there is,
one button downloads it, checks it and restarts into it.

What it deliberately does not do is install on its own. This application places
real orders, so a new binary arriving and running itself without anybody
deciding to is not a trade-off worth making. The check is quiet, the install is
a choice, and it is refused outright while the app is armed or an order is
working.

Every download is verified against the SHA256 the release publishes. If the
release has no checksum, or the checksum does not match, the update is thrown
away rather than installed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional, Tuple

from . import __version__

API = "https://api.github.com/repos/{repo}/releases/latest"
TIMEOUT = 20
USER_AGENT = f"usdinr-rollover/{__version__}"

# A release may ship the checksum as a .sha256 asset or put it in the notes.
_SHA_IN_NOTES = re.compile(r"\b([0-9a-f]{64})\b", re.IGNORECASE)


class UpdateError(RuntimeError):
    pass


@dataclass
class Release:
    version: str
    url: str                 # the .exe asset
    size: int
    notes: str
    sha256: Optional[str]
    html_url: str

    @property
    def label(self) -> str:
        return f"v{self.version}"


def parse_version(text: str) -> Tuple[int, ...]:
    """Turn 'v1.2.3' into (1, 2, 3). Unparseable parts count as zero."""
    cleaned = str(text or "").strip().lstrip("vV").split("+")[0].split("-")[0]
    parts = []
    for chunk in cleaned.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


def _get(url: str, accept: str = "application/vnd.github+json") -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def check(repo: str) -> Optional[Release]:
    """Return the latest release when it is newer than this build, else None.

    Network problems return None rather than raising: a missed update check
    must never be able to stop the app from starting.
    """
    try:
        payload = json.loads(_get(API.format(repo=repo)).decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError):
        return None

    tag = payload.get("tag_name") or payload.get("name") or ""
    if not tag or not is_newer(tag):
        return None

    assets = payload.get("assets") or []
    exe = next((a for a in assets
                if str(a.get("name", "")).lower().endswith(".exe")), None)
    if not exe:
        return None

    notes = payload.get("body") or ""
    sha = None

    sha_asset = next((a for a in assets
                      if str(a.get("name", "")).lower().endswith(".sha256")), None)
    if sha_asset:
        try:
            text = _get(sha_asset["browser_download_url"],
                        accept="application/octet-stream").decode("utf-8")
            found = _SHA_IN_NOTES.search(text)
            sha = found.group(1).lower() if found else None
        except (urllib.error.URLError, OSError, UnicodeDecodeError):
            sha = None
    if sha is None:
        found = _SHA_IN_NOTES.search(notes)
        sha = found.group(1).lower() if found else None

    return Release(
        version=str(tag).lstrip("vV"),
        url=exe["browser_download_url"],
        size=int(exe.get("size") or 0),
        notes=notes.strip(),
        sha256=sha,
        html_url=payload.get("html_url", ""),
    )


def download(release: Release, progress=None) -> str:
    """Fetch the new build to a temporary file and verify it. Returns the path."""
    if not release.sha256:
        raise UpdateError(
            "the release does not publish a SHA256 checksum, so the download "
            "cannot be verified. Update by hand from " + release.html_url)

    handle, path = tempfile.mkstemp(suffix=".exe", prefix="roll_app_update_")
    os.close(handle)

    digest = hashlib.sha256()
    read = 0
    try:
        request = urllib.request.Request(
            release.url, headers={"User-Agent": USER_AGENT,
                                  "Accept": "application/octet-stream"})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response, \
                open(path, "wb") as out:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                read += len(chunk)
                if progress and release.size:
                    progress(read / release.size)
    except (urllib.error.URLError, OSError) as exc:
        _remove(path)
        raise UpdateError(f"download failed: {exc}") from exc

    if digest.hexdigest().lower() != release.sha256:
        _remove(path)
        raise UpdateError(
            "the downloaded file does not match the published checksum. "
            "It has been discarded.")

    if release.size and read != release.size:
        _remove(path)
        raise UpdateError(
            f"expected {release.size} bytes but got {read}. Discarded.")

    return path


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def can_install() -> Tuple[bool, str]:
    """Only a frozen build can replace itself."""
    if not getattr(sys, "frozen", False):
        return False, ("this is running from source, so there is nothing to "
                       "replace. Use git pull instead.")
    return True, ""


def install_and_restart(new_exe: str) -> None:
    """Swap the running executable for the new one and start it again.

    Windows will not let a running executable be overwritten, so a small batch
    file waits for this process to exit, keeps the old build alongside as
    .old.exe, moves the new one into place and launches it.
    """
    ok, why = can_install()
    if not ok:
        raise UpdateError(why)

    target = os.path.abspath(sys.executable)
    backup = target + ".old.exe"

    script = os.path.join(tempfile.gettempdir(), "roll_app_update.cmd")
    with open(script, "w", encoding="ascii") as fh:
        fh.write(
            "@echo off\r\n"
            "setlocal\r\n"
            f'set "TARGET={target}"\r\n'
            f'set "NEWEXE={new_exe}"\r\n'
            f'set "BACKUP={backup}"\r\n'
            ":wait\r\n"
            'ping -n 2 127.0.0.1 >nul\r\n'
            '2>nul (>>"%TARGET%" call ) || goto wait\r\n'
            'if exist "%BACKUP%" del /q "%BACKUP%"\r\n'
            'move /y "%TARGET%" "%BACKUP%" >nul\r\n'
            'move /y "%NEWEXE%" "%TARGET%" >nul\r\n'
            'if errorlevel 1 move /y "%BACKUP%" "%TARGET%" >nul\r\n'
            'start "" "%TARGET%"\r\n'
            'del /q "%~f0"\r\n'
        )

    subprocess.Popen(["cmd", "/c", script],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                     | getattr(subprocess, "DETACHED_PROCESS", 0))
    sys.exit(0)
