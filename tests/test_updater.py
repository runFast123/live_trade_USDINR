"""Tests for the update check. No network: every response is a fixture."""
from __future__ import annotations

import unittest

from rollover import updater
from rollover.updater import Release, UpdateError, is_newer, parse_version


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

    def payload(self, tag="v2.0.0", assets=None, body=None):
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
        self.assertEqual(release.version, "2.0.0")
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
