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
import time
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

# sha256sum writes "<hash>  <filename>", one line per file.
_SHA_LINE = re.compile(r"^\s*([0-9a-f]{64})\s+\*?(\S+)\s*$",
                       re.IGNORECASE | re.MULTILINE)

# The release ships two executables, the window and the console tool, so
# neither "the first .exe asset" nor "the first hash in the file" identifies
# the one that is running. Both have to be matched by name.
DEFAULT_EXE = "roll_app.exe"


def running_exe_name() -> str:
    """The basename of the executable that would be replaced."""
    if getattr(sys, "frozen", False):
        name = os.path.basename(sys.executable)
        if name.lower().endswith(".exe"):
            return name
    return DEFAULT_EXE


def _pick_asset(assets, wanted: str):
    """The asset for this executable, by name. Never merely the first .exe."""
    wanted = wanted.lower()
    for asset in assets:
        if str(asset.get("name", "")).lower() == wanted:
            return asset
    # A release that does not carry this name at all. Fall back to the window,
    # which is what every release has shipped since the first one.
    for asset in assets:
        if str(asset.get("name", "")).lower() == DEFAULT_EXE:
            return asset
    return None


def _sha_for(text: str, wanted: str) -> Optional[str]:
    """The checksum belonging to `wanted`, out of a possibly multi-line file."""
    wanted = os.path.basename(wanted).lower()
    lines = _SHA_LINE.findall(text or "")
    for digest, name in lines:
        if os.path.basename(name).lower() == wanted:
            return digest.lower()
    if len(lines) == 1:
        # A single-file checksum from an older release, whatever it names.
        return lines[0][0].lower()
    return None


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


def check(repo: str, exe_name: Optional[str] = None) -> Optional[Release]:
    """Return the latest release when it is newer than this build, else None.

    Network problems return None rather than raising: a missed update check
    must never be able to stop the app from starting.
    """
    wanted = exe_name or running_exe_name()
    try:
        payload = json.loads(_get(API.format(repo=repo)).decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError):
        return None

    tag = payload.get("tag_name") or payload.get("name") or ""
    if not tag or not is_newer(tag):
        return None

    assets = payload.get("assets") or []
    exe = _pick_asset(assets, wanted)
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
            sha = _sha_for(text, str(exe.get("name") or wanted))
        except (urllib.error.URLError, OSError, UnicodeDecodeError):
            sha = None
    if sha is None:
        sha = _sha_for(notes, str(exe.get("name") or wanted))
    if sha is None and not _SHA_LINE.search(notes):
        # Notes carrying a bare hash and no filename. Unambiguous only when
        # there is exactly one of them; two would be a coin toss, and the
        # download then refuses rather than verifying against the wrong file.
        found = _SHA_IN_NOTES.findall(notes)
        sha = found[0].lower() if len(found) == 1 else None

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


def write_swap_script(target: str, new_exe: str, backup: str,
                      script_path: Optional[str] = None,
                      relaunch: bool = True) -> str:
    """Write the batch file that replaces the executable, and return its path.

    Windows will not let a running executable be overwritten, so the swap has
    to outlive this process. The script waits until the target is no longer
    locked, keeps the old build alongside as .old.exe, moves the new one into
    place, and puts the old one back if that move fails.

    It gives up after about two minutes. An app that has not closed by then is
    not merely slow, and leaving a script spinning forever behind the user's
    back is worse than abandoning the update.

    Kept separate from running it so the swap can be exercised in a test
    without replacing anything real.
    """
    if script_path is None:
        script_path = os.path.join(tempfile.gettempdir(), "roll_app_update.cmd")

    lines = [
        "@echo off",
        "setlocal",
        f'set "TARGET={target}"',
        f'set "NEWEXE={new_exe}"',
        f'set "BACKUP={backup}"',
        "set /a TRIES=0",
        ":wait",
        "set /a TRIES+=1",
        "if %TRIES% GTR 60 goto giveup",
        "ping -n 2 127.0.0.1 >nul",
        # Appending nothing to the file fails while it is still running.
        '2>nul (>>"%TARGET%" call ) || goto wait',
        'if exist "%BACKUP%" del /q "%BACKUP%"',
        'move /y "%TARGET%" "%BACKUP%" >nul',
        'move /y "%NEWEXE%" "%TARGET%" >nul',
        'if errorlevel 1 move /y "%BACKUP%" "%TARGET%" >nul',
    ]
    if relaunch:
        lines.append('start "" "%TARGET%"')
    lines += [
        "goto done",
        ":giveup",
        'del /q "%NEWEXE%"',
        ":done",
        'del /q "%~f0"',
    ]

    with open(script_path, "w", encoding="ascii", newline="\r\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return script_path


def stage_update(new_exe: str) -> str:
    """Start the swap running in the background and return its script path.

    It does **not** end this process, and it must not: the swap cannot begin
    until the executable is unlocked, which only happens once this process is
    gone. Ending it is the caller's job, and has to be done from the main
    thread, because sys.exit() on a worker thread raises SystemExit in that
    thread alone and leaves the process very much alive.

    That was exactly the bug: the update downloaded, verified, logged
    "Restarting", exited a worker thread, and then nothing happened. The
    executable stayed locked, the script sat waiting for a lock that would
    never clear, and the download was orphaned in the temp directory.
    """
    ok, why = can_install()
    if not ok:
        raise UpdateError(why)

    target = os.path.abspath(sys.executable)
    script = write_swap_script(target, new_exe, target + ".old.exe")

    subprocess.Popen(["cmd", "/c", script],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                     | getattr(subprocess, "DETACHED_PROCESS", 0))
    return script


def quit_now(code: int = 0) -> None:
    """End the process immediately so the executable is released.

    os._exit rather than sys.exit: a lingering non-daemon thread, or Tk still
    unwinding, would keep the file locked and strand the update. Everything
    that matters is already on disk, because the log writes and closes on every
    line.
    """
    sys.stdout.flush() if sys.stdout else None
    sys.stderr.flush() if sys.stderr else None
    os._exit(code)


def cleanup_stale_downloads(older_than_seconds: int = 3600) -> int:
    """Delete update downloads left behind by an attempt that never finished.

    Each is the size of a whole build, so a few failed attempts quietly cost
    well over a hundred megabytes.
    """
    removed = 0
    folder = tempfile.gettempdir()
    now = time.time()
    try:
        names = os.listdir(folder)
    except OSError:
        return 0

    for name in names:
        if not (name.startswith("roll_app_update_") and name.endswith(".exe")):
            continue
        path = os.path.join(folder, name)
        try:
            if now - os.path.getmtime(path) > older_than_seconds:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed
