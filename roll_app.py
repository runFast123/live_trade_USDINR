"""USDINR rollover watcher.

    roll_app.exe                 open the window and start watching
    roll_app.exe --find USDINR   list the contracts and their tokens
    roll_app.exe --check         validate config.json and exit
    roll_app.exe --selftest      run the rule on worked examples, no network
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from rollover.config import ConfigError, RollConfig, app_dir
from rollover.logbook import Logbook
from rollover.money import D, money


def _paths():
    base = app_dir()
    return base, os.path.join(base, "config.json"), os.path.join(base, "logs")


def cmd_check(cfg: RollConfig) -> int:
    print("config.json is valid.")
    print(f"  contract     {cfg.underlying}  segment {cfg.segment_id}")
    print(f"  near token   {cfg.near_token or '(not set)'}   expiry {cfg.near_expiry or '?'}")
    print(f"  far token    {cfg.far_token or '(not set)'}   expiry {cfg.far_expiry or '?'}")
    if cfg.limit_mode == "bps":
        print("  limit mode   basis points, by tenor")
        for months, bps in sorted(cfg.limit_bps_schedule.items(),
                                  key=lambda kv: int(kv[0])):
            print(f"               {months} month(s): {bps} bps")
        print(f"  tolerance    +/- {cfg.tenor_tolerance_days} days around a tenor")
    else:
        print("  limit mode   fixed")
        print(f"  roll limit   {money(cfg.roll_limit_d)}")
    print(f"  size         {cfg.lots} lot(s) = {cfg.clip_qty} units")
    print(f"  allowance    {cfg.allowance_ticks} tick(s) = {money(cfg.allowance)}")
    print(f"  mode         {'DRY RUN' if cfg.dry_run else 'LIVE ORDERS'}")
    if not cfg.near_token or not cfg.far_token:
        print("\nSet near_token and far_token before running. Use --find to list them.")
        return 1
    return 0


def cmd_selftest(cfg: RollConfig) -> int:
    """Run the rule on fixed numbers. Nothing is fetched and nothing is sent."""
    from rollover import rule
    from rollover.quotes import Quote

    def quote(bid, ask):
        return Quote("0", D(bid), D(ask), time.monotonic(), D("1"))

    # Tenor matters: the same rupee cost is cheap for two months and dear for
    # one, so every case says how far apart the two contracts expire.
    cases = [
        ("Sep into Oct, one month, as measured", 30,
         "95.9675", "95.9700", "96.2775", "96.2800"),
        ("Sep into Oct, one month, cheaper", 30,
         "95.9675", "95.9700", "96.2425", "96.2450"),
        ("Sep into Nov, two months, as measured", 59,
         "95.9400", "95.9450", "96.5150", "96.5200"),
        ("Sep into Nov, two months, cheaper", 59,
         "95.9400", "95.9450", "96.3800", "96.3850"),
        ("the same 0.40 cost, judged as one month", 30,
         "95.9675", "95.9700", "96.3650", "96.3675"),
        ("the same 0.40 cost, judged as two months", 59,
         "95.9675", "95.9700", "96.3650", "96.3675"),
        ("a weekly, only three days apart", 3,
         "95.9675", "95.9700", "95.9900", "95.9925"),
    ]

    if cfg.limit_mode == "bps":
        pairs = ", ".join(
            f"{m} month{'s' if str(m) != '1' else ''} at {b} bps"
            for m, b in sorted(cfg.limit_bps_schedule.items()))
        print("Rule: roll when (far ask - near bid) is below the limit for the tenor")
        print(f"      {pairs}")
        print("      A basis point is a share of the price, so 30 bps is 0.2879")
        print("      at 95.97 and 0.3150 at 105.00. It is 0.30 only at exactly 100.")
    else:
        print(f"Rule: roll when (far ask - near bid) < {money(cfg.roll_limit_d)}")
    print(f"Size: {cfg.lots} lot(s) = {cfg.clip_qty} units, "
          f"allowance {cfg.allowance_ticks} tick(s)")
    print()

    for title, days, nb, na, fb, fa in cases:
        decision = rule.compute(quote(nb, na), quote(fb, fa), cfg, days=days)
        print(f"{title}")
        print(f"  near bid {money(decision.near_bid)}   far ask {money(decision.far_ask)}")
        bps = decision.cost_bps
        print(f"  roll cost {money(decision.roll_cost)}"
              + (f" = {money(bps, 1)} bps" if bps is not None else "")
              + f"   Rs {money(decision.cost_per_lot, 2)} per clip")
        if decision.limit_detail:
            print(f"  limit     {decision.limit_detail.describe()}")
        print(f"  -> {'ROLL' if decision.qualifies else 'do nothing'}")
        for blocker in decision.blockers:
            print(f"     {blocker}")
        print()
    return 0


def cmd_find(cfg: RollConfig, name: str, log: Logbook, base: str) -> int:
    from rollover.broker import Broker

    broker = Broker(cfg, log)
    broker.connect(os.path.join(base, "session.json"))
    rows = broker.search(name)
    if not rows:
        print(f"Nothing in today's scrip master matches {name!r} in segment {cfg.segment_id}.")
        return 1

    print(f"{'Token':<10} {'Expiry':<12} {'Lot':<8} Contract")
    print("-" * 60)
    for row in rows:
        print(f"{row['Token']:<10} {row['Expiry']:<12} {row['LotSize']:<8} {row['SecDesc']}")
    print("\nCopy the near and far tokens into near_token and far_token in config.json,")
    print("and the matching dates into near_expiry and far_expiry.")
    return 0


def cmd_run(cfg: RollConfig, log: Logbook, base: str, config_path: str) -> int:
    """Sign in, choose the contracts, then watch.

    All three windows share one hidden Tk root, so the contract chooser can be
    reopened later from the watch window instead of only at startup.
    """
    import tkinter as tk

    from rollover.engine import RollEngine
    from rollover.login import LoginWindow
    from rollover.picker import ContractWindow
    from rollover.ui import RollWindow

    log.info("=" * 70)
    log.info(f"Starting. Mode: {'DRY RUN' if cfg.dry_run else 'LIVE ORDERS'}")
    if cfg.limit_mode == "bps":
        pairs = ", ".join(f"{m}m at {b} bps"
                          for m, b in sorted(cfg.limit_bps_schedule.items(),
                                             key=lambda kv: int(kv[0])))
        log.info(f"Rule: roll when (far ask - near bid) is below the tenor limit "
                 f"({pairs}), size {cfg.lots} lot(s) = {cfg.clip_qty} units")
    else:
        log.info(f"Rule: roll when (far ask - near bid) < {money(cfg.roll_limit_d)}, "
                 f"size {cfg.lots} lot(s) = {cfg.clip_qty} units")
    if not cfg.dry_run:
        log.warn("LIVE MODE. Real orders will be sent when you arm the app "
                 "and every gate passes.")

    root = tk.Tk()
    root.withdraw()

    login = LoginWindow(root, cfg, log, base, config_path)
    root.wait_window(login)
    if not login.ok:
        log.info("Login cancelled. Nothing was started.")
        root.destroy()
        return 1
    broker = login.broker

    def choose_contracts() -> bool:
        picker = ContractWindow(root, cfg, log, broker, config_path)
        picker.grab_set()
        root.wait_window(picker)
        return picker.ok

    if not (cfg.near_token and cfg.far_token):
        if not choose_contracts():
            log.info("No contracts chosen. Nothing was started.")
            root.destroy()
            return 1

    engine = RollEngine(cfg, log, base, broker=broker)
    engine.start()

    def change_contracts() -> None:
        before = (cfg.near_token, cfg.far_token)
        if choose_contracts() and (cfg.near_token, cfg.far_token) != before:
            log.info("Contracts changed. Restarting the watch loop.")
            engine.restart()

    window = RollWindow(root, engine, log, on_change_contracts=change_contracts,
                        config_path=config_path)
    root.wait_window(window)
    engine.stop()
    root.destroy()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="roll_app",
        description="Watch two USDINR futures and roll one clip when the cost is "
                    "below the limit.")
    parser.add_argument("--config", help="path to config.json")
    parser.add_argument("--find", metavar="NAME",
                        help="list matching contracts and their tokens, then exit")
    parser.add_argument("--check", action="store_true",
                        help="validate the configuration and exit")
    parser.add_argument("--selftest", action="store_true",
                        help="run the rule on worked examples with no network")
    args = parser.parse_args(argv)

    base, config_path, log_dir = _paths()
    if args.config:
        config_path = args.config

    try:
        cfg = RollConfig.load(config_path)
    except ConfigError as exc:
        if args.selftest:
            print(f"(no usable config at {config_path}, using defaults for the selftest)\n")
            cfg = RollConfig()
        elif not os.path.exists(config_path):
            # First run. Write the defaults so the windows have something to fill in.
            cfg = RollConfig()
            try:
                cfg.save(config_path)
                print(f"Created {config_path} with default settings.")
            except OSError as write_error:
                print(f"ERROR: could not create {config_path}: {write_error}",
                      file=sys.stderr)
                return 2
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    if args.selftest:
        return cmd_selftest(cfg)
    if args.check:
        return cmd_check(cfg)

    log = Logbook(log_dir)
    try:
        if args.find:
            return cmd_find(cfg, args.find, log, base)
        return cmd_run(cfg, log, base, config_path)
    except Exception as exc:
        log.error(f"Fatal: {exc}")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
