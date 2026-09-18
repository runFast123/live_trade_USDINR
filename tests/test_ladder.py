"""Several limits at once, each with its own quantity.

The behaviour worth pinning hardest is which rung gets credited when more than
one qualifies, because it is the only part of this that is not obvious and it
decides how much of the campaign stays reachable.
"""
from __future__ import annotations

import datetime as dt
import shutil
import tempfile
import unittest
from datetime import date

from rollover import rule
from rollover.config import ConfigError, RollConfig
from rollover.ladder import (Ladder, LadderError, Rung, campaign_key, parse)
from rollover.money import D
from rollover.quotes import Quote

NOW = dt.datetime(2026, 9, 18, 11, 0, 0)


def quote(token, bid, ask):
    return Quote(token=token, bid=D(bid), ask=D(ask), at=NOW, divisor=D(1),
                 bid_qty=500, ask_qty=500)


def two_rungs():
    return parse([{"bps": "30", "qty": 10000}, {"bps": "50", "qty": 20000}],
                 lot_size=1000)


def config(**kw):
    kw.setdefault("near_token", "1769")
    kw.setdefault("far_token", "1584")
    kw.setdefault("limit_ladder", [{"bps": "30", "qty": 10000},
                                   {"bps": "50", "qty": 20000}])
    return RollConfig(**kw)


def decide(far_ask, progress=None, cfg=None, ladder=None, days=59):
    cfg = cfg or config()
    return rule.compute(
        quote("1769", "95.7800", "95.7825"),
        quote("1584", D(far_ask) - D("0.0025"), far_ask),
        cfg, days=days, ladder=ladder or two_rungs(), progress=progress or {})


class TestParsing(unittest.TestCase):
    def test_rungs_come_back_cheapest_first(self):
        built = parse([{"bps": "50", "qty": 20000}, {"bps": "30", "qty": 10000}])
        self.assertEqual([r.bps for r in built.rungs], [D("30"), D("50")])

    def test_the_total_is_the_sum_not_the_largest(self):
        """Each rung is its own allocation. 10,000 and 20,000 is 30,000."""
        self.assertEqual(two_rungs().total_qty, 30000)

    def test_nothing_configured_is_not_an_error(self):
        for empty in (None, [], "", {}):
            self.assertFalse(parse(empty))

    def test_a_quantity_off_the_lot_grid_is_refused(self):
        """The exchange would refuse it, so refuse it here where it is visible."""
        with self.assertRaises(LadderError) as ctx:
            parse([{"bps": "30", "qty": 10500}], lot_size=1000)
        self.assertIn("multiple of the lot size", str(ctx.exception))

    def test_a_duplicate_limit_is_refused(self):
        with self.assertRaises(LadderError) as ctx:
            parse([{"bps": "30", "qty": 1000}, {"bps": "30", "qty": 2000}])
        self.assertIn("twice", str(ctx.exception))

    def test_a_rung_looser_than_the_tenor_limit_is_refused(self):
        """A ladder spends within the client's limit. It may not raise it."""
        with self.assertRaises(LadderError) as ctx:
            parse([{"bps": "60", "qty": 1000}], ceiling_bps=D("50"))
        self.assertIn("looser", str(ctx.exception))
        self.assertIn("limit_bps_schedule", str(ctx.exception))

    def test_a_rung_at_exactly_the_ceiling_is_allowed(self):
        self.assertTrue(parse([{"bps": "50", "qty": 1000}], ceiling_bps=D("50")))

    def test_rubbish_is_refused_rather_than_skipped(self):
        """Dropping a bad rung would silently roll less than was asked for."""
        for bad in ([{"bps": "x", "qty": 1000}],
                    [{"bps": "30", "qty": "many"}],
                    [{"bps": "30"}],
                    [{"qty": 1000}],
                    [{"bps": "0", "qty": 1000}],
                    [{"bps": "30", "qty": 0}],
                    [{"bps": "30", "qty": -1000}],
                    ["not an object"],
                    "not a list"):
            with self.assertRaises(LadderError, msg=bad):
                parse(bad)


class TestPricingTheRungs(unittest.TestCase):
    def setUp(self):
        self.views = two_rungs().evaluate(
            roll_cost=D("0.2700"), reference=D("95.78"), near_bid=D("95.7800"))

    def test_each_rung_becomes_a_rupee_limit(self):
        self.assertEqual(self.views[0].limit_rupees, D("0.2873"))
        self.assertEqual(self.views[1].limit_rupees, D("0.4789"))

    def test_each_rung_becomes_a_far_ask_to_watch_for(self):
        """The number an operator actually wants: the price that would do it."""
        self.assertEqual(self.views[0].required_far_ask, D("96.0673"))
        self.assertEqual(self.views[1].required_far_ask, D("96.2589"))

    def test_the_distance_is_signed(self):
        """Negative means already through it."""
        self.assertLess(self.views[0].distance_bps, 0)

    def test_a_rung_not_yet_reached_reads_positive(self):
        views = two_rungs().evaluate(D("0.4000"), D("95.78"), D("95.7800"))
        self.assertGreater(views[0].distance_bps, 0)
        self.assertLess(views[1].distance_bps, 0)

    def test_without_a_price_nothing_is_invented(self):
        views = two_rungs().evaluate(None, None, None)
        self.assertTrue(all(v.limit_rupees is None for v in views))
        self.assertTrue(all(not v.qualifies for v in views))
        self.assertEqual(views[0].status, "no price")


class TestWhichRungIsWorked(unittest.TestCase):
    """The decision that decides how much of the campaign stays reachable."""

    def test_the_cheapest_qualifying_rung_is_worked_first(self):
        decision = decide("96.0500")
        self.assertEqual(decision.active_rung.rung.bps, D("30"))

    def test_the_next_rung_takes_over_when_the_first_is_spent(self):
        decision = decide("96.0500", progress={"30": 10000})
        self.assertEqual(decision.active_rung.rung.bps, D("50"))

    def test_a_price_only_the_loose_rung_reaches_uses_that_one(self):
        decision = decide("96.1800")
        self.assertEqual(decision.active_rung.rung.bps, D("50"))
        self.assertTrue(decision.qualifies)

    def test_working_cheap_first_keeps_more_of_the_campaign_reachable(self):
        """This is why cheapest-first, stated as the outcome it protects.

        Roll the same 10,000 either way, at the same price, then ask what a
        later and worse market could still do. Spending the loose rung early
        means the rest of the campaign needs the tight limit for the rest of
        its life.
        """
        ladder = two_rungs()

        def reachable_at(bps, progress):
            """Quantity a market at this cost could still roll."""
            views = ladder.evaluate(None, None, None, progress)
            return sum(v.remaining for v in views if v.rung.bps > bps)

        cheap_first = ladder.credit({}, ladder.rungs[0], 10000)
        loose_first = ladder.credit({}, ladder.rungs[1], 10000)

        self.assertEqual(ladder.done_total(cheap_first),
                         ladder.done_total(loose_first), "not a fair comparison")

        # A later market at 41.8 bps: only the 50 rung can take it.
        self.assertEqual(reachable_at(D("41.8"), cheap_first), 20000)
        self.assertEqual(reachable_at(D("41.8"), loose_first), 10000)

    def test_spending_the_loose_rung_first_can_strand_the_rest(self):
        ladder = two_rungs()
        spent = ladder.credit({}, ladder.rungs[1], 20000)     # all of the 50 rung
        stuck = decide("96.1800", progress=spent)             # 41.8 bps
        self.assertFalse(stuck.qualifies)
        self.assertIn("nearest is 30 bps", stuck.blockers[0])

    def test_nothing_qualifies_is_reported_with_the_nearest_rung(self):
        decision = decide("96.4000")
        self.assertFalse(decision.qualifies)
        self.assertIsNone(decision.active_rung)
        self.assertIn("nearest is 50 bps", decision.blockers[0])
        self.assertIn("20,000 left", decision.blockers[0])

    def test_a_finished_ladder_says_so_rather_than_blocking_vaguely(self):
        decision = decide("96.0500", progress={"30": 10000, "50": 20000})
        self.assertFalse(decision.qualifies)
        self.assertIn("complete", decision.blockers[0])
        self.assertIn("30,000", decision.blockers[0])

    def test_a_finished_ladder_asks_for_no_quantity(self):
        self.assertEqual(decide("96.0500", progress={"30": 10000, "50": 20000}).qty, 0)


class TestTheSizeThatGoesOut(unittest.TestCase):
    def test_one_order_is_still_capped_by_the_clip(self):
        """A rung is a budget for the campaign, not an order size.

        Sending 10,000 in one order against a book showing far less is the
        problem the depth gate exists for, so `lots` still decides what goes
        out at once.
        """
        self.assertEqual(decide("96.0500", cfg=config(lots=1)).qty, 1000)
        self.assertEqual(decide("96.0500", cfg=config(lots=10)).qty, 10000)

    def test_the_clip_is_trimmed_to_what_is_left_on_the_rung(self):
        """Never roll past the rung, even when the clip would allow it."""
        decision = decide("96.0500", progress={"30": 9000}, cfg=config(lots=10))
        self.assertEqual(decision.qty, 1000)

    def test_it_does_not_spill_into_the_next_rung_within_one_clip(self):
        """A single order has a single price, so it belongs to one rung."""
        decision = decide("96.0500", progress={"30": 9000}, cfg=config(lots=25))
        self.assertEqual(decision.active_rung.rung.bps, D("30"))
        self.assertEqual(decision.qty, 1000)


class TestCrediting(unittest.TestCase):
    def setUp(self):
        self.ladder = two_rungs()

    def test_a_fill_is_recorded_against_its_rung(self):
        progress = self.ladder.credit({}, self.ladder.rungs[0], 3000)
        self.assertEqual(progress["30"], 3000)
        self.assertEqual(self.ladder.done_total(progress), 3000)
        self.assertEqual(self.ladder.remaining_total(progress), 27000)

    def test_fills_accumulate(self):
        progress = self.ladder.credit({}, self.ladder.rungs[0], 3000)
        progress = self.ladder.credit(progress, self.ladder.rungs[0], 4000)
        self.assertEqual(progress["30"], 7000)

    def test_a_rung_cannot_be_credited_past_its_own_size(self):
        progress = self.ladder.credit({}, self.ladder.rungs[0], 99000)
        self.assertEqual(progress["30"], 10000)

    def test_crediting_does_not_mutate_what_it_was_given(self):
        original = {}
        self.ladder.credit(original, self.ladder.rungs[0], 1000)
        self.assertEqual(original, {})

    def test_progress_beyond_the_rung_is_not_counted_twice(self):
        """A hand-edited state file must not inflate the total."""
        self.assertEqual(self.ladder.done_total({"30": 999999}), 10000)


class TestItSurvivesARestart(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ladder_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def engine(self, **kw):
        from rollover.engine import RollEngine

        class Log:
            def info(self, m): pass
            def warn(self, m): pass
            def error(self, m): pass
            def alert(self, m): pass

        class Broker:
            logged_in = True
            scrip_file_date = date.today()

        cfg = config(use_live_feed=False, record_market=False,
                     update_check=False, **kw)
        return RollEngine(cfg, Log(), self.dir, broker=Broker())

    def test_progress_comes_back(self):
        first = self.engine()
        first.ladder_progress = first.ladder.credit(
            {}, first.ladder.rungs[0], 4000)
        first._persist()

        self.assertEqual(self.engine().ladder_progress, {"30": 4000})

    def test_it_does_not_reset_overnight(self):
        """A campaign takes as long as it takes. Daily reset would re-roll it."""
        import json
        import os
        first = self.engine()
        first.ladder_progress = {"30": 4000}
        first._persist()

        path = os.path.join(self.dir, "state.json")
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        saved["trading_date"] = "2026-09-01"       # a much earlier day
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(saved, fh)

        again = self.engine()
        self.assertEqual(again.ladder_progress, {"30": 4000})
        self.assertEqual(again.session.clips_done_today, 0, "daily count kept")

    def test_changing_contracts_starts_a_new_campaign(self):
        first = self.engine()
        first.ladder_progress = {"30": 4000}
        first._persist()

        self.assertEqual(self.engine(far_token="9999").ladder_progress, {})

    def test_resetting_clears_it_on_disk_too(self):
        first = self.engine()
        first.ladder_progress = {"30": 4000}
        first._persist()
        first.reset_ladder()

        self.assertEqual(self.engine().ladder_progress, {})


class TestTheEngineRefusesABadLadder(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="badladder_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def build(self, ladder, **kw):
        from rollover.engine import RollEngine

        said = []

        class Log:
            def info(self, m): said.append(m)
            def warn(self, m): said.append(m)
            def error(self, m): said.append(m)
            def alert(self, m): said.append(m)

        class Broker:
            logged_in = True
            scrip_file_date = date.today()

        cfg = RollConfig(near_token="1769", far_token="1584",
                         near_expiry="2026-09-28", far_expiry="2026-11-26",
                         limit_ladder=ladder, use_live_feed=False,
                         record_market=False, update_check=False, **kw)
        return RollEngine(cfg, Log(), self.dir, broker=Broker()), said

    def test_a_broken_ladder_falls_back_to_the_single_limit(self):
        """Refusing to trade at all would be worse than the plain limit."""
        engine, said = self.build([{"bps": "30", "qty": 10500}])
        self.assertFalse(engine.ladder)
        self.assertTrue(any("cannot be used" in m for m in said))

    def test_a_good_ladder_is_announced(self):
        engine, said = self.build([{"bps": "30", "qty": 10000}])
        self.assertTrue(engine.ladder)
        self.assertTrue(any("Ladder:" in m for m in said))


class TestConfigValidation(unittest.TestCase):
    def test_a_bad_ladder_fails_validation(self):
        cfg = config(limit_ladder=[{"bps": "30", "qty": 10500}])
        with self.assertRaises(ConfigError) as ctx:
            cfg.validate()
        self.assertIn("lot size", str(ctx.exception))

    def test_a_ladder_needs_bps_mode(self):
        cfg = config(limit_mode="absolute")
        with self.assertRaises(ConfigError) as ctx:
            cfg.validate()
        self.assertIn("limit_mode", str(ctx.exception))

    def test_a_good_ladder_validates(self):
        config().validate()

    def test_no_ladder_is_the_default(self):
        self.assertEqual(RollConfig().limit_ladder, [])


class TestWithoutALadder(unittest.TestCase):
    """The single-limit path must behave exactly as it did before."""

    def plain(self, far_ask, **kw):
        cfg = RollConfig(near_token="1769", far_token="1584", **kw)
        return rule.compute(quote("1769", "95.7800", "95.7825"),
                            quote("1584", D(far_ask) - D("0.0025"), far_ask),
                            cfg, days=59)

    def test_no_rungs_are_reported(self):
        self.assertEqual(self.plain("96.0500").rungs, [])
        self.assertIsNone(self.plain("96.0500").active_rung)

    def test_the_tenor_limit_still_applies(self):
        self.assertTrue(self.plain("96.0500").qualifies)     # 28 bps under 50
        self.assertFalse(self.plain("96.4000").qualifies)    # 65 bps over 50

    def test_the_clip_is_the_configured_size(self):
        self.assertEqual(self.plain("96.0500", lots=3).qty, 3000)


class TestCampaignKey(unittest.TestCase):
    def test_it_is_the_pair_of_contracts(self):
        self.assertEqual(campaign_key("1769", "1584"), "1769>1584")

    def test_it_differs_when_either_leg_changes(self):
        self.assertNotEqual(campaign_key("1769", "1584"),
                            campaign_key("1769", "9999"))
        self.assertNotEqual(campaign_key("1769", "1584"),
                            campaign_key("8888", "1584"))

    def test_it_is_not_confused_by_stray_spaces(self):
        self.assertEqual(campaign_key(" 1769 ", "1584"), campaign_key("1769", "1584"))


class TestDescribing(unittest.TestCase):
    def test_a_rung_reads_as_a_limit_and_a_size(self):
        text = Rung(D("30"), 10000).describe(lot_size=1000)
        self.assertIn("30 bps", text)
        self.assertIn("10,000", text)
        self.assertIn("10 lots", text)

    def test_one_lot_is_singular(self):
        self.assertIn("(1 lot)", Rung(D("30"), 1000).describe(lot_size=1000))

    def test_the_decision_names_the_rung_it_is_working(self):
        text = decide("96.0500").describe()
        self.assertIn("ladder", text)
        self.assertIn("working rung  30 bps", text)

    def test_an_empty_ladder_is_falsy(self):
        self.assertFalse(Ladder([]))
        self.assertTrue(two_rungs())


if __name__ == "__main__":
    unittest.main(verbosity=2)
