"""Adding, removing and re-legging a section.

The operator picks the contracts; nothing here decides which months to roll.
What it decides is whether a choice may be accepted, and it refuses for reasons
about the money rather than about tidiness.
"""
from __future__ import annotations

import unittest

from rollover import editing
from rollover.config import RollConfig, section_key
from rollover.editing import EditError


def row(token, desc, expiry):
    return {"Token": token, "SecDesc": desc, "Expiry": expiry}


SEP = row("1769", "USDINR26SEPFUT", "2026-09-28")
OCT = row("1500", "USDINR26OCTFUT", "2026-10-29")
NOV = row("1584", "USDINR26NOVFUT", "2026-11-26")
DEC = row("1600", "USDINR26DECFUT", "2026-12-29")


def config(**kw):
    base = dict(near_token="1769", near_expiry="2026-09-28",
                far_token="1584", far_expiry="2026-11-26",
                limit_ladder=[{"bps": "50", "qty": 20000}])
    base.update(kw)
    return RollConfig(**base)


class TestWritingDownWhatIsAlreadyThere(unittest.TestCase):
    """A file with no sections is the single-pair arrangement."""

    def test_the_existing_pair_becomes_a_section(self):
        got = editing.as_sections(config())
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["near_token"], "1769")
        self.assertEqual(got[0]["far_token"], "1584")

    def test_it_keeps_its_ladder(self):
        """Or it would inherit the newcomer's and lose its own."""
        got = editing.as_sections(config())
        self.assertEqual(got[0]["limit_ladder"], [{"bps": "50", "qty": 20000}])

    def test_it_is_named_from_its_expiries(self):
        self.assertEqual(editing.as_sections(config())[0]["name"],
                         "Sep into Nov")

    def test_explicit_sections_are_left_alone(self):
        cfg = config(sections=[{"name": "mine", "far_token": "1500",
                                "far_expiry": "2026-10-29",
                                "limit_ladder": [{"bps": "30", "qty": 1000}]}])
        self.assertEqual(editing.as_sections(cfg)[0]["name"], "mine")

    def test_a_config_with_no_contracts_has_no_sections(self):
        self.assertEqual(editing.as_sections(RollConfig()), [])


class TestAdding(unittest.TestCase):
    def test_a_second_section_is_added(self):
        got = editing.add(config(), SEP, OCT)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[1]["far_token"], "1500")

    def test_it_arrives_disabled(self):
        """It has no ladder yet, so it has no cap on what it would sell."""
        got = editing.add(config(), SEP, OCT)
        self.assertFalse(got[1]["enabled"])

    def test_the_existing_section_keeps_its_own_ladder(self):
        got = editing.add(config(), SEP, OCT)
        self.assertEqual(got[0]["limit_ladder"], [{"bps": "50", "qty": 20000}])
        self.assertEqual(got[1]["limit_ladder"], [])

    def test_it_is_named_from_the_contracts_chosen(self):
        self.assertEqual(editing.add(config(), SEP, OCT)[1]["name"],
                         "Sep into Oct")

    def test_the_first_section_of_all_is_enabled(self):
        got = editing.add(RollConfig(), SEP, NOV)
        self.assertEqual(len(got), 1)
        self.assertNotEqual(got[0].get("enabled", True), False)

    def test_a_pair_already_rolled_is_refused(self):
        with self.assertRaises(EditError) as ctx:
            editing.add(config(), SEP, NOV)
        self.assertIn("already rolls that pair", str(ctx.exception))
        self.assertIn("owned the whole campaign", str(ctx.exception))

    def test_a_far_leg_before_the_near_one_is_refused(self):
        with self.assertRaises(EditError):
            editing.add(config(), NOV, SEP)

    def test_a_missing_contract_is_refused(self):
        with self.assertRaises(EditError):
            editing.add(config(), SEP, {"Token": "", "Expiry": ""})

    def test_three_sections_are_allowed(self):
        cfg = config(sections=editing.add(config(), SEP, OCT))
        cfg.sections[1]["limit_ladder"] = [{"bps": "30", "qty": 10000}]
        cfg.sections[1]["enabled"] = True
        got = editing.add(cfg, SEP, DEC)
        self.assertEqual(len(got), 3)


class TestEnabling(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.cfg.sections = editing.add(self.cfg, SEP, OCT)
        self.key = section_key("1769", "1500")

    def test_enabling_without_a_ladder_is_refused(self):
        """That is the moment it could start selling."""
        with self.assertRaises(EditError) as ctx:
            editing.enable(self.cfg, self.key, True)
        self.assertIn("no limit_ladder", str(ctx.exception))

    def test_enabling_with_one_is_allowed(self):
        self.cfg.sections[1]["limit_ladder"] = [{"bps": "30", "qty": 10000}]
        got = editing.enable(self.cfg, self.key, True)
        self.assertTrue(got[1]["enabled"])

    def test_disabling_is_always_allowed(self):
        """It stops claiming any of the position, which cannot make it worse."""
        self.cfg.sections[1]["limit_ladder"] = [{"bps": "30", "qty": 10000}]
        self.cfg.sections = editing.enable(self.cfg, self.key, True)
        got = editing.enable(self.cfg, self.key, False)
        self.assertFalse(got[1]["enabled"])

    def test_an_unknown_section_is_refused(self):
        with self.assertRaises(EditError):
            editing.enable(self.cfg, "9999>9999", True)


class TestRemoving(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.cfg.sections = editing.add(self.cfg, SEP, OCT)

    def test_a_section_can_be_taken_out(self):
        got = editing.remove(self.cfg, section_key("1769", "1500"))
        self.assertEqual([s["far_token"] for s in got], ["1584"])

    def test_the_last_one_cannot(self):
        self.cfg.sections = editing.remove(self.cfg, section_key("1769", "1500"))
        with self.assertRaises(EditError) as ctx:
            editing.remove(self.cfg, section_key("1769", "1584"))
        self.assertIn("at least one section", str(ctx.exception))

    def test_an_unknown_section_is_refused(self):
        with self.assertRaises(EditError):
            editing.remove(self.cfg, "9999>9999")


class TestChangingTheContracts(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.cfg.sections = editing.add(self.cfg, SEP, OCT)
        self.cfg.sections[1]["limit_ladder"] = [{"bps": "30", "qty": 10000}]

    def test_a_section_can_be_pointed_at_a_different_far_month(self):
        got = editing.relegs(self.cfg, section_key("1769", "1500"), SEP, DEC)
        self.assertEqual(got[1]["far_token"], "1600")

    def test_its_ladder_comes_with_it(self):
        got = editing.relegs(self.cfg, section_key("1769", "1500"), SEP, DEC)
        self.assertEqual(got[1]["limit_ladder"], [{"bps": "30", "qty": 10000}])

    def test_its_progress_does_not_because_the_key_changed(self):
        """The state file is keyed by the pair, so a new pair starts fresh."""
        got = editing.relegs(self.cfg, section_key("1769", "1500"), SEP, DEC)
        self.assertNotEqual(section_key(got[1]["near_token"], got[1]["far_token"]),
                            section_key("1769", "1500"))

    def test_it_may_not_collide_with_another_section(self):
        with self.assertRaises(EditError) as ctx:
            editing.relegs(self.cfg, section_key("1769", "1500"), SEP, NOV)
        self.assertIn("same pair", str(ctx.exception))

    def test_the_other_sections_are_untouched(self):
        got = editing.relegs(self.cfg, section_key("1769", "1500"), SEP, DEC)
        self.assertEqual(got[0]["far_token"], "1584")


class TestEveryChangeIsValidatedWhole(unittest.TestCase):
    """A section can never be added in a state the app would refuse to start."""

    def test_an_accepted_addition_produces_a_valid_config(self):
        cfg = config()
        cfg.sections = editing.add(cfg, SEP, OCT)
        cfg.validate()

    def test_an_accepted_enable_produces_a_valid_config(self):
        cfg = config()
        cfg.sections = editing.add(cfg, SEP, OCT)
        cfg.sections[1]["limit_ladder"] = [{"bps": "30", "qty": 10000}]
        cfg.sections = editing.enable(cfg, section_key("1769", "1500"), True)
        cfg.validate()

    def test_a_refusal_changes_nothing(self):
        cfg = config()
        before = list(cfg.sections)
        with self.assertRaises(EditError):
            editing.add(cfg, SEP, NOV)
        self.assertEqual(cfg.sections, before)


class TestApplying(unittest.TestCase):
    def test_it_writes_the_file(self):
        import json
        import os
        import shutil
        import tempfile

        folder = tempfile.mkdtemp(prefix="editing_")
        try:
            path = os.path.join(folder, "config.json")
            cfg = config()
            cfg.save(path)
            editing.apply(cfg, editing.add(cfg, SEP, OCT), path)

            with open(path, encoding="utf-8") as fh:
                self.assertEqual(len(json.load(fh)["sections"]), 2)
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_without_a_path_it_only_changes_the_running_config(self):
        cfg = config()
        editing.apply(cfg, editing.add(cfg, SEP, OCT))
        self.assertEqual(len(cfg.sections), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
