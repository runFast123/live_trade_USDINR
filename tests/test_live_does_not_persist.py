"""Live is a decision about one session, and a file cannot make it.

Go live sets dry_run False in memory. The window saves config.json whenever a
limit is edited -- two call sites in ui.py. So pressing Go live and then
touching any box wrote "dry_run": false to disk, and the NEXT launch sent
real orders with nobody typing the confirmation and no preflight run for that
session.

Reproduced end to end before the fix. It was inert only because the account
cannot trade, which is exactly why it was cheap to fix now.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from rollover import livemode
from rollover.config import RollConfig


class Engine:
    def __init__(self):
        self.disarmed = []

    def disarm(self, why="operator"):
        self.disarmed.append(why)


class LiveCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="livefile_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "config.json")

    def config(self, **kw):
        base = dict(near_token="1769", near_expiry="2026-09-28",
                    far_token="1584", far_expiry="2026-11-26",
                    quantity_unit_confirmed=True, dry_run=True)
        base.update(kw)
        return RollConfig(**base)

    def on_disk(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def go_live(self, cfg):
        checks = livemode.preflight(cfg, None)
        for check in checks:
            check.passed = True
        refusal = livemode.go_live(cfg, Engine(), livemode.CONFIRM,
                                   checks=checks)
        self.assertIsNone(refusal)
        return cfg


class TestItIsNeverWrittenDown(LiveCase):
    def test_saving_does_not_write_dry_run_at_all(self):
        cfg = self.config()
        cfg.save(self.path)
        self.assertNotIn("dry_run", self.on_disk())

    def test_going_live_then_editing_a_limit_does_not_persist_it(self):
        """The exact sequence: Go live, then touch any editable box."""
        cfg = self.config()
        cfg.save(self.path)
        self.go_live(cfg)
        self.assertFalse(cfg.dry_run)          # live for THIS session
        cfg.save(self.path)                    # what an edit does
        self.assertNotIn("dry_run", self.on_disk())

    def test_so_the_next_launch_is_dry(self):
        cfg = self.config()
        cfg.save(self.path)
        self.go_live(cfg)
        cfg.save(self.path)
        self.assertTrue(RollConfig.load(self.path).dry_run)

    def test_everything_else_still_saves(self):
        """Excluding one key must not have excluded its neighbours."""
        cfg = self.config(lots=7)
        cfg.save(self.path)
        saved = self.on_disk()
        self.assertEqual(saved["lots"], 7)
        self.assertEqual(saved["near_token"], "1769")


class TestAFileThatAsksForLive(LiveCase):
    """Older builds wrote it, so files in the wild say live."""

    def write_live(self):
        cfg = self.config()
        cfg.save(self.path)
        raw = self.on_disk()
        raw["dry_run"] = False
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)

    def test_it_starts_in_dry_run_anyway(self):
        self.write_live()
        self.assertTrue(RollConfig.load(self.path).dry_run)

    def test_and_records_that_the_file_asked(self):
        """Silently overriding what the file says is its own kind of lie."""
        self.write_live()
        self.assertTrue(RollConfig.load(self.path).live_in_file)

    def test_an_ordinary_file_does_not_set_that_flag(self):
        self.config().save(self.path)
        self.assertFalse(RollConfig.load(self.path).live_in_file)

    def test_saving_it_back_clears_the_request_from_the_file(self):
        self.write_live()
        RollConfig.load(self.path).save(self.path)
        self.assertNotIn("dry_run", self.on_disk())

    def test_the_flag_never_reaches_the_file(self):
        self.write_live()
        RollConfig.load(self.path).save(self.path)
        self.assertNotIn("live_in_file", self.on_disk())

    def test_going_live_deliberately_still_works(self):
        """The fix must not make the real route harder."""
        self.write_live()
        cfg = RollConfig.load(self.path)
        self.go_live(cfg)
        self.assertFalse(cfg.dry_run)


class TestTheWindowSaysSo(unittest.TestCase):
    def test_the_pill_names_the_reason(self):
        import inspect

        from rollover.ui import RollWindow
        source = inspect.getsource(RollWindow._refresh_mode)
        self.assertIn("live_in_file", source)
        self.assertIn("DRY RUN (file said live)", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
