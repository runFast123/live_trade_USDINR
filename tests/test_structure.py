"""Two guards that read the source, because a test cannot cover every path.

Both of these are properties of the *shape* of the code rather than of any one
run through it, and both are the kind of thing a later edit breaks silently.

The first is the rule that half a roll stops the account, not one section. The
second is the promise the whole sections design rests on: that a Section reads
exactly like the session object the gates were written against, so not a line
of gate code had to change.
"""
from __future__ import annotations

import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def parse(relative: str) -> ast.Module:
    with open(os.path.join(ROOT, relative), encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=relative)


def function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is gone; this guard needs rewriting")


class TestEveryHaltFromExecuteIsAnAccountHalt(unittest.TestCase):
    """A half rolled position is an account-level fact.

    Every section sells the same September into the same uncertainty. If a
    second leg fails and only the section that sent it stopped, its siblings
    would carry on selling that same September against a position nobody can
    account for. So nothing on the execution path may set a section's own halt
    -- it must go through engine.halt(), which stops all of them.

    Checked in the source rather than by exercising every failure, because the
    failures that matter are the ones nobody thought to write a test for.
    """

    def setUp(self):
        self.tree = parse(os.path.join("rollover", "engine.py"))

    def execution_path(self):
        """_execute and everything it calls that could stop the engine."""
        names = ["_execute", "_handle_second_leg_failure", "_count_clip",
                 "_credit_rung", "_confirm_trades"]
        return [(name, function(self.tree, name)) for name in names]

    def test_no_section_halt_is_set_on_the_execution_path(self):
        for name, node in self.execution_path():
            for inner in ast.walk(node):
                if isinstance(inner, ast.Attribute) and \
                        isinstance(inner.ctx, ast.Store) and \
                        inner.attr in ("own_halt", "halted_reason"):
                    self.fail(f"{name} line {inner.lineno} sets "
                              f"{ast.unparse(inner)} directly. A failure "
                              "during execution must halt the account, not "
                              "one section: use self.halt().")

    def test_the_execution_path_does_have_a_way_to_halt(self):
        """Otherwise the guard above passes for the wrong reason."""
        found = set()
        for name, node in self.execution_path():
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and \
                        isinstance(inner.func, ast.Attribute) and \
                        inner.func.attr == "halt" and \
                        isinstance(inner.func.value, ast.Name) and \
                        inner.func.value.id == "self":
                    found.add(name)
        self.assertIn("_execute", found)

    def test_halt_writes_the_account_not_a_section(self):
        node = function(self.tree, "halt")
        written = {ast.unparse(inner) for inner in ast.walk(node)
                   if isinstance(inner, ast.Attribute)
                   and isinstance(inner.ctx, ast.Store)}
        self.assertIn("self.account.halted_reason", written)
        self.assertNotIn("section.own_halt", written)

    def test_clearing_a_halt_clears_both(self):
        """A halt cleared on the account while a section kept its own would
        leave that section stopped for a reason nobody could see."""
        node = function(self.tree, "clear_halt")
        written = {ast.unparse(inner) for inner in ast.walk(node)
                   if isinstance(inner, ast.Attribute)
                   and isinstance(inner.ctx, ast.Store)}
        self.assertIn("self.account.halted_reason", written)
        self.assertIn("section.own_halt", written)


class TestASectionReadsLikeTheOldSession(unittest.TestCase):
    """The gates were written against one session object and never changed.

    That only holds while a Section answers to every name the gates ask for.
    A gate reaching for an attribute a Section has not got does not fail
    politely -- gates.evaluate raises, the tick dies, and the screen shows the
    last thing it managed to say while nothing is being watched at all.
    """

    def gate_reads(self):
        """Every attribute gates.evaluate asks of its session argument."""
        node = function(parse(os.path.join("rollover", "gates.py")), "evaluate")
        args = [a.arg for a in node.args.args]
        self.assertIn("session", args)
        return {inner.attr for inner in ast.walk(node)
                if isinstance(inner, ast.Attribute)
                and isinstance(inner.value, ast.Name)
                and inner.value.id == "session"}

    def a_section(self):
        from rollover.config import RollConfig
        from rollover.sections import AccountState, Section

        cfg = RollConfig(near_token="1769", near_expiry="2026-09-28",
                         far_token="1584", far_expiry="2026-11-26")
        return Section(cfg.section_specs()[0], AccountState(), cfg)

    def test_the_gates_ask_for_something(self):
        """A guard that asserts nothing is worse than no guard."""
        self.assertGreaterEqual(len(self.gate_reads()), 8)

    def test_a_section_answers_to_every_one(self):
        section = self.a_section()
        missing = sorted(name for name in self.gate_reads()
                         if not hasattr(section, name))
        self.assertEqual(missing, [], f"the gates read {missing} from the "
                                      "session; a Section has not got it")

    def test_the_account_wide_ones_come_from_the_account(self):
        """in_flight and the halt are per account, not per roll: one order at
        a time across every section, and a halt stops all of them."""
        section = self.a_section()
        section.account.in_flight = True
        self.assertTrue(section.in_flight)
        section.account.halted_reason = "something went wrong"
        self.assertEqual(section.halted_reason, "something went wrong")

    def test_an_account_halt_beats_a_section_one(self):
        section = self.a_section()
        section.own_halt = "mine"
        section.account.halted_reason = "everyone's"
        self.assertEqual(section.halted_reason, "everyone's")


if __name__ == "__main__":
    unittest.main(verbosity=2)
