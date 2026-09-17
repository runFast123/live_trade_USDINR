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

    cases = [
        ("your note: Sep 95.9400 bid, Nov 96.5200 ask", "95.9400", "95.9450",
         "96.5150", "96.5200"),
        ("roll cost exactly at the limit", "95.9400", "95.9450", "96.2400", "96.2400"),
        ("roll cost just under the limit", "95.9400", "95.9450", "96.2375", "96.2375"),
        ("backwardation, far cheaper than near", "95.9400", "95.9450", "95.8000", "95.8050"),
    ]

    print(f"Rule: roll when (far ask - near bid) < {money(cfg.roll_limit_d)}")
    print(f"Size: {cfg.lots} lot(s) = {cfg.clip_qty} units, "
          f"allowance {cfg.allowance_ticks} tick(s)\n")

    for title, nb, na, fb, fa in cases:
        decision = rule.compute(quote(nb, na), quote(fb, fa), cfg)
        print(f"{title}")
        print(f"  near bid {money(decision.near_bid)}   far ask {money(decision.far_ask)}")
        print(f"  roll cost {money(decision.roll_cost)}  "
              f"(Rs {money(decision.cost_per_lot, 2)} per clip)")
        print(f"  worst case {money(decision.worst_case)}  "
              f"sell {money(decision.sell_limit)}  buy {money(decision.buy_limit)}")
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
