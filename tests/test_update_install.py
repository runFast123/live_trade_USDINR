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
import time
import unittest

from rollover import updater
from rollover.updater import (Release, UpdateError, stage_update,
                              write_swap_script)


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


class TestTheProcessActuallyExits(unittest.TestCase):
    """The bug that stopped updates working.

    install_and_restart ended with sys.exit(0), but it ran on the download
    worker thread. sys.exit there raises SystemExit in that thread alone and
    leaves the process running, so the executable was never released, the swap
    script waited for a lock that never cleared, and the download was orphaned
    in temp. The log said "Restarting" and nothing restarted.

    Proved against the real build: while it was running the swap was blocked,
    and it completed the moment the process exited.
    """

    def test_sys_exit_on_a_worker_thread_does_not_end_the_process(self):
        import sys as _sys
        import threading

        outcome = []

        def worker():
            try:
                _sys.exit(0)
            except SystemExit:
                outcome.append("only this thread died")

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(outcome, ["only this thread died"])
        # If sys.exit had worked, this line would never run.
        self.assertTrue(True)

    @staticmethod
    def code_of(function) -> str:
        """The function's body without its docstring.

        The docstrings here deliberately name sys.exit while explaining why it
        must not be used, so matching on raw source would flag the explanation
        as if it were the mistake.
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        node = tree.body[0]
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        return " ; ".join(ast.unparse(stmt) for stmt in body)

    def test_staging_does_not_try_to_end_the_process(self):
        source = self.code_of(stage_update)
        for forbidden in ("sys.exit", "os._exit", "quit_now"):
            self.assertNotIn(forbidden, source,
                             f"{forbidden} in stage_update would run on whichever "
                             "thread called it")

    def test_the_old_footgun_is_gone(self):
        self.assertFalse(hasattr(updater, "install_and_restart"),
                         "install_and_restart bundled staging and exiting, which "
                         "is what made it easy to call from the wrong thread")

    def test_the_download_worker_hands_back_rather_than_exiting(self):
        from rollover.ui import RollWindow

        source = self.code_of(RollWindow._do_update)
        # ast.unparse normalises quoting, so match its form.
        self.assertIn("'ready'", source,
                      "the worker must hand the file to the main thread")
        self.assertIn("_updates.put", source)
        for forbidden in ("stage_update", "quit_now", "sys.exit"):
            self.assertNotIn(forbidden, source,
                             f"{forbidden} must not run on the download thread")

    def test_the_install_step_runs_on_the_main_thread_and_exits(self):
        from rollover.ui import RollWindow

        source = self.code_of(RollWindow._install_now)
        self.assertIn("stage_update", source)
        self.assertIn("quit_now", source)

    def test_quit_now_uses_a_hard_exit(self):
        source = self.code_of(updater.quit_now)
        self.assertIn("os._exit", source,
                      "a lingering thread would keep the executable locked")


class TestStaleDownloadCleanup(unittest.TestCase):
    """Each failed attempt left a whole build behind: four of them, 145 MB."""

    def setUp(self):
        self.made = []

    def tearDown(self):
        for path in self.made:
            try:
                os.remove(path)
            except OSError:
                pass

    def make(self, age_seconds):
        handle, path = tempfile.mkstemp(suffix=".exe", prefix="roll_app_update_")
        os.close(handle)
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        self.made.append(path)
        return path

    def test_an_old_download_is_removed(self):
        old = self.make(7200)
        updater.cleanup_stale_downloads(older_than_seconds=3600)
        self.assertFalse(os.path.exists(old))

    def test_a_download_in_progress_is_left_alone(self):
        fresh = self.make(30)
        updater.cleanup_stale_downloads(older_than_seconds=3600)
        self.assertTrue(os.path.exists(fresh))

    def test_it_counts_what_it_removed(self):
        self.make(7200)
        self.make(7200)
        self.assertGreaterEqual(
            updater.cleanup_stale_downloads(older_than_seconds=3600), 2)

    def test_unrelated_files_are_not_touched(self):
        handle, path = tempfile.mkstemp(suffix=".exe", prefix="something_else_")
        os.close(handle)
        stamp = time.time() - 99999
        os.utime(path, (stamp, stamp))
        self.made.append(path)
        updater.cleanup_stale_downloads(older_than_seconds=3600)
        self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
