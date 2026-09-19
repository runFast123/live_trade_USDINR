"""Tests for the update check. No network: every response is a fixture."""
from __future__ import annotations

import unittest

from rollover import __version__, updater
from rollover.updater import Release, UpdateError, is_newer, parse_version

# A release that is always newer than this build, whatever version it reaches.
NEWER = f"v{parse_version(__version__)[0] + 1}.0.0"


class TestVersionCompare(unittest.TestCase):
    def test_plain_versions(self):
        self.assertEqual(parse_version("1.2.3"), (1, 2, 3))
        self.assertEqual(parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(parse_version("1.2"), (1, 2, 0))
        self.assertEqual(parse_version("2"), (2, 0, 0))

    def test_prerelease_and_build_suffixes_are_ignored(self):
        self.assertEqual(parse_version("1.2.3-beta.1"), (1, 2, 3))
        self.assertEqual(parse_version("1.2.3+build9"), (1, 2, 3))

    def test_rubbish_does_not_explode(self):
        self.assertEqual(parse_version(""), (0, 0, 0))
        self.assertEqual(parse_version(None), (0, 0, 0))
        self.assertEqual(parse_version("not-a-version"), (0, 0, 0))

    def test_newer_and_older(self):
        self.assertTrue(is_newer("1.0.1", "1.0.0"))
        self.assertTrue(is_newer("1.1.0", "1.0.9"))
        self.assertTrue(is_newer("2.0.0", "1.9.9"))
        self.assertFalse(is_newer("1.0.0", "1.0.0"))
        self.assertFalse(is_newer("0.9.9", "1.0.0"))

    def test_ten_beats_nine_rather_than_sorting_as_text(self):
        self.assertTrue(is_newer("1.10.0", "1.9.0"))


class TestCheck(unittest.TestCase):
    """check() reads the GitHub payload. The HTTP call is stubbed out."""

    def setUp(self):
        self._real_get = updater._get
        self.notes = "Fixes the thing.\n\nSHA256: " + "a" * 64

    def tearDown(self):
        updater._get = self._real_get

    def payload(self, tag=None, assets=None, body=None):
        # Derived from this build rather than written down. A fixed "newer"
        # version stops being newer the moment the app reaches it, which is
        # exactly what happened at 2.0.0: nine of these turned red on a version
        # bump that had changed nothing they test.
        tag = tag or NEWER
        if assets is None:
            assets = [{"name": "roll_app.exe", "size": 123,
                       "browser_download_url": "https://example.invalid/roll_app.exe"}]
        return {
            "tag_name": tag,
            "assets": assets,
            "body": self.notes if body is None else body,
            "html_url": "https://github.com/x/y/releases/tag/" + tag,
        }

    def stub(self, payload, sha_text=None):
        import json

        def fake_get(url, accept="application/vnd.github+json"):
            if url.endswith(".sha256"):
                return (sha_text or "").encode()
            return json.dumps(payload).encode()

        updater._get = fake_get

    def test_a_newer_release_is_reported(self):
        self.stub(self.payload())
        release = updater.check("owner/repo")
        self.assertIsNotNone(release)
        self.assertEqual(release.version, NEWER.lstrip("v"))
        self.assertEqual(release.sha256, "a" * 64)

    def test_the_same_version_is_not_an_update(self):
        self.stub(self.payload(tag="v" + updater.__version__))
        self.assertIsNone(updater.check("owner/repo"))

    def test_an_older_version_is_not_an_update(self):
        self.stub(self.payload(tag="v0.0.1"))
        self.assertIsNone(updater.check("owner/repo"))

    def test_a_release_with_no_exe_is_ignored(self):
        self.stub(self.payload(assets=[{"name": "notes.txt", "size": 1,
                                        "browser_download_url": "x"}]))
        self.assertIsNone(updater.check("owner/repo"))

    def test_the_checksum_asset_wins_over_the_notes(self):
        assets = [
            {"name": "roll_app.exe", "size": 5, "browser_download_url": "u"},
            {"name": "roll_app.exe.sha256", "size": 5,
             "browser_download_url": "https://example.invalid/x.sha256"},
        ]
        self.stub(self.payload(assets=assets), sha_text="b" * 64 + "  roll_app.exe")
        self.assertEqual(updater.check("owner/repo").sha256, "b" * 64)

    def test_a_network_failure_is_not_fatal(self):
        def boom(url, accept=None):
            raise OSError("no network")
        updater._get = boom
        self.assertIsNone(updater.check("owner/repo"))


class TestTwoExecutablesInOneRelease(TestCheck):
    """The release ships the window and the console tool side by side.

    Before this, the updater took the first asset ending in .exe and the first
    64-hex string in the checksum file. Both were correct only by accident of
    ordering, and getting either wrong would overwrite the app with the console
    build, or verify it against the wrong file's hash.
    """

    BOTH = [
        {"name": "roll_cli.exe", "size": 7,
         "browser_download_url": "https://example.invalid/roll_cli.exe"},
        {"name": "roll_app.exe", "size": 5,
         "browser_download_url": "https://example.invalid/roll_app.exe"},
        {"name": "roll_app.exe.sha256", "size": 5,
         "browser_download_url": "https://example.invalid/x.sha256"},
    ]
    SUMS = ("c" * 64 + "  roll_app.exe\n") + ("d" * 64 + "  roll_cli.exe\n")

    def test_the_window_updates_itself_not_the_console_tool(self):
        self.stub(self.payload(assets=self.BOTH), sha_text=self.SUMS)
        release = updater.check("owner/repo", exe_name="roll_app.exe")
        self.assertTrue(release.url.endswith("roll_app.exe"))
        self.assertEqual(release.sha256, "c" * 64)

    def test_the_console_tool_updates_itself(self):
        self.stub(self.payload(assets=self.BOTH), sha_text=self.SUMS)
        release = updater.check("owner/repo", exe_name="roll_cli.exe")
        self.assertTrue(release.url.endswith("roll_cli.exe"))
        self.assertEqual(release.sha256, "d" * 64)

    def test_asset_order_does_not_decide_it(self):
        """roll_cli is listed first; the window must still pick its own."""
        self.assertEqual(self.BOTH[0]["name"], "roll_cli.exe")
        self.stub(self.payload(assets=self.BOTH), sha_text=self.SUMS)
        self.assertTrue(updater.check("owner/repo", exe_name="roll_app.exe")
                        .url.endswith("roll_app.exe"))

    def test_an_older_release_with_only_the_window_still_updates(self):
        """Releases before this change carry one exe. Do not strand them."""
        assets = [{"name": "roll_app.exe", "size": 5,
                   "browser_download_url": "https://example.invalid/roll_app.exe"}]
        self.stub(self.payload(assets=assets))
        release = updater.check("owner/repo", exe_name="roll_cli.exe")
        self.assertIsNotNone(release)
        self.assertTrue(release.url.endswith("roll_app.exe"))

    def test_an_old_single_line_checksum_is_still_read(self):
        assets = [
            {"name": "roll_app.exe", "size": 5, "browser_download_url": "u"},
            {"name": "roll_app.exe.sha256", "size": 5,
             "browser_download_url": "https://example.invalid/x.sha256"},
        ]
        self.stub(self.payload(assets=assets), sha_text="e" * 64 + "  build.exe")
        self.assertEqual(updater.check("owner/repo").sha256, "e" * 64)

    def test_a_bare_hash_in_the_notes_is_still_read(self):
        self.stub(self.payload())
        self.assertEqual(updater.check("owner/repo").sha256, "a" * 64)

    def test_two_bare_hashes_in_the_notes_verify_nothing(self):
        """Ambiguous is not the same as correct. Refuse rather than guess."""
        notes = "SHA256 " + "a" * 64 + " and " + "b" * 64
        self.stub(self.payload(body=notes))
        self.assertIsNone(updater.check("owner/repo").sha256)

    def test_a_named_line_in_the_notes_beats_position(self):
        notes = ("f" * 64 + "  roll_cli.exe\n" + "0" * 64 + "  roll_app.exe\n")
        self.stub(self.payload(body=notes))
        self.assertEqual(updater.check("owner/repo", exe_name="roll_app.exe")
                         .sha256, "0" * 64)

    def test_the_default_is_the_window_when_not_frozen(self):
        self.assertEqual(updater.running_exe_name(), "roll_app.exe")


class TestDownloadRefusals(unittest.TestCase):
    def test_a_release_without_a_checksum_is_refused(self):
        release = Release(version="2.0.0", url="https://example.invalid/a.exe",
                          size=10, notes="", sha256=None,
                          html_url="https://example.invalid/r")
        with self.assertRaises(UpdateError) as ctx:
            updater.download(release)
        self.assertIn("SHA256", str(ctx.exception))

    def test_installing_from_source_is_refused(self):
        ok, why = updater.can_install()
        self.assertFalse(ok)          # the test run is never a frozen build
        self.assertIn("from source", why)


if __name__ == "__main__":
    unittest.main(verbosity=2)
