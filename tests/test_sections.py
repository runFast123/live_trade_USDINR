"""Several rolls configured as sections of one file.

A section states only what differs from the settings above it, which is how
"both sections sell the same September" gets said once rather than twice.

The config it trades on is DERIVED on every read and never stored. That is not
tidiness: settings above change while the app runs, `dry_run` above all, and a
section holding a copy taken at startup would go on reporting the old value.
A section that still believed it was in dry run while the engine sent real
orders would report the margin gate as "not checked in dry run" -- which is to
say it would pass, and live orders would go out with the margin check silently
disabled.
"""
from __future__ import annotations

import unittest

from rollover.config import (SECTION_META, SECTION_OVERRIDES, ConfigError,
                             RollConfig, section_key)

SEP_OCT = {"name": "Sep into Oct", "far_token": "1500",
           "far_expiry": "2026-10-29",
           "limit_ladder": [{"bps": "30", "qty": 10000}]}
SEP_NOV = {"name": "Sep into Nov", "far_token": "1584",
           "far_expiry": "2026-11-26",
           "limit_ladder": [{"bps": "50", "qty": 20000}]}


def config(**kw):
    base = dict(near_token="1769", near_expiry="2026-09-28",
                far_token="1584", far_expiry="2026-11-26")
    base.update(kw)
    return RollConfig(**base)


class TestNoSectionsIsTodaysApp(unittest.TestCase):
    """An existing config.json must keep working byte for byte."""

    def test_one_section_is_derived_from_the_settings_above(self):
        specs = config().section_specs()
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].cfg.near_token, "1769")
        self.assertEqual(specs[0].cfg.far_token, "1584")

    def test_it_is_the_very_same_config_object(self):
        """Nothing is copied, so nothing can drift from it."""
        cfg = config()
        self.assertIs(cfg.section_specs()[0].cfg, cfg)

    def test_it_is_enabled_and_named_from_the_expiries(self):
        spec = config().section_specs()[0]
        self.assertTrue(spec.enabled)
        self.assertEqual(spec.name, "Sep into Nov")

    def test_an_unnameable_pair_still_gets_a_section(self):
        spec = config(near_expiry="", far_expiry="").section_specs()[0]
        self.assertEqual(len(config(near_expiry="").section_specs()), 1)
        self.assertEqual(spec.key, section_key("1769", "1584"))

    def test_the_default_config_has_none(self):
        self.assertEqual(RollConfig().sections, [])


class TestASectionIsASparseOverride(unittest.TestCase):
    def setUp(self):
        self.cfg = config(lots=1, sections=[SEP_OCT, dict(SEP_NOV, lots=2)])
        self.specs = self.cfg.section_specs()

    def test_the_near_leg_is_stated_once_and_inherited(self):
        self.assertEqual({s.cfg.near_token for s in self.specs}, {"1769"})

    def test_each_section_keeps_its_own_far_leg(self):
        self.assertEqual([s.cfg.far_token for s in self.specs], ["1500", "1584"])

    def test_each_section_keeps_its_own_ladder(self):
        self.assertEqual([s.cfg.limit_ladder[0]["bps"] for s in self.specs],
                         ["30", "50"])

    def test_an_override_it_does_not_state_is_inherited(self):
        self.assertEqual(self.specs[0].cfg.lots, 1)

    def test_an_override_it_does_state_applies(self):
        self.assertEqual(self.specs[1].cfg.lots, 2)
        self.assertEqual(self.specs[1].cfg.clip_qty, 2000)

    def test_a_section_config_has_no_sections_of_its_own(self):
        """One level only; a section cannot contain sections."""
        self.assertEqual(self.specs[0].cfg.sections, [])

    def test_the_key_identifies_the_pair(self):
        self.assertEqual([s.key for s in self.specs],
                         ["1769>1500", "1769>1584"])

    def test_the_name_falls_back_to_the_expiries(self):
        specs = config(sections=[{"far_token": "1500",
                                  "far_expiry": "2026-10-29",
                                  "limit_ladder": [{"bps": "30", "qty": 1000}]},
                                 SEP_NOV]).section_specs()
        self.assertEqual(specs[0].name, "Sep into Oct")

    def test_a_section_can_be_disabled(self):
        specs = config(sections=[SEP_OCT, dict(SEP_NOV, enabled=False)]).section_specs()
        self.assertTrue(specs[0].enabled)
        self.assertFalse(specs[1].enabled)


class TestNothingIsStored(unittest.TestCase):
    """The trap: a stale copy would disable the margin gate on a live order."""

    def setUp(self):
        self.cfg = config(dry_run=True, sections=[SEP_OCT, SEP_NOV])

    def test_dry_run_follows_the_parent(self):
        self.assertTrue(all(s.cfg.dry_run for s in self.cfg.section_specs()))
        self.cfg.dry_run = False                 # what livemode.go_live does
        self.assertFalse(any(s.cfg.dry_run for s in self.cfg.section_specs()))

    def test_a_spec_taken_earlier_is_not_reused(self):
        before = self.cfg.section_specs()
        self.cfg.dry_run = False
        after = self.cfg.section_specs()
        self.assertTrue(before[0].cfg.dry_run, "the old spec is a snapshot")
        self.assertFalse(after[0].cfg.dry_run, "a fresh read sees the change")

    def test_other_shared_settings_follow_too(self):
        self.cfg.require_margin = False
        self.assertFalse(any(s.cfg.require_margin
                             for s in self.cfg.section_specs()))

    def test_deriving_is_cheap_enough_to_do_every_tick(self):
        import time
        started = time.perf_counter()
        for _ in range(200):
            self.cfg.section_specs()
        each = (time.perf_counter() - started) / 200
        self.assertLess(each, 0.01, f"{each * 1000:.2f}ms a tick is too slow")


class TestTheLadderHole(unittest.TestCase):
    """A section with no ladder has no cap, and summed to nothing.

    It rolls a clip at a time for as long as the market allows while
    contributing zero to the total checked against the position, so two
    sections would fit inside the position on paper while one of them
    consumed all of it. The shipped config.json has no limit_ladder key at
    all, so two sections inheriting it would both be uncapped.
    """

    def refusal(self, **kw):
        try:
            config(**kw).validate()
            return None
        except ConfigError as exc:
            return str(exc)

    def test_two_sections_with_no_ladder_anywhere_are_refused(self):
        why = self.refusal(sections=[
            {"name": "A", "far_token": "1500", "far_expiry": "2026-10-29"},
            {"name": "B", "far_token": "1584", "far_expiry": "2026-11-26"}])
        self.assertIsNotNone(why)
        self.assertIn("no limit_ladder", why)

    def test_one_laddered_and_one_not_is_still_refused(self):
        why = self.refusal(sections=[
            SEP_OCT,
            {"name": "B", "far_token": "1584", "far_expiry": "2026-11-26"}])
        self.assertIn("B has no limit_ladder", why)

    def test_the_refusal_says_why_it_matters(self):
        why = self.refusal(sections=[
            SEP_OCT,
            {"name": "B", "far_token": "1584", "far_expiry": "2026-11-26"}])
        self.assertIn("no cap", why)
        self.assertIn("fit inside the position", why)

    def test_a_disabled_section_without_one_is_fine(self):
        self.assertIsNone(self.refusal(sections=[
            SEP_OCT,
            {"name": "B", "far_token": "1584", "far_expiry": "2026-11-26",
             "enabled": False}]))

    def test_inheriting_a_ladder_from_above_counts(self):
        self.assertIsNone(self.refusal(
            limit_ladder=[{"bps": "50", "qty": 20000}],
            sections=[SEP_OCT, {"name": "B", "far_token": "1584",
                                "far_expiry": "2026-11-26"}]))

    def test_one_section_without_a_ladder_is_the_original_arrangement(self):
        """Single-pair rolling has always been bounded by the position gate."""
        self.assertIsNone(self.refusal(sections=[]))
        self.assertIsNone(self.refusal(sections=[
            {"name": "only", "far_token": "1584", "far_expiry": "2026-11-26"}]))


class TestWhatASectionMayNotSet(unittest.TestCase):
    """Strict, unlike the top level, because a section is hand-written."""

    def refusal(self, section):
        try:
            config(sections=[dict(SEP_OCT, **section), SEP_NOV]).validate()
            return None
        except ConfigError as exc:
            return str(exc)

    def test_dry_run_is_not_a_section_setting(self):
        """It would be a way to send live orders from a window saying DRY RUN."""
        why = self.refusal({"dry_run": True})
        self.assertIn("dry_run cannot be set per section", why)

    def test_neither_is_the_tick_or_the_window(self):
        for key in ("tick", "window_open", "validity", "product_type",
                    "require_margin", "price_band_low"):
            self.assertIsNotNone(self.refusal({key: "x"}), msg=key)

    def test_a_typo_is_refused_rather_than_ignored(self):
        why = self.refusal({"limit_ladders": []})
        self.assertIn("limit_ladders", why)

    def test_the_refusal_lists_what_is_allowed(self):
        why = self.refusal({"nonsense": 1})
        for allowed in ("far_token", "limit_ladder", "lots"):
            self.assertIn(allowed, why)

    def test_every_override_is_actually_a_config_field(self):
        fields = set(RollConfig.__dataclass_fields__)
        for name in SECTION_OVERRIDES:
            self.assertIn(name, fields, msg=name)

    def test_the_meta_keys_are_not_config_fields(self):
        fields = set(RollConfig.__dataclass_fields__)
        for name in SECTION_META:
            self.assertNotIn(name, fields, msg=name)


class TestOtherRefusals(unittest.TestCase):
    def refusal(self, **kw):
        try:
            config(**kw).validate()
            return None
        except ConfigError as exc:
            return str(exc)

    def test_two_sections_on_the_same_pair(self):
        why = self.refusal(sections=[SEP_NOV, dict(SEP_NOV, name="again")])
        self.assertIn("same pair", why)

    def test_a_section_that_is_not_an_object(self):
        self.assertIsNotNone(self.refusal(sections=["not an object", SEP_NOV]))

    def test_a_section_carrying_an_invalid_value(self):
        why = self.refusal(sections=[dict(SEP_OCT, lots=0), SEP_NOV])
        self.assertIn("lots", why)

    def test_a_section_with_an_off_grid_ladder(self):
        why = self.refusal(sections=[
            dict(SEP_OCT, limit_ladder=[{"bps": "30", "qty": 1500}]), SEP_NOV])
        self.assertIn("lot size", why)

    def test_leg_order_must_be_one_of_three(self):
        self.assertIsNotNone(self.refusal(leg_order="sideways"))
        for mode in ("auto", "near_first", "far_first"):
            self.assertIsNone(self.refusal(leg_order=mode), msg=mode)

    def test_the_depth_multiple_must_be_at_least_one(self):
        self.assertIsNotNone(self.refusal(far_first_near_depth_multiple=0))
        self.assertIsNone(self.refusal(far_first_near_depth_multiple=3))

    def test_the_account_clip_cap_is_a_real_number_or_nothing(self):
        self.assertIsNotNone(self.refusal(max_clips_per_day_account=0))
        self.assertIsNone(self.refusal(max_clips_per_day_account=None))
        self.assertIsNone(self.refusal(max_clips_per_day_account=4))


class TestItRoundTrips(unittest.TestCase):
    def test_sections_survive_save_and_load(self):
        import json
        import os
        import shutil
        import tempfile

        folder = tempfile.mkdtemp(prefix="sections_")
        try:
            path = os.path.join(folder, "config.json")
            cfg = config(sections=[SEP_OCT, SEP_NOV], leg_order="far_first",
                         max_clips_per_day_account=4)
            cfg.save(path)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(len(json.load(fh)["sections"]), 2)

            back = RollConfig.load(path)
            self.assertEqual(len(back.section_specs()), 2)
            self.assertEqual(back.leg_order, "far_first")
            self.assertEqual(back.max_clips_per_day_account, 4)
        finally:
            shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
