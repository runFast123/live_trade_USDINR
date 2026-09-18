"""The watch window.

Both legs with their depth, the roll cost against the limit, every gate, and the
log. The operator's job here is to decide whether the app may act, so the only
controls are arm, disarm, clear halt, and changing the contracts.
"""
from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from . import theme as T
from . import __version__, livemode, notify, updater
from .engine import ARMED, DONE, HALTED, RollEngine, WATCHING, WORKING
from .money import D, money

def _numeric(text: str) -> bool:
    try:
        D(text)
        return True
    except Exception:
        return False


STATE_COLOUR = {
    "STARTING": T.MUTED,
    WATCHING: T.ACCENT,
    ARMED: T.WARN,
    WORKING: T.WARN,
    DONE: T.SUCCESS,
    HALTED: T.DANGER,
}

GATE_COLUMNS = (
    ("state", "", 70, "center"),
    ("name", "Gate", 210, "w"),
    ("detail", "Detail", 560, "w"),
)


class LiveModeDialog(tk.Toplevel):
    """The one window in this program that lets real orders out.

    It is deliberately not a toggle. It states what a clip commits in rupees,
    lists every precondition with its verdict, and will not enable its own
    button until they all pass and the phrase has been typed exactly.

    Going the other way needs none of this, and has no dialog at all.
    """

    def __init__(self, parent, cfg, engine, fonts, log, on_done=None):
        super().__init__(parent)
        self.cfg, self.engine, self.fonts = cfg, engine, fonts
        self.log, self.on_done = log, on_done

        self.title("Switch to live orders")
        self.configure(bg=T.BG)
        self.resizable(False, False)
        self.transient(parent)

        self.checks = livemode.preflight(cfg, engine)
        self.exposure = livemode.exposure(cfg, self._price(), self._lot_size(),
                                          engine=engine)

        body = tk.Frame(self, bg=T.BG)
        body.pack(fill="both", expand=True, padx=T.PAD_L, pady=T.PAD_L)

        tk.Label(body, text="This sends real orders",
                 bg=T.BG, fg=T.DANGER, font=fonts.title,
                 anchor="w").pack(fill="x")

        tk.Label(body, text=livemode.summary(cfg, self.checks, self.exposure),
                 bg=T.SURFACE_2, fg=T.TEXT, font=fonts.mono, justify="left",
                 anchor="w", padx=T.PAD_M, pady=T.PAD_M).pack(
                     fill="x", pady=T.PAD_M)

        self.blocked = bool(livemode.blockers(self.checks))
        if self.blocked:
            tk.Label(body,
                     text=("Live mode is not available until every line above "
                           "reads OK."),
                     bg=T.BG, fg=T.WARN, font=fonts.ui, anchor="w",
                     wraplength=520, justify="left").pack(fill="x")
        else:
            tk.Label(body, text=f'Type  {livemode.CONFIRM}  to confirm:',
                     bg=T.BG, fg=T.TEXT, font=fonts.ui, anchor="w").pack(fill="x")
            self.entry = tk.Entry(body, bg=T.SURFACE_2, fg=T.TEXT, font=fonts.mono,
                                  insertbackground=T.TEXT, relief="flat",
                                  highlightthickness=1,
                                  highlightbackground=T.BORDER,
                                  highlightcolor=T.ACCENT)
            self.entry.pack(fill="x", ipady=6, pady=(T.PAD_S, T.PAD_M))
            self.entry.bind("<KeyRelease>", lambda _e: self._retest())
            self.entry.bind("<Return>", lambda _e: self._confirm())

        row = tk.Frame(body, bg=T.BG)
        row.pack(fill="x")

        T.Button(row, "Cancel", self.destroy, fonts, width=130,
                 height=40).pack(side="right")

        if not self.blocked:
            self.go = T.Button(row, "Send real orders", self._confirm, fonts,
                               kind="danger", width=210, height=40)
            self.go.pack(side="right", padx=(0, T.PAD_S))
            self.go.set_enabled(False)

        self.bind("<Escape>", lambda _e: self.destroy())
        T.centre(self)
        self.grab_set()
        if not self.blocked:
            self.entry.focus_set()

    def _price(self):
        """A price to value the clip at, if one is to be had."""
        try:
            snap = self.engine.snapshot()
            for quote in (snap.near_quote, snap.far_quote):
                if quote is not None and quote.ask:
                    return quote.ask
        except Exception:
            pass
        return None

    def _lot_size(self):
        try:
            return self.engine.session.near.lot_size
        except Exception:
            return None

    def _retest(self) -> None:
        typed = self.entry.get().strip()
        self.go.set_enabled(typed == livemode.CONFIRM)

    def _confirm(self) -> None:
        if self.blocked:
            return
        # Re-run the preconditions rather than trusting the ones drawn a minute
        # ago. A halt, a dropped feed or a signed-out session between opening
        # this window and pressing the button all have to count.
        refusal = livemode.go_live(self.cfg, self.engine, self.entry.get())
        if refusal:
            from tkinter import messagebox
            messagebox.showwarning("Not switched", refusal, parent=self)
            self.log.warn(f"Live mode refused: {refusal}")
            return

        self.log.alert(
            "LIVE ORDERS ARE ENABLED. Real orders will be sent when armed and "
            "the roll cost clears the limit.")
        for line in self.exposure.lines(
                bool(getattr(self.cfg, "quantity_unit_confirmed", False))):
            self.log.info(f"  {line}")
        notify.alarm()
        self.destroy()
        if self.on_done:
            self.on_done()


class RollWindow(tk.Toplevel):
    def __init__(self, master, engine: RollEngine, log,
                 on_change_contracts: Optional[Callable[[], None]] = None,
                 config_path: Optional[str] = None):
        super().__init__(master)
        self.engine = engine
        self.log = log
        self.cfg = engine.cfg
        self.on_change_contracts = on_change_contracts
        self.config_path = config_path

        self.title(f"{self.cfg.underlying} rollover")
        self.configure(bg=T.BG)
        width, height = T.fit(self, 1180, 940, margin_h=120)
        self.geometry(f"{width}x{height}+24+18")
        self.minsize(940, 600)
        if T.is_compact(self):
            # A short screen has nothing to spare, so open filling it.
            try:
                self.state("zoomed")
            except tk.TclError:
                pass

        self.compact = T.is_compact(self)
        # On a short screen the panes at the bottom are what gets squeezed, so
        # the fixed cards above them give up their padding first.
        self.gap = T.PAD_S if self.compact else T.PAD_M
        T.apply_icon(self)
        self.fonts = T.Fonts(compact=self.compact)
        T.apply_ttk_theme(self, self.fonts)

        self._refresh_id = None
        self._last_log_count = -1
        self._alerts_seen = 0          # so a new ALERT can be heard, not just seen
        self._updates: "queue.Queue[tuple]" = queue.Queue()
        # Last seen price per cell, so a change can be shown rather than just
        # rendered: {cell key: (price, direction, moved_at, delta)}
        self._ticks: dict = {}
        self._release = None
        self._updating = False
        self._build()
        self._start_update_check()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_id = self.after(200, self._refresh)

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        self.scroller = T.ScrollFrame(self)
        self.scroller.pack(fill="both", expand=True)
        root = tk.Frame(self.scroller.body, bg=T.BG, padx=T.PAD_L, pady=self.gap)
        root.pack(fill="both", expand=True)
        self._build_header(root)
        self._build_legs(root)
        self._build_cost(root)
        self._build_ladder(root)
        self._build_actions(root)
        self._build_gates_and_log(root)

    def _build_header(self, parent) -> None:
        bar = tk.Frame(parent, bg=T.BG)
        bar.pack(fill="x", pady=(0, self.gap))

        left = tk.Frame(bar, bg=T.BG)
        left.pack(side="left", fill="x", expand=True)

        self.state_pill = T.Pill(left, self.fonts, width=132, height=30)
        self.state_pill.pack(side="left")
        self.state_pill.set("starting", T.MUTED)

        self.note = tk.Label(left, text="", bg=T.BG, fg=T.MUTED,
                             font=self.fonts.ui, anchor="w", justify="left",
                             wraplength=560)
        self.note.pack(side="left", padx=T.PAD_M)

        right = tk.Frame(bar, bg=T.BG)
        right.pack(side="right")

        self.mode_pill = T.Pill(right, self.fonts, width=186, height=30)
        self.mode_pill.pack(side="right")
        self._mode_shown = None
        self._refresh_mode()

        self.clock = tk.Label(right, text="", bg=T.BG, fg=T.FAINT,
                              font=self.fonts.mono)
        self.clock.pack(side="right", padx=T.PAD_M)

        self.source_pill = T.Pill(right, self.fonts, width=124, height=30)
        self.source_pill.pack(side="right", padx=(0, T.PAD_S))
        self.source_pill.set("connecting", T.MUTED)

        self.update_button = T.Button(right, "Update", self._do_update, self.fonts,
                                      kind="primary", width=172, height=30)
        # Packed only once a newer release has actually been found.

        tk.Label(right, text=f"v{__version__}", bg=T.BG, fg=T.FAINT,
                 font=self.fonts.ui_small).pack(side="right", padx=(0, T.PAD_S))

    def _build_legs(self, parent) -> None:
        wrap = tk.Frame(parent, bg=T.BG)
        wrap.pack(fill="x")
        wrap.columnconfigure(0, weight=1, uniform="leg")
        wrap.columnconfigure(1, weight=1, uniform="leg")

        self.leg_widgets = {}
        for column, (key, title, side, colour) in enumerate((
                ("near", "NEAR LEG", "SELL at the bid", T.BUY),
                ("far", "FAR LEG", "BUY at the ask", T.SELL))):
            holder = T.card(wrap, padx=T.PAD_M, pady=self.gap)
            holder.grid(row=0, column=column, sticky="nsew",
                        padx=(0, T.PAD_S) if column == 0 else (T.PAD_S, 0))
            box = holder.inner

            top = tk.Frame(box, bg=T.SURFACE)
            top.pack(fill="x")
            tk.Label(top, text=title, bg=T.SURFACE, fg=colour,
                     font=self.fonts.label).pack(side="left")
            tk.Label(top, text=side, bg=T.SURFACE, fg=T.FAINT,
                     font=self.fonts.ui_small).pack(side="right")

            name = tk.Label(box, text="--", bg=T.SURFACE, fg=T.TEXT,
                            font=self.fonts.mono_medium, anchor="w")
            name.pack(fill="x", pady=(T.PAD_S, 0))
            meta = tk.Label(box, text="", bg=T.SURFACE, fg=T.FAINT,
                            font=self.fonts.ui_small, anchor="w")
            meta.pack(fill="x")

            prices = tk.Frame(box, bg=T.SURFACE)
            prices.pack(fill="x", pady=(self.gap, 0))
            prices.columnconfigure(0, weight=1, uniform="px")
            prices.columnconfigure(1, weight=1, uniform="px")

            cells = {"name": name, "meta": meta}
            for i, (field, caption) in enumerate((("bid", "BID"), ("ask", "ASK"))):
                cell = tk.Frame(prices, bg=T.SURFACE)
                cell.grid(row=0, column=i, sticky="we", padx=(0, T.PAD_S))

                head = tk.Frame(cell, bg=T.SURFACE)
                head.pack(fill="x")
                tk.Label(head, text=caption, bg=T.SURFACE, fg=T.FAINT,
                         font=self.fonts.label).pack(side="left")
                delta = tk.Label(head, text="", bg=T.SURFACE, fg=T.SURFACE,
                                 font=self.fonts.ui_small)
                delta.pack(side="right")

                value = tk.Label(cell, text="--", bg=T.SURFACE, fg=T.TEXT,
                                 font=self.fonts.mono_large, anchor="w",
                                 padx=4)
                value.pack(fill="x")
                size = tk.Label(cell, text="", bg=T.SURFACE, fg=T.MUTED,
                                font=self.fonts.ui_small, anchor="w")
                size.pack(fill="x")
                cells[field] = value
                cells[field + "_delta"] = delta
                cells[field + "_size"] = size

            cells["age"] = tk.Label(top, text="", bg=T.SURFACE, fg=T.FAINT,
                                    font=self.fonts.ui_small)
            cells["age"].pack(side="right", padx=(0, T.PAD_S))
            self.leg_widgets[key] = cells

    def _build_cost(self, parent) -> None:
        holder = T.card(parent, padx=T.PAD_L, pady=self.gap)
        holder.pack(fill="x", pady=self.gap)
        box = holder.inner

        left = tk.Frame(box, bg=T.SURFACE)
        left.pack(side="left")
        tk.Label(left, text="ROLL COST", bg=T.SURFACE, fg=T.FAINT,
                 font=self.fonts.label, anchor="w").pack(fill="x")
        hero = tk.Frame(left, bg=T.SURFACE)
        hero.pack(fill="x")
        self.cost = tk.Label(hero, text="--", bg=T.SURFACE, fg=T.TEXT,
                             font=self.fonts.mono_hero, anchor="w")
        self.cost.pack(side="left")
        self.cost_delta = tk.Label(hero, text="", bg=T.SURFACE, fg=T.SURFACE,
                                   font=self.fonts.ui_medium)
        self.cost_delta.pack(side="left", padx=(T.PAD_S, 0), anchor="s", pady=(0, 8))
        self.cost_rupees = tk.Label(left, text="", bg=T.SURFACE, fg=T.MUTED,
                                    font=self.fonts.ui, anchor="w")
        self.cost_rupees.pack(fill="x")

        right = tk.Frame(box, bg=T.SURFACE)
        right.pack(side="right", padx=(T.PAD_L, 0))
        self.verdict = tk.Label(right, text="", bg=T.SURFACE, fg=T.MUTED,
                                font=self.fonts.mono_large)
        self.verdict.pack(anchor="e")
        self.verdict_note = tk.Label(right, text="", bg=T.SURFACE, fg=T.FAINT,
                                     font=self.fonts.ui_small, justify="right")
        self.verdict_note.pack(anchor="e")

        # ---- the limit, editable ------------------------------------------
        limit_box = tk.Frame(box, bg=T.SURFACE)
        limit_box.pack(side="left", padx=T.PAD_XL)
        tk.Label(limit_box, text="ROLL LIMIT", bg=T.SURFACE, fg=T.FAINT,
                 font=self.fonts.label, anchor="w").pack(fill="x")

        row = tk.Frame(limit_box, bg=T.SURFACE)
        row.pack(fill="x", pady=(2, 0))
        self.limit_var = tk.StringVar()
        self.limit_entry = T.entry(row, self.fonts, textvariable=self.limit_var,
                                   width=8)
        self.limit_entry.pack(side="left", ipady=5)
        self.limit_entry.bind("<Return>", lambda _e: self._apply_limit())
        self.limit_entry.bind("<Escape>", lambda _e: self._reset_limit())
        self.limit_unit = tk.Label(row, text="", bg=T.SURFACE, fg=T.MUTED,
                                   font=self.fonts.ui_small)
        self.limit_unit.pack(side="left", padx=(T.PAD_XS, 0))
        T.Button(row, "Set", self._apply_limit, self.fonts,
                 width=58, height=30).pack(side="left", padx=T.PAD_XS)

        self.limit_note = tk.Label(limit_box, text="", bg=T.SURFACE,
                                   fg=T.FAINT, font=self.fonts.ui_small, anchor="w")
        self.limit_note.pack(fill="x")
        self._last_decision = None
        self._reset_limit()

        stats = tk.Frame(box, bg=T.SURFACE)
        stats.pack(side="left", padx=T.PAD_M)
        self.stat_labels = {}
        for i, (key, caption) in enumerate((("worst", "WORST CASE"),
                                            ("qty", "QTY"),
                                            ("sell", "SELL LIMIT"),
                                            ("buy", "BUY LIMIT"))):
            cell = tk.Frame(stats, bg=T.SURFACE)
            cell.grid(row=i // 2, column=i % 2, sticky="w",
                      padx=(0, T.PAD_L), pady=2)
            tk.Label(cell, text=caption, bg=T.SURFACE, fg=T.FAINT,
                     font=self.fonts.label, anchor="w").pack(fill="x")
            value = tk.Label(cell, text="--", bg=T.SURFACE, fg=T.TEXT,
                             font=self.fonts.mono, anchor="w")
            value.pack(fill="x")
            self.stat_labels[key] = value

    LADDER_HEADINGS = ("LIMIT", "QTY", "IN RUPEES", "FAR ASK AT OR BELOW",
                       "DISTANCE", "ROLLED", "")

    def _build_ladder(self, parent) -> None:
        """Several roll limits at once, each with its own quantity, editable.

        The single ROLL LIMIT box answers "may I roll?" and nothing else. An
        operator willing to do ten thousand at thirty basis points and twenty
        thousand at fifty needs to set both and watch both, so each rung gets
        the same treatment that one limit had: a figure you can type, the rupee
        amount it works out to, the far ask that would satisfy it, and how far
        away the market is right now.
        """
        self.ladder_card = T.card(parent, padx=T.PAD_L, pady=self.gap)
        box = self.ladder_card.inner

        head = tk.Frame(box, bg=T.SURFACE)
        head.pack(fill="x")
        tk.Label(head, text="LADDER", bg=T.SURFACE, fg=T.FAINT,
                 font=self.fonts.label, anchor="w").pack(side="left")
        self.ladder_total = tk.Label(head, text="", bg=T.SURFACE, fg=T.MUTED,
                                     font=self.fonts.ui_small, anchor="e")
        self.ladder_total.pack(side="right")

        self.ladder_grid = tk.Frame(box, bg=T.SURFACE)
        self.ladder_grid.pack(fill="x", pady=(T.PAD_S, 0))
        for column, caption in enumerate(self.LADDER_HEADINGS):
            tk.Label(self.ladder_grid, text=caption, bg=T.SURFACE, fg=T.FAINT,
                     font=self.fonts.label,
                     anchor="w" if column < 2 else "e").grid(
                         row=0, column=column, sticky="ew",
                         padx=(0, T.PAD_M), pady=(0, 2))
        self.ladder_grid.columnconfigure(6, weight=1)

        controls = tk.Frame(box, bg=T.SURFACE)
        controls.pack(fill="x", pady=(T.PAD_S, 0))
        T.Button(controls, "Add limit", self._add_rung, self.fonts,
                 width=110, height=30).pack(side="left")
        T.Button(controls, "Set ladder", self._apply_ladder, self.fonts,
                 kind="primary", width=110, height=30).pack(side="left",
                                                            padx=T.PAD_XS)
        # Wrapped, not truncated: a refusal that stops mid-sentence tells the
        # operator a rung is wrong without telling them what to do about it.
        self.ladder_note = tk.Label(controls, text="", bg=T.SURFACE, fg=T.FAINT,
                                    font=self.fonts.ui_small, anchor="w",
                                    justify="left", wraplength=620)
        self.ladder_note.pack(side="left", padx=T.PAD_S, fill="x", expand=True)

        self.rung_rows = []
        self._ladder_shown = False
        self._rebuild_rung_rows()

    # ---- the rows ---------------------------------------------------------
    def _rebuild_rung_rows(self) -> None:
        """Draw one row per rung of the ladder the engine is actually using."""
        for row in list(self.rung_rows):
            self._forget_row(row)
        self.rung_rows = []

        ladder = getattr(self.engine, "ladder", None)
        for rung in (ladder.rungs if ladder else []):
            self._add_row(f"{rung.bps.normalize():f}", str(rung.qty), rung.key)

        for bps in (getattr(self.engine, "watch_limits", None) or []):
            key = f"{bps.normalize():f}"
            self._add_row(key, "", key, watch=True)
        self._reflow_rows()

    def _forget_row(self, row) -> None:
        for widget in (row["bps_box"], row["qty_box"], row["remove"],
                       *row["cells"].values()):
            widget.destroy()

    def _add_row(self, bps: str = "", qty: str = "", key=None,
                 watch: bool = False) -> None:
        row = {"key": key, "watch": watch, "bps": tk.StringVar(value=bps),
               "qty": tk.StringVar(value=qty), "cells": {}}

        row["bps_box"] = T.entry(self.ladder_grid, self.fonts,
                                 textvariable=row["bps"], width=6)
        row["qty_box"] = T.entry(self.ladder_grid, self.fonts,
                                 textvariable=row["qty"], width=9)
        for box in (row["bps_box"], row["qty_box"]):
            box.bind("<Return>", lambda _e: self._apply_ladder())
            box.bind("<Escape>", lambda _e: self._rebuild_rung_rows())

        for name in ("rupees", "target", "gap", "rolled", "status"):
            row["cells"][name] = tk.Label(
                self.ladder_grid, text="--", bg=T.SURFACE, fg=T.MUTED,
                font=self.fonts.mono, anchor="e")

        row["remove"] = T.Button(self.ladder_grid, "Remove",
                                 lambda r=row: self._remove_rung(r),
                                 self.fonts, width=88, height=26)
        self.rung_rows.append(row)

    def _reflow_rows(self) -> None:
        """Place every row's widgets. Called after any add or remove."""
        for index, row in enumerate(self.rung_rows, start=1):
            row["bps_box"].grid(row=index, column=0, sticky="w",
                                padx=(0, T.PAD_M), pady=2, ipady=3)
            row["qty_box"].grid(row=index, column=1, sticky="w",
                                padx=(0, T.PAD_M), pady=2, ipady=3)
            for column, name in enumerate(("rupees", "target", "gap", "rolled"),
                                          start=2):
                row["cells"][name].grid(row=index, column=column, sticky="e",
                                        padx=(0, T.PAD_M))
            row["cells"]["status"].configure(anchor="w")
            row["cells"]["status"].grid(row=index, column=6, sticky="w",
                                        padx=(0, T.PAD_M))
            row["remove"].grid(row=index, column=7, sticky="e", pady=2)

    def _add_rung(self) -> None:
        self._add_row()
        self._reflow_rows()
        self._show_ladder()
        self._ladder_note(
            "a limit with a quantity is traded; leave the quantity blank to "
            "watch and compare it only", T.MUTED)
        self.rung_rows[-1]["bps_box"].focus_set()

    def _remove_rung(self, row) -> None:
        self._forget_row(row)
        if row in self.rung_rows:
            self.rung_rows.remove(row)
        self._reflow_rows()
        self._ladder_note("removed; press Set ladder to apply", T.WARN)

    def _ladder_note(self, text: str, colour: str) -> None:
        self.ladder_note.configure(text=text, fg=colour)

    def _show_ladder(self) -> None:
        if not self._ladder_shown:
            self.ladder_card.pack(fill="x", pady=self.gap,
                                  before=self.actions_bar)
            self._ladder_shown = True

    # ---- applying ---------------------------------------------------------
    def _typed_ladder(self):
        """The rows, split into rungs and watch lines. None on a complaint.

        A limit with a quantity is a rung: it will be traded, and it is bound
        by the tenor ceiling. A limit with the quantity left blank is a watch
        line: priced and compared so several limits can be read at once, never
        traded, and therefore not bound by the ceiling. That is the safe place
        to ask "what would fifty look like?" -- the single roll limit is not,
        because it IS the ceiling.
        """
        rungs, watch = [], []
        for row in self.rung_rows:
            bps, qty = row["bps"].get().strip(), row["qty"].get().strip()
            if not bps:
                if qty:
                    self._ladder_note("a quantity with no limit is not a rung",
                                      T.DANGER)
                    return None
                continue                      # a row left blank is nothing
            if qty:
                rungs.append({"bps": bps, "qty": qty})
            else:
                watch.append(bps)
        return rungs, watch

    def _apply_ladder(self) -> None:
        """Validate what has been typed and put it into force.

        The same validation config.json goes through, so a ladder that would
        make the app unsafe is refused rather than accepted. Applying always
        disarms: a typed digit must not fire a roll on the next tick.
        """
        from dataclasses import replace

        from .ladder import parse as parse_ladder, parse_watch

        typed = self._typed_ladder()
        if typed is None:
            return
        wanted, watch = typed

        if getattr(self.engine.session, "in_flight", False):
            # A clip in flight was sized and priced against the rung the
            # decision is holding. Changing the ladder under it means the fill
            # is credited to a rung that no longer exists, and the quantity is
            # silently lost -- the position moves and the campaign does not
            # know, so it would roll that much again.
            self._ladder_note("an order is working; wait for it to finish",
                              T.WARN)
            return

        ceiling = self.engine._tenor_ceiling_bps()
        if wanted and ceiling is None and self.cfg.limit_mode == "bps":
            # Without a tenor there is no ceiling, and without a ceiling a rung
            # could quietly be looser than the limit for this roll. Refusing is
            # the safe direction, and it clears as soon as the contracts load.
            self._ladder_note(
                "the tenor is not known yet, so a rung cannot be checked "
                "against the limit", T.DANGER)
            return

        try:
            built = parse_ladder(wanted, lot_size=self.cfg.expected_lot_size,
                                 ceiling_bps=ceiling)
            # Watch lines are deliberately NOT ceiling-checked. They cannot
            # trade, so they cannot loosen anything, and that is exactly what
            # makes them the safe place to ask what a wider limit would look
            # like -- the single roll limit is not, because it IS the ceiling.
            parse_watch(watch)
            candidate = replace(self.cfg, limit_ladder=wanted,
                                watch_limits=watch)
            candidate.validate()
        except Exception as exc:
            first = str(exc).splitlines()[-1].strip(" -")
            self._ladder_note(first, T.DANGER)
            return

        # Compare the rungs, not the text. config.json holds qty as a number
        # and the box hands back a string, so comparing the raw entries made
        # every press look like a change: disarming and re-saving each time.
        current = getattr(self.engine, "ladder", None)
        same_rungs = ([(r.bps, r.qty) for r in (current.rungs if current else [])]
                      == [(r.bps, r.qty) for r in built.rungs])
        if same_rungs and parse_watch(watch) == parse_watch(self.cfg.watch_limits):
            self._ladder_note("unchanged", T.MUTED)
            return

        was_armed = self.engine.armed
        self.engine.disarm("ladder changed")
        self.cfg.limit_ladder = wanted
        self.cfg.watch_limits = watch
        self.engine.rebuild_ladder()
        self._rebuild_rung_rows()

        self.log.info(
            f"Ladder set to {len(wanted)} rung(s)"
            + (f" and {len(watch)} watch limit(s)" if watch else "")
            + (" (disarmed)" if was_armed else ""))

        if self.config_path:
            try:
                self.cfg.save(self.config_path)
                self._ladder_note("saved", T.SUCCESS)
            except OSError as exc:
                self.log.warn(f"Could not save config.json: {exc}")
                self._ladder_note("applied, not saved", T.WARN)
        else:
            self._ladder_note("applied", T.SUCCESS)

        self.after(4000, lambda: self._ladder_note("", T.FAINT))

    # ---- the live numbers -------------------------------------------------
    def _draw_ladder(self, decision) -> None:
        """Fill in what the market is doing against each rung."""
        if (not self.rung_rows and not getattr(self.engine, "ladder", None)
                and not self.cfg.watch_limits):
            if self._ladder_shown:
                self.ladder_card.pack_forget()
                self._ladder_shown = False
            return
        self._show_ladder()

        views = {v.rung.key: v for v in (getattr(decision, "rungs", None) or [])}
        watched = {v.rung.key: v
                   for v in (getattr(decision, "watch_rungs", None) or [])}
        active = getattr(decision, "active_rung", None)
        active_key = active.rung.key if active else None

        done = total = 0
        for row in self.rung_rows:
            source = watched if row.get("watch") else views
            view = source.get(row["key"]) if row["key"] else None
            cells = row["cells"]

            if view is None:
                # Typed but not applied yet, or no quote to price it against.
                for name in ("rupees", "target", "gap", "rolled"):
                    cells[name].configure(text="--", fg=T.MUTED)
                cells["status"].configure(text="not set", fg=T.FAINT)
                continue

            total += view.rung.qty
            done += view.done

            if view.watch:
                # It is priced for comparison and will never be traded, so it
                # is shown in the colour of information rather than of action.
                colour = T.ACCENT_TEXT if view.qualifies else T.MUTED
                status = view.status
            elif view.exhausted:
                colour, status = T.FAINT, "done"
            elif view.rung.key == active_key:
                colour, status = T.WARN, "WORKING"
            elif view.qualifies:
                colour, status = T.SUCCESS, "READY"
            else:
                colour, status = T.MUTED, view.status

            gap = "--"
            if view.distance_bps is not None:
                # Negative means the market is already through this rung, which
                # reads better as how far past it we are.
                inside = view.distance_bps < 0
                gap = ("-" if inside else "+") + money(abs(view.distance_bps), 1)

            cells["rupees"].configure(
                text=money(view.limit_rupees) if view.limit_rupees is not None
                else "--", fg=T.TEXT)
            cells["target"].configure(
                text=money(view.required_far_ask)
                if view.required_far_ask is not None else "--", fg=T.TEXT)
            cells["gap"].configure(text=gap, fg=colour)
            cells["rolled"].configure(
                text="watching" if view.watch
                else f"{view.done:,} / {view.rung.qty:,}",
                fg=T.FAINT if view.watch else T.TEXT)
            cells["status"].configure(text=status, fg=colour)

        if total:
            left = max(0, total - done)
            self.ladder_total.configure(
                text=f"{done:,} of {total:,} rolled, {left:,} left"
                     f"   clip {self.cfg.clip_qty:,}")
        else:
            self.ladder_total.configure(text="no rungs set")

    def _build_actions(self, parent) -> None:
        bar = tk.Frame(parent, bg=T.BG)
        bar.pack(fill="x", pady=(0, self.gap))
        self.actions_bar = bar

        self.arm_button = T.Button(bar, "ARM", self.engine.arm, self.fonts,
                                   kind="success", width=170, height=42)
        self.arm_button.pack(side="left")

        T.Button(bar, "Disarm", lambda: self.engine.disarm("button"),
                 self.fonts, width=120, height=42).pack(side="left", padx=T.PAD_S)

        self.halt_button = T.Button(bar, "Clear halt", self.engine.clear_halt,
                                    self.fonts, kind="warn", width=130, height=42)
        self.halt_button.pack(side="left")
        self.halt_button.set_enabled(False)

        if self.on_change_contracts:
            T.Button(bar, "Change contracts", self._change_contracts, self.fonts,
                     width=180, height=42).pack(side="left", padx=T.PAD_S)

        self.mode_button = T.Button(bar, "Go live...", self._switch_mode,
                                    self.fonts, kind="warn", width=150, height=42)
        self.mode_button.pack(side="left", padx=T.PAD_S)

        T.Button(bar, "Cancel all", self._cancel_all, self.fonts,
                 kind="danger", width=140, height=42).pack(side="left", padx=T.PAD_S)

        if getattr(self.engine, "ladder", None):
            T.Button(bar, "Reset ladder", self._reset_ladder, self.fonts,
                     width=150, height=42).pack(side="left")
        self._refresh_mode()

        self.arm_timer = tk.Label(bar, text="", bg=T.BG, fg=T.WARN,
                                  font=self.fonts.mono_medium)
        self.arm_timer.pack(side="right", padx=T.PAD_S)

    def _reset_ladder(self) -> None:
        from tkinter import messagebox

        ladder = getattr(self.engine, "ladder", None)
        if not ladder:
            return
        if getattr(self.engine.session, "in_flight", False):
            messagebox.showwarning(
                "Reset ladder",
                "An order is working. Wait for it to finish, so its fill is "
                "counted before the record is cleared.", parent=self)
            return
        done = ladder.done_total(self.engine.ladder_progress)
        if not messagebox.askyesno(
                "Reset ladder",
                f"This forgets that {done:,} of {ladder.total_qty:,} has been "
                "rolled, and offers the whole campaign again.\n\n"
                "It changes nothing at the exchange. Only do this when the "
                "progress shown no longer matches the position book.\n\n"
                "Reset it?",
                parent=self, default="no"):
            return
        self.engine.disarm("ladder reset")
        self.engine.reset_ladder()

    # ---- dry run and live -------------------------------------------------
    def _refresh_mode(self, dry_run=None) -> None:
        """Keep the pill and the button agreeing with what will actually happen.

        Driven from the engine's own snapshot on every redraw, not only from
        the button that changed it, so the screen cannot sit claiming DRY RUN
        while the engine would send an order.
        """
        dry = self.cfg.dry_run if dry_run is None else dry_run
        if dry == self._mode_shown:
            return
        self._mode_shown = dry

        if dry:
            self.mode_pill.set("DRY RUN, nothing sent", T.WARN, "#2a2008")
        else:
            self.mode_pill.set("LIVE ORDERS", T.DANGER, "#2a0d0b")

        button = getattr(self, "mode_button", None)   # built after the pill
        if button is not None:
            button.set_text("Go live..." if dry else "Back to dry run")

    def _switch_mode(self) -> None:
        if not self.cfg.dry_run:
            livemode.go_dry(self.cfg, self.engine)
            self.log.alert("Switched to DRY RUN. Nothing further will be sent.")
            self._refresh_mode()
            return
        LiveModeDialog(self, self.cfg, self.engine, self.fonts, self.log,
                       on_done=self._refresh_mode)

    # ---- updates ----------------------------------------------------------
    def _start_update_check(self) -> None:
        """Ask GitHub, on a worker thread, whether there is a newer build."""
        if not getattr(self.cfg, "update_check", False):
            return

        def work():
            try:
                found = updater.check(self.cfg.update_repo)
            except Exception:
                found = None            # never let a check break startup
            if found:
                self._updates.put(("available", found))

        threading.Thread(target=work, daemon=True).start()

    def _poll_updates(self) -> None:
        try:
            while True:
                kind, payload = self._updates.get_nowait()
                if kind == "available":
                    self._release = payload
                    self.update_button.set_text(f"Update to {payload.label}")
                    if not self.update_button.winfo_ismapped():
                        self.update_button.pack(side="right", padx=T.PAD_S)
                    self.log.info(f"Version {payload.version} is available "
                                  f"(running {__version__}).")
                elif kind == "progress":
                    self.update_button.set_text(f"Downloading {payload:.0%}")
                elif kind == "ready":
                    self._install_now(payload)
                elif kind == "failed":
                    self._updating = False
                    self.update_button.set_enabled(True)
                    self.update_button.set_text(
                        f"Update to {self._release.label}" if self._release
                        else "Update")
                    self.log.error(f"Update failed: {payload}")
        except queue.Empty:
            pass

    def _install_now(self, downloaded: str) -> None:
        """Swap in the new build and end this process. Main thread only."""
        self.update_button.set_text("Restarting...")
        try:
            self.log.info("Update verified. Handing over to the installer.")
            self.engine.stop()
            updater.stage_update(downloaded)
        except Exception as exc:
            self._updating = False
            self.update_button.set_enabled(True)
            self.log.error(f"Update could not be installed: {exc}")
            return

        self.log.info("Closing so the new version can replace this one.")
        self._cancel_refresh()
        try:
            self.destroy()
        except tk.TclError:
            pass
        # The swap cannot start until this process releases the file, so go
        # now rather than risk a thread holding the window open.
        updater.quit_now(0)

    def _do_update(self) -> None:
        """Download and install, but never in the middle of something."""
        from tkinter import messagebox

        if self._updating or not self._release:
            return

        snap = self.engine.snapshot()
        if self.engine.armed:
            messagebox.showwarning(
                "Not now",
                "The app is armed. Disarm before updating.", parent=self)
            return
        if snap is not None and snap.state == WORKING:
            messagebox.showwarning(
                "Not now",
                "An order is working. Wait for it to finish before updating.",
                parent=self)
            return

        ok, why = updater.can_install()
        if not ok:
            messagebox.showinfo("Update", why, parent=self)
            return

        notes = (self._release.notes or "").strip()
        if len(notes) > 700:
            notes = notes[:700] + "..."
        blank = os.linesep * 2
        if not messagebox.askyesno(
                "Update",
                f"Install version {self._release.version}?" + blank
                + (notes or "No release notes.") + blank
                + "The app will close and reopen on the new version.",
                parent=self):
            return

        self._updating = True
        self.update_button.set_enabled(False)
        self.engine.disarm("updating")

        def work():
            try:
                path = updater.download(
                    self._release,
                    progress=lambda f: self._updates.put(("progress", f)))
                # Hand back to the main thread. Ending the process from here
                # would only end this thread, leaving the executable locked
                # and the swap waiting forever.
                self._updates.put(("ready", path))
            except Exception as exc:
                self._updates.put(("failed", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    # ---- the roll limit ---------------------------------------------------
    @property
    def _bps_mode(self) -> bool:
        return self.cfg.limit_mode == "bps"

    def _current_tenor(self):
        """The tenor of the pair on screen, which is what the limit keys off."""
        detail = getattr(self._last_decision, "limit_detail", None)
        return detail.tenor_months if detail else None

    def _rungs_above(self, ceiling):
        """Rungs the ladder holds that a proposed ceiling would not allow."""
        if ceiling is None:
            return []
        ladder = getattr(self.engine, "ladder", None)
        return [f"{r.bps.normalize():f}" for r in (ladder.rungs if ladder else [])
                if r.bps > ceiling]

    def _reset_limit(self) -> None:
        if self._bps_mode:
            months = self._current_tenor()
            schedule = self.cfg.limit_bps_schedule or {}
            current = schedule.get(str(months)) if months else None
            self.limit_var.set(str(current) if current is not None else "")
            self.limit_unit.configure(text="bps")
        else:
            self.limit_var.set(money(self.cfg.roll_limit_d))
            self.limit_unit.configure(text="Rs")
        self._describe_limit()

    def _describe_limit(self) -> None:
        """Spell out what the limit works out to, in both units."""
        detail = getattr(self._last_decision, "limit_detail", None)
        if detail is None:
            self._limit_note("waiting for a quote", T.FAINT)
        else:
            self._limit_note(detail.describe(), T.FAINT)

    def _limit_note(self, text: str, colour: str) -> None:
        self.limit_note.configure(text=text, fg=colour)

    def _apply_limit(self) -> None:
        """Change the limit the rule compares against.

        The new value goes through the same validation as config.json, so a
        limit that would make the app unsafe is refused rather than accepted.
        Applying it always disarms: the operator must look at the new number
        against the live cost and arm again deliberately, instead of a typed
        digit firing a roll on the next tick.
        """
        from dataclasses import replace

        typed = self.limit_var.get().strip()
        if not typed:
            self._limit_note("enter a limit", T.DANGER)
            return

        if self._bps_mode:
            months = self._current_tenor()
            if months is None:
                self._limit_note("no tenor yet, so there is nothing to set",
                                 T.DANGER)
                return
            schedule = dict(self.cfg.limit_bps_schedule or {})
            old_text = schedule.get(str(months))
            schedule[str(months)] = typed
            changes = {"limit_bps_schedule": schedule}
            described = f"the {months} month limit from {old_text} to {typed} bps"
            unchanged = (old_text == typed)
        else:
            changes = {"roll_limit": typed}
            old_text = self.cfg.roll_limit
            described = f"the limit from {old_text} to {typed}"
            unchanged = (D(old_text) == D(typed)) if _numeric(typed) else False

        try:
            candidate = replace(self.cfg, **changes)
            candidate.validate()
        except Exception as exc:
            first = str(exc).splitlines()[-1].strip(" -")
            self._limit_note(first[:70], T.DANGER)
            return

        # Lowering the limit under a looser rung would leave that rung in force
        # for the rest of the session -- the ladder is built once at startup --
        # so the app would go on trading at the old, wider number against the
        # new instruction. Refuse, and say which rung is in the way, rather
        # than silently discarding a rung the operator put there.
        blocking = self._rungs_above(self.engine._tenor_ceiling_bps(candidate))
        if blocking:
            self._limit_note(
                f"the {blocking[0]} bps rung is above that; change it first",
                T.DANGER)
            return

        if unchanged:
            self._reset_limit()
            return

        was_armed = self.engine.armed
        self.engine.disarm("roll limit changed")
        for name, value in changes.items():
            setattr(self.cfg, name, value)

        self.log.info("Changed " + described
                      + (" (disarmed)" if was_armed else ""))

        if self.config_path:
            try:
                self.cfg.save(self.config_path)
                self._limit_note("saved", T.SUCCESS)
            except OSError as exc:
                self.log.warn(f"Could not save config.json: {exc}")
                self._limit_note("applied, not saved", T.WARN)
        else:
            self._limit_note("applied", T.SUCCESS)

        self.after(4000, self._describe_limit)

    def _change_contracts(self) -> None:
        self.engine.disarm("changing contracts")
        self.on_change_contracts()

    def _build_gates_and_log(self, parent) -> None:
        split = tk.PanedWindow(parent, orient="vertical", bg=T.BG, bd=0,
                               sashwidth=6, sashrelief="flat", showhandle=False)
        split.pack(fill="both", expand=True)

        gates_holder = T.card(split, padx=T.PAD_S, pady=T.PAD_S)
        split.add(gates_holder, minsize=120, stretch="always")
        gbox = gates_holder.inner

        head = tk.Frame(gbox, bg=T.SURFACE)
        head.pack(fill="x", pady=(0, T.PAD_XS))
        tk.Label(head, text="SAFETY GATES", bg=T.SURFACE, fg=T.FAINT,
                 font=self.fonts.label).pack(side="left", padx=T.PAD_XS)
        self.gate_summary = tk.Label(head, text="", bg=T.SURFACE, fg=T.MUTED,
                                     font=self.fonts.ui_small)
        self.gate_summary.pack(side="right", padx=T.PAD_XS)

        table = tk.Frame(gbox, bg=T.SURFACE)
        table.pack(fill="both", expand=True)
        self.gates = ttk.Treeview(table, style="App.Treeview", show="headings",
                                  columns=[c[0] for c in GATE_COLUMNS],
                                  selectmode="none")
        for key, title, width, anchor in GATE_COLUMNS:
            self.gates.heading(key, text=title, anchor="w")
            self.gates.column(key, width=width, anchor=anchor,
                              stretch=(key == "detail"))
        self.gates.pack(side="left", fill="both", expand=True)
        gbar = ttk.Scrollbar(table, orient="vertical", command=self.gates.yview,
                             style="App.Vertical.TScrollbar")
        gbar.pack(side="right", fill="y")
        self.gates.configure(yscrollcommand=gbar.set)
        self.gates.tag_configure("pass", foreground=T.SUCCESS)
        self.gates.tag_configure("fail", foreground=T.DANGER)

        log_holder = T.card(split, padx=T.PAD_S, pady=T.PAD_S)
        split.add(log_holder, minsize=96, stretch="always")
        lbox = log_holder.inner
        tk.Label(lbox, text="LOG", bg=T.SURFACE, fg=T.FAINT,
                 font=self.fonts.label, anchor="w").pack(
                     fill="x", padx=T.PAD_XS, pady=(0, T.PAD_XS))

        holder = tk.Frame(lbox, bg=T.SURFACE)
        holder.pack(fill="both", expand=True)
        lbar = ttk.Scrollbar(holder, orient="vertical",
                             style="App.Vertical.TScrollbar")
        lbar.pack(side="right", fill="y")
        self.log_box = tk.Text(holder, bg=T.SURFACE_2, fg=T.MUTED,
                               font=self.fonts.mono, bd=0, highlightthickness=0,
                               wrap="word", state="disabled", padx=T.PAD_M,
                               pady=T.PAD_S, height=4, yscrollcommand=lbar.set,
                               spacing1=1, spacing3=2)
        self.log_box.pack(side="left", fill="both", expand=True)
        lbar.configure(command=self.log_box.yview)
        self.log_box.tag_configure("INFO", foreground=T.TEXT)
        self.log_box.tag_configure("WARN", foreground=T.WARN)
        self.log_box.tag_configure("ERROR", foreground=T.DANGER)
        self.log_box.tag_configure("ALERT", foreground=T.DANGER)
        self.log_box.tag_configure("stamp", foreground=T.FAINT)

    # ---------------------------------------------------------------- refresh
    def _refresh(self) -> None:
        try:
            self.clock.configure(text=time.strftime("%H:%M:%S"))
            self._poll_updates()
            self._draw(self.engine.snapshot())
            self._draw_log()
        finally:
            if self.winfo_exists():
                self._refresh_id = self.after(200, self._refresh)

    def _draw(self, snap) -> None:
        if snap is None:
            return

        self.state_pill.set(snap.state.lower(),
                            STATE_COLOUR.get(snap.state, T.MUTED))
        self._refresh_mode(snap.dry_run)
        self._draw_ladder(snap.decision)

        source = snap.quote_source or "connecting"
        self.source_pill.set(source,
                             T.SUCCESS if source == "live feed" else T.WARN)
        self.note.configure(text=snap.halted_reason or snap.note,
                            fg=T.DANGER if snap.halted_reason else T.MUTED)
        self.halt_button.set_enabled(bool(snap.halted_reason))

        for key, info, quote in (("near", snap.near, snap.near_quote),
                                 ("far", snap.far, snap.far_quote)):
            self._draw_leg(self.leg_widgets[key], info, quote, key)

        self._draw_cost(snap)
        self._draw_gates(snap.report)

        if snap.armed_until:
            left = max(0, int(snap.armed_until - time.monotonic()))
            self.arm_timer.configure(text=f"armed  {left}s" if left else "")
            self.arm_button.set_text("ARMED")
        else:
            self.arm_timer.configure(text="")
            self.arm_button.set_text("ARM")

    def _draw_price(self, key: str, value, delta_label, price) -> None:
        """Show a price, tinted for a moment whenever it moves.

        The loop redraws several times per quote, so the flash is keyed off the
        price changing and a timestamp, not off the redraw. Without this the
        numbers update correctly but the panel looks frozen.
        """
        seen = self._ticks.get(key)
        now = time.monotonic()

        if seen is None:
            self._ticks[key] = (price, 0, 0.0, None)
        elif price != seen[0]:
            direction = 1 if price > seen[0] else -1
            self._ticks[key] = (price, direction, now, price - seen[0])

        _, direction, moved_at, change = self._ticks[key]
        lit = direction and (now - moved_at) < T.FLASH_SECONDS

        if lit:
            colour = T.UP if direction > 0 else T.DOWN
            tint = T.UP_TINT if direction > 0 else T.DOWN_TINT
            value.configure(text=money(price), fg=colour, bg=tint)
            delta_label.configure(
                text=f"{'+' if change > 0 else ''}{money(change)}", fg=colour)
        else:
            value.configure(text=money(price), fg=T.TEXT, bg=T.SURFACE)
            delta_label.configure(text="", fg=T.SURFACE)

    def _draw_delta(self, key: str, label, value) -> None:
        """Show how much a number just moved, then let it fade."""
        seen = self._ticks.get(key)
        now = time.monotonic()
        if seen is None:
            self._ticks[key] = (value, 0, 0.0, None)
        elif value != seen[0]:
            self._ticks[key] = (value, 1 if value > seen[0] else -1, now,
                                value - seen[0])

        _, direction, moved_at, change = self._ticks[key]
        if direction and (now - moved_at) < T.FLASH_SECONDS:
            label.configure(text=f"{'+' if change > 0 else ''}{money(change)}",
                            fg=T.UP if direction > 0 else T.DOWN)
        else:
            label.configure(text="", fg=T.SURFACE)

    def _draw_leg(self, cells, info, quote, key: str) -> None:
        if info is None:
            cells["name"].configure(text="--")
            cells["meta"].configure(text="")
        else:
            cells["name"].configure(text=info.sec_desc or info.symbol or info.token)
            bits = [f"token {info.token}"]
            if info.expiry:
                from datetime import date as _date
                left = (info.expiry - _date.today()).days
                bits.append(f"expires {info.expiry} ({left}d)")
            bits.append(f"lot {info.lot_size}")
            cells["meta"].configure(text="   ".join(bits))

        if quote is None:
            for field in ("bid", "ask"):
                cells[field].configure(text="--", fg=T.FAINT, bg=T.SURFACE)
                cells[field + "_delta"].configure(text="")
                cells[field + "_size"].configure(text="")
            cells["age"].configure(text="no quote", fg=T.DANGER)
            return

        for field, price, size in (("bid", quote.bid, quote.bid_qty),
                                   ("ask", quote.ask, quote.ask_qty)):
            self._draw_price(f"{key}_{field}", cells[field],
                             cells[field + "_delta"], price)
            if size is None:
                cells[field + "_size"].configure(text="")
            else:
                enough = size >= self.cfg.clip_qty
                cells[field + "_size"].configure(
                    text=f"{size:,} resting", fg=T.MUTED if enough else T.WARN)

        # On the websocket the quote is always current, so what is worth
        # showing is when the book last moved. On the polled fallback it is how
        # old the snapshot is, which is the thing to worry about.
        moved = quote.since_move()
        if moved is not None:
            cells["age"].configure(text=f"moved {moved:.1f}s ago", fg=T.FAINT)
        else:
            age = quote.age()
            fresh = age <= self.cfg.max_quote_age_sec
            cells["age"].configure(text=f"read {age:.1f}s ago",
                                   fg=T.FAINT if fresh else T.DANGER)

    def _draw_cost(self, snap) -> None:
        dec = snap.decision
        if dec is None:
            self.cost.configure(text="--", fg=T.FAINT)
            self.cost_delta.configure(text="", fg=T.SURFACE)
            self.cost_rupees.configure(text="")
            self._last_decision = None
            self.verdict.configure(text="NO DATA", fg=T.DANGER)
            self.verdict_note.configure(text="")
            for label in self.stat_labels.values():
                label.configure(text="--")
            return

        ready = snap.report.ok if snap.report else False
        # The cost keeps its own colour, which says whether it qualifies. The
        # movement is shown beside it instead, so the two never fight.
        self.cost.configure(text=money(dec.roll_cost),
                            fg=T.SUCCESS if dec.qualifies else T.TEXT)
        self._draw_delta("roll_cost", self.cost_delta, dec.roll_cost)

        # Basis points are the unit the roll is actually discussed in, so the
        # cost is shown in both.
        bps = dec.cost_bps
        self.cost_rupees.configure(
            text=(f"{money(bps, 1)} bps" if bps is not None else "")
                 + f"     Rs {money(dec.cost_per_lot, 2)} for {self.cfg.lots} lot"
                 + ("s" if self.cfg.lots != 1 else ""))

        first = self._last_decision is None
        self._last_decision = dec
        if first:
            self._reset_limit()
        else:
            self._describe_limit()

        self.stat_labels["worst"].configure(text=money(dec.worst_case))
        self.stat_labels["sell"].configure(text=money(dec.sell_limit))
        self.stat_labels["buy"].configure(text=money(dec.buy_limit))
        self.stat_labels["qty"].configure(text=f"{dec.qty:,}")

        if ready:
            self.verdict.configure(text="READY", fg=T.SUCCESS)
            self.verdict_note.configure(text="every gate passes")
        elif dec.qualifies:
            failing = len(snap.report.failures) if snap.report else 0
            self.verdict.configure(text="BELOW LIMIT", fg=T.WARN)
            self.verdict_note.configure(
                text=f"but {failing} gate{'s' if failing != 1 else ''} still blocking")
        else:
            self.verdict.configure(text="WAITING", fg=T.MUTED)
            over = None
            if dec.cost_bps is not None and dec.limit_bps is not None:
                over = dec.cost_bps - dec.limit_bps
            self.verdict_note.configure(
                text=(f"{money(over, 1)} bps too dear" if over and over > 0
                      else "cost is not below the limit"))

    def _draw_gates(self, report) -> None:
        self.gates.delete(*self.gates.get_children())
        if report is None:
            self.gate_summary.configure(text="")
            self.gates.insert("", "end", values=("", "waiting", "no gate report yet"))
            return

        failures = report.failures
        passed = len(report.gates) - len(failures)
        self.gate_summary.configure(
            text=f"{passed} of {len(report.gates)} passing",
            fg=T.SUCCESS if not failures else T.WARN)

        # Failures first: when something is blocking, that is the whole question.
        for gate in failures + [g for g in report.gates if g.ok]:
            self.gates.insert("", "end", tags=("pass" if gate.ok else "fail",),
                              values=("PASS" if gate.ok else "BLOCK",
                                      gate.name, gate.detail))

    def _draw_log(self) -> None:
        entries = self.log.recent(300)
        if len(entries) == self._last_log_count:
            return
        self._last_log_count = len(entries)

        # Anything that halts, or the cost finally clearing, is worth hearing.
        # The operator is not necessarily looking at the screen.
        alerts = sum(1 for _, level, _ in entries if level == "ALERT")
        if alerts > self._alerts_seen:
            newest = next((m for _, lvl, m in reversed(entries) if lvl == "ALERT"), "")
            if "come below the limit" in newest:
                notify.chime()
            else:
                notify.alarm()
        self._alerts_seen = alerts
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        for stamp, level, message in entries:
            self.log_box.insert("end", f"{stamp[11:]}  ", "stamp")
            self.log_box.insert("end", f"{message}\n", level)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _cancel_refresh(self) -> None:
        """Stop the redraw timer before the widgets go away.

        Without this the pending callback fires against a destroyed window. On
        the update path os._exit usually wins that race, but relying on a race
        is not a reason to leave the error there.
        """
        if self._refresh_id is not None:
            try:
                self.after_cancel(self._refresh_id)
            except tk.TclError:
                pass
            self._refresh_id = None

    def _on_close(self) -> None:
        """Do not walk away from live orders.

        A Day order outlives this process at the exchange, and the app-side
        synthetic IOC that would have cancelled it dies with the process. So
        closing cancels first, and says so if anything was left in doubt.
        """
        from tkinter import messagebox

        problem = None
        try:
            problem = self.engine.shutdown()
        except Exception as exc:                 # never trap the operator
            problem = f"closing down cleanly failed: {exc}. Check the terminal."

        if problem:
            messagebox.showwarning("Orders may still be live", problem, parent=self)

        self._cancel_refresh()
        self.destroy()

    def _cancel_all(self) -> None:
        from tkinter import messagebox

        if self.cfg.dry_run:
            messagebox.showinfo(
                "Cancel all",
                "Nothing has been sent, so there is nothing to cancel.",
                parent=self)
            return

        if not messagebox.askyesno(
                "Cancel all",
                "Cancel every order still working on either leg?" + os.linesep * 2
                + "This does not close any position you already hold.",
                parent=self, default="no"):
            return

        self.engine.disarm("cancel all")
        cancelled, failed, unreadable = self.engine.cancel_all()
        if unreadable:
            messagebox.showwarning("Cancel all", unreadable, parent=self)
        elif failed:
            messagebox.showwarning(
                "Cancel all",
                f"Cancelled {cancelled}, but {failed} could not be cancelled and "
                "may still be live. Check the terminal.", parent=self)
        else:
            messagebox.showinfo(
                "Cancel all",
                f"Cancelled {cancelled} working order(s)." if cancelled
                else "Nothing was working.", parent=self)
