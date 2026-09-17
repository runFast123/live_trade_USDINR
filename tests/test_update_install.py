"""Tests for downloading and installing an update.

The download tests stub the HTTP call. The swap test is real: it writes the
batch file the app would write, runs it against throwaway files, and checks
that the replacement actually happened on this machine. The one thing not
exercised is replacing a *running* executable, because that would mean killing
the test process.
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest

from rollover import updater
from rollover.updater import Release, UpdateError, write_swap_script


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeResponse(io.BytesIO):
    """Enough of an HTTP response for urlopen's context manager."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class TestDownloadVerification(unittest.TestCase):
    def setUp(self):
        self.payload = b"pretend this is a 36 megabyte executable"
        self._real_urlopen = updater.urllib.request.urlopen

    def tearDown(self):
        updater.urllib.request.urlopen = self._real_urlopen

    def serve(self, data: bytes):
        updater.urllib.request.urlopen = lambda req, timeout=None: FakeResponse(data)

    MISSING = object()          # so sha=None can mean "no checksum published"

    def release(self, sha=MISSING, size=None):
        return Release(version="9.9.9", url="https://example.invalid/roll_app.exe",
                       size=len(self.payload) if size is None else size,
                       notes="",
                       sha256=sha256(self.payload) if sha is self.MISSING else sha,
                       html_url="https://example.invalid/r")

    def test_a_good_download_is_kept(self):
        self.serve(self.payload)
        path = updater.download(self.release())
        try:
            self.assertTrue(os.path.exists(path))
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), self.payload)
        finally:
            os.remove(path)

    def test_a_wrong_checksum_is_discarded(self):
        self.serve(b"a different file entirely")
        with self.assertRaises(UpdateError) as ctx:
            updater.download(self.release())
        self.assertIn("does not match the published checksum", str(ctx.exception))

    def test_nothing_is_left_behind_when_the_checksum_fails(self):
        before = set(os.listdir(tempfile.gettempdir()))
        self.serve(b"wrong")
        with self.assertRaises(UpdateError):
            updater.download(self.release())
        after = set(os.listdir(tempfile.gettempdir()))
        leftovers = [n for n in after - before if n.startswith("roll_app_update_")]
        self.assertEqual(leftovers, [])

    def test_a_short_download_is_discarded(self):
        self.serve(self.payload)
        with self.assertRaises(UpdateError) as ctx:
            updater.download(self.release(size=len(self.payload) + 500))
        self.assertIn("expected", str(ctx.exception))

    def test_a_release_with_no_checksum_is_refused_before_downloading(self):
        called = []
        updater.urllib.request.urlopen = lambda *a, **k: called.append(1)
        with self.assertRaises(UpdateError):
            updater.download(self.release(sha=None))
        self.assertEqual(called, [])

    def test_progress_is_reported(self):
        self.serve(self.payload)
        seen = []
        path = updater.download(self.release(), progress=seen.append)
        try:
            self.assertTrue(seen)
            self.assertLessEqual(max(seen), 1.0)
        finally:
            os.remove(path)


class TestSwapScript(unittest.TestCase):
    """The batch file is run for real against throwaway files."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="swaptest_")
        self.target = os.path.join(self.dir, "app.exe")
        self.new = os.path.join(self.dir, "new.exe")
        self.backup = self.target + ".old.exe"
        self.script = os.path.join(self.dir, "swap.cmd")
        with open(self.target, "w") as fh:
            fh.write("OLD BUILD")
        with open(self.new, "w") as fh:
            fh.write("NEW BUILD")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_swap(self, relaunch=False, timeout=60):
        path = write_swap_script(self.target, self.new, self.backup,
                                 script_path=self.script, relaunch=relaunch)
        subprocess.run(["cmd", "/c", path], timeout=timeout,
                       capture_output=True, check=False)

    def read(self, path):
        with open(path) as fh:
            return fh.read()

    def test_the_new_build_takes_the_targets_place(self):
        self.run_swap()
        self.assertEqual(self.read(self.target), "NEW BUILD")

    def test_the_old_build_is_kept_as_a_backup(self):
        self.run_swap()
        self.assertTrue(os.path.exists(self.backup))
        self.assertEqual(self.read(self.backup), "OLD BUILD")

    def test_the_downloaded_file_is_moved_not_copied(self):
        self.run_swap()
        self.assertFalse(os.path.exists(self.new))

    def test_the_script_deletes_itself(self):
        self.run_swap()
        self.assertFalse(os.path.exists(self.script))

    def test_an_existing_backup_is_replaced(self):
        with open(self.backup, "w") as fh:
            fh.write("ANCIENT")
        self.run_swap()
        self.assertEqual(self.read(self.backup), "OLD BUILD")

    def test_a_path_with_spaces_survives_quoting(self):
        spaced = os.path.join(self.dir, "Program Files Like")
        os.makedirs(spaced, exist_ok=True)
        self.target = os.path.join(spaced, "my app.exe")
        self.new = os.path.join(spaced, "my new.exe")
        self.backup = self.target + ".old.exe"
        with open(self.target, "w") as fh:
            fh.write("OLD BUILD")
        with open(self.new, "w") as fh:
            fh.write("NEW BUILD")
        self.run_swap()
        self.assertEqual(self.read(self.target), "NEW BUILD")

    def test_the_script_gives_up_rather_than_spinning_forever(self):
        text = write_swap_script("a", "b", "c", script_path=self.script,
                                 relaunch=False)
        body = self.read(text)
        self.assertIn("TRIES", body)
        self.assertIn("goto giveup", body)

    def test_relaunch_is_included_by_default_and_omitted_on_request(self):
        with_start = self.read(write_swap_script(
            "a", "b", "c", script_path=self.script, relaunch=True))
        without = self.read(write_swap_script(
            "a", "b", "c", script_path=self.script, relaunch=False))
        self.assertIn('start ""', with_start)
        self.assertNotIn('start ""', without)


if __name__ == "__main__":
    unittest.main(verbosity=2)
