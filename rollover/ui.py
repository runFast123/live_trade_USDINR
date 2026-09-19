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
from . import __version__, editing, livemode, notify, updater
from .config import section_key
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


def _sortable(value):
    """A missing figure sorts last, not first: "--" is not the cheapest."""
    from decimal import Decimal
    return Decimal("999999") if value is None else value


def _inside_bps(view):
    """How far inside its own limit a section is. Missing sorts last."""
    from decimal import Decimal
    decision = view.decision
    cost = getattr(decision, "cost_bps", None)
    limit = getattr(decision, "limit_bps", None)
    if cost is None or limit is None:
        return Decimal("-999999")
    return limit - cost


def _why_waiting(gate) -> str:
    """Why a section is not trading, in words that are not the gate's name.

    The strip used to print the failing gate's NAME. So a market that was
    shut read as "market open", and a position that was missing read as
    "position" -- the gate names are labels for a row, not statements, and as
    statements the first one says the opposite of the truth. Every gate's
    detail is already written to stand on its own.
    """
    detail = (getattr(gate, "detail", "") or "").strip()
    name = (getattr(gate, "name", "") or "").strip()
    if not detail:
        return name or "waiting"
    # A detail that opens with a number is a measurement -- "0.1250 (max
    # 0.0500)" -- and a measurement has to say what was measured. Next to the
    # gate's own name in the table below it is clear; alone in a column it
    # names nothing at all.
    if name and not detail[0].isalpha():
        return f"{name}: {detail}"
    return detail


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
        self._last_log_seq = -1
        self._alerts_seen = 0          # so a new ALERT can be heard, not just seen
        self._updates: "queue.Queue[tuple]" = queue.Queue()
        # Last seen price per cell, so a change can be shown rather than just
        # rendered: {cell key: (price, direction, moved_at, delta)}
        self._ticks: dict = {}
        self._release = None
        self._updating = False
        self._last_snapshot = None
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
        self._build_strip(root)
        self._build_legs(root)
        self._build_cost(root)
        self._build_ladder(root)
        self._build_actions(root)
        self._build_gates_and_log(root)

    def _build_header(self, parent) -> None:
        bar = tk.Frame(parent, bg=T.BG)
        bar.pack(fill="x", pady=(0, self.gap))
        self.header_bar = bar

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

        T.Button(right, "?", self._show_help, self.fonts,
                 width=30, height=30).pack(side="right", padx=(0, T.PAD_S))

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

        # Wrapped, not widened: this label sits in a row with the worst case
        # and the buy limit, and letting it grow pushed the buy limit off the
        # right edge of the card.
        self.limit_note = tk.Label(limit_box, text="", bg=T.SURFACE,
                                   fg=T.FAINT, font=self.fonts.ui_small,
                                   anchor="w", justify="left", wraplength=280)
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

    # How many cards sit side by side before wrapping to the next line.
    # ---- the sections, and which one the cards below are showing -----------
    STRIP_COLUMNS = (
        ("name", "Section", 190, "w"),
        ("legs", "Near into far", 220, "w"),
        ("cost", "Roll cost", 110, "e"),
        ("limit", "Limit", 90, "e"),
        ("gap", "Distance", 100, "e"),
        ("rolled", "Rolled", 150, "e"),
        ("state", "Waiting for", 240, "w"),
    )

    def _build_strip(self, parent) -> None:
        """One row per section, so several can be compared at a glance.

        The cards below show one section in full. Stacking every section's
        cards would put the second one off the bottom of the screen, which is
        no way to compare them; a row each, with the detail for whichever is
        selected, keeps both the comparison and the detail available.
        """
        self.strip_card = T.card(parent, padx=T.PAD_L, pady=self.gap)
        box = self.strip_card.inner

        head = tk.Frame(box, bg=T.SURFACE)
        head.pack(fill="x")
        tk.Label(head, text="SECTIONS", bg=T.SURFACE, fg=T.FAINT,
                 font=self.fonts.label, anchor="w").pack(side="left")
        self.strip_note = tk.Label(head, text="", bg=T.SURFACE, fg=T.MUTED,
                                   font=self.fonts.ui_small, anchor="e")
        self.strip_note.pack(side="right")

        self.strip = ttk.Treeview(
            box, columns=[c[0] for c in self.STRIP_COLUMNS],
            show="headings", height=3, selectmode="browse")
        for key, title, width, anchor in self.STRIP_COLUMNS:
            # Click a heading to sort by it, again to reverse. With several
            # rolls the question is usually "which is cheapest" or "which has
            # most left", and both are a column already on screen.
            self.strip.heading(key, text=title,
                               command=lambda k=key: self._sort_strip(k))
            self.strip.column(key, width=width, anchor=anchor,
                              stretch=(key == "state"))
        self.strip.pack(fill="x", pady=(T.PAD_S, 0))
        self.strip.bind("<<TreeviewSelect>>", lambda _e: self._focus_selected())
        # Double-click switches a section on or off: it is the thing an
        # operator does most, and hunting for the button each time is friction
        # on a screen they check every twenty minutes.
        self.strip.bind("<Double-1>", self._strip_double_click)

        self.strip.tag_configure("ready", foreground=T.SUCCESS)
        self.strip.tag_configure("halted", foreground=T.DANGER)
        self.strip.tag_configure("off", foreground=T.FAINT)
        self.strip.tag_configure("waiting", foreground=T.MUTED)
        self.strip.tag_configure("next", background=T.SURFACE_2)

        # None means the order they are configured in, which is the default.
        self._sort_by = None
        self._sort_down = False

        row = tk.Frame(box, bg=T.SURFACE)
        row.pack(fill="x", pady=(T.PAD_M, 0))
        T.Button(row, "Add section", self._add_section, self.fonts,
                 width=140, height=30).pack(side="left")
        self.enable_button = T.Button(row, "Enable", self._toggle_section,
                                      self.fonts, width=190, height=30)
        self.enable_button.pack(side="left", padx=T.PAD_XS)
        self.relegs_button = T.Button(row, "Change contracts",
                                      self._change_contracts, self.fonts,
                                      width=230, height=30)
        self.relegs_button.pack(side="left")
        self.remove_button = T.Button(row, "Remove", self._remove_section,
                                      self.fonts, kind="danger",
                                      width=190, height=30)
        self.remove_button.pack(side="left", padx=T.PAD_XS)
        # Under the buttons, not beside them: beside them it had 520 pixels
        # and a refusal that runs to two sentences was cut off mid-word.
        self.section_note = tk.Label(box, text="", bg=T.SURFACE, fg=T.FAINT,
                                     font=self.fonts.ui_small, anchor="w",
                                     justify="left", wraplength=1100)
        self.section_note.pack(fill="x", pady=(T.PAD_S, 0))

        self.allocation_label = tk.Label(
            box, text="", bg=T.SURFACE, fg=T.MUTED, font=self.fonts.ui_small,
            anchor="w", justify="left", wraplength=1000)
        self.allocation_label.pack(fill="x", pady=(T.PAD_S, 0))

        self._strip_shown = False
        self.focus_key = None
        # Tree row id -> section key. The row text is for people to
        # read, not for the program to identify a section by.
        self._strip_keys = {}

    def _focused_section(self):
        """The engine's Section for whatever the strip has selected."""
        view = self._focused()
        if view is not None:
            for section in self.engine.sections:
                if section.key == view.key:
                    return section
        return self.engine.sections[0] if self.engine.sections else None

    # ---- working the strip -------------------------------------------------
    SORT_KEYS = {
        "name": lambda v: (v.name or "").lower(),
        "legs": lambda v: ((v.near.sec_desc if v.near else ""),
                           (v.far.sec_desc if v.far else "")),
        "cost": lambda v: _sortable(getattr(v.decision, "cost_bps", None)),
        "limit": lambda v: _sortable(getattr(v.decision, "limit_bps", None)),
        "gap": lambda v: -_inside_bps(v),          # furthest inside first
        "rolled": lambda v: -(v.done or 0),
        "state": lambda v: (0 if not v.enabled else
                            (1 if v.halted_reason else 2), v.name or ""),
    }

    def _sort_strip(self, key: str) -> None:
        """Sort by a column, and reverse it on a second click.

        Sorting only changes the order they are listed in. Which section
        rolls next is decided in the engine and shown as a mark on the row,
        so re-ordering the table cannot change what trades.
        """
        if key not in self.SORT_KEYS:
            return
        if self._sort_by == key:
            if self._sort_down:
                self._sort_by, self._sort_down = None, False   # back to config
            else:
                self._sort_down = True
        else:
            self._sort_by, self._sort_down = key, False
        self._draw_strip(self._last_snapshot) if self._last_snapshot else None

    def _ordered(self, views):
        if self._sort_by is None:
            return list(views)
        try:
            out = sorted(views, key=self.SORT_KEYS[self._sort_by])
        except Exception:
            return list(views)
        return list(reversed(out)) if self._sort_down else out

    def _strip_double_click(self, event) -> None:
        """Switch the row under the pointer on or off."""
        if self.strip.identify_region(event.x, event.y) == "heading":
            return
        item = self.strip.identify_row(event.y)
        if not item:
            return
        self.strip.selection_set(item)
        self._focus_selected()
        self._toggle_section()
        return "break"

    def _busy(self) -> bool:
        """True, with a note, when a clip is working and must not be disturbed.

        Changing the sections rebuilds them, and a clip already in flight is
        holding the OLD section object. Its fill is credited to that object,
        which is no longer the one the engine runs or the one written to the
        state file -- so the quantity rolls at the exchange and disappears
        from the record, and the campaign rolls it again. Measured: 4,000
        credited to an orphan while the live sections showed nothing.

        The limit editor has always refused for the same reason. These did
        not.
        """
        if not getattr(self.engine.account, "in_flight", False):
            return False
        self._section_note(
            "an order is working; wait for it to finish. Changing the "
            "sections now would lose what it fills from the record.", T.WARN)
        return True

    def _section_note(self, text: str, colour: str) -> None:
        self.section_note.configure(text=text, fg=colour)

    def _views(self):
        snap = self._last_snapshot
        return list(getattr(snap, "sections", None) or []) if snap else []

    def _focused(self):
        """The section whose cards are on screen, or the first one."""
        views = self._views()
        if not views:
            return None
        for view in views:
            if view.key == self.focus_key:
                return view
        return views[0]

    def _focus_selected(self) -> None:
        """Follow the selected row to its section.

        Through an explicit row -> key map. It used to match the displayed
        name against the section name, which quietly stopped working the
        moment the cell gained a marker for which section rolls next: nothing
        raised, the selection simply stopped doing anything.
        """
        picked = self.strip.selection()
        if not picked:
            return
        key = self._strip_keys.get(picked[0])
        if key is None or key == self.focus_key:
            self._update_enable_button()
            return
        if key in {v.key for v in self._views()}:
            self.focus_key = key
            self._rebuild_rung_rows()
            self._reset_limit()
            # Whatever was last said was about the section that was selected
            # when it was said. Leaving it up next to another section's name
            # reads as a complaint about that one.
            self._section_note("", T.FAINT)
        self._update_enable_button()

    def _update_enable_button(self) -> None:
        """Name the section every control is about to act on.

        With several rolls on screen, "Remove section" does not say which one
        it removes, and the limits card does not say whose limits it is
        showing. Both act on the row selected above, and neither said so.
        """
        view = self._focused()
        if view is None:
            return
        several = len(self._views()) > 1
        who = f" {view.name}" if several else ""
        self.enable_button.set_text(
            ("Disable" if view.enabled else "Enable") + who)
        self.remove_button.set_text("Remove" + who)
        self.relegs_button.set_text(
            ("Change contracts" + (" of" + who if several else "")))
        if getattr(self, "ladder_heading", None) is not None:
            self.ladder_heading.configure(
                text="ROLL COST AT EACH LIMIT"
                     + (f"   {view.name.upper()}" if several else ""))

    def _draw_strip(self, snap) -> None:
        """The rows, and the controls that change what the rows are.

        Always on screen. It used to hide itself with one roll, to keep the
        window looking as it had before sections existed -- but Add section
        lives in this card, so hiding it meant the only way to reach a second
        section was a button that appeared once you already had two. A person
        looking at this window could not get there from here.
        """
        views = list(getattr(snap, "sections", None) or [])
        if not self._strip_shown:
            self.strip_card.pack(fill="x", pady=self.gap, after=self.header_bar)
            self._strip_shown = True
        # No blank rows under a single roll, and no scrolling under four.
        self.strip.configure(height=max(1, min(len(views) or 1, 6)))

        focused = self._focused()
        ordered = self._ordered(views)
        next_key = getattr(snap, "next_key", None)
        self.strip.delete(*self.strip.get_children())
        self._strip_keys = {}
        for view in ordered:
            decision = view.decision
            cost = (f"{money(decision.cost_bps, 1)} bps"
                    if decision is not None and decision.cost_bps is not None
                    else "--")
            limit = (f"{money(decision.limit_bps, 0)} bps"
                     if decision is not None and decision.limit_bps is not None
                     else "--")
            gap = "--"
            if decision is not None and decision.cost_bps is not None \
                    and decision.limit_bps is not None:
                inside = decision.limit_bps - decision.cost_bps
                gap = (f"{money(abs(inside), 1)} bps "
                       + ("inside" if inside > 0 else "away"))

            if not view.enabled:
                tag, state = "off", "disabled"
            elif view.halted_reason:
                tag, state = "halted", "HALTED"
            elif decision is not None and decision.qualifies and \
                    view.report is not None and view.report.ok:
                tag, state = "ready", "READY"
            elif view.report is not None and view.report.failures:
                tag, state = "waiting", _why_waiting(view.report.failures[0])
            else:
                tag, state = "waiting", "watching"

            rolled = ("--" if view.allocated is None
                      else f"{view.done:,} / {view.allocated:,}")
            legs = "--"
            if view.near is not None and view.far is not None:
                legs = f"{view.near.sec_desc} -> {view.far.sec_desc}"

            # Which section would go first if a roll fired now. Decided by
            # the engine, never worked out again here, so the mark and the
            # section actually chosen cannot drift apart.
            is_next = next_key is not None and view.key == next_key
            name = ("> " + view.name) if is_next else ("   " + view.name)
            tags = [tag, view.key] + (["next"] if is_next else [])

            item = self.strip.insert(
                "", "end", values=(name, legs, cost, limit, gap, rolled, state),
                tags=tuple(tags))
            self._strip_keys[item] = view.key

        # Keep the selection on the focused section without re-entering.
        children = self.strip.get_children()
        if focused is not None:
            for index, view in enumerate(ordered):
                if view.key == focused.key and index < len(children):
                    if self.strip.selection() != (children[index],):
                        self.strip.selection_set(children[index])
                    break

        if len(views) > 1:
            how = "click a row to work on it, double-click to switch it on or off"
            if next_key:
                how = "> is ready and would roll first.  " + how
            if self._sort_by:
                how += (f"   sorted by {self._sort_by}"
                        + (", reversed" if self._sort_down else ""))
            note = f"{how} -- showing {focused.name} below" if focused else how
        else:
            # With one roll there is nothing to choose between, so the note
            # says what the card is for instead.
            note = "Add section to watch another pair of contracts alongside"
        self.strip_note.configure(text=note)
        # Every section switched off is a silent do-nothing: the app watches,
        # the gates pass, and no roll can ever fire. Say so where the reason
        # for not trading is read.
        if views and not any(v.enabled for v in views):
            self.allocation_label.configure(
                text="Nothing will trade: every section is switched off. "
                     "Select one above and press Enable.", fg=T.WARN)
        else:
            self.allocation_label.configure(
                text=(snap.allocation_refusal or snap.allocation or ""),
                fg=T.DANGER if snap.allocation_refusal else T.MUTED)
        self._update_enable_button()

    # ---- changing the sections ---------------------------------------------
    def _pick_contracts(self, title, apply):
        from .picker import ContractWindow

        window = ContractWindow(self, self.cfg, self.log, self.engine.broker,
                                self.config_path, apply=apply, title=title)
        self.wait_window(window)
        return bool(getattr(window, "ok", False))

    def _add_section(self) -> None:

        if self._busy():
            return
        made = {}

        def accept(near_row, far_row):
            sections = editing.add(self.cfg, near_row, far_row)
            editing.apply(self.cfg, sections, self.config_path)
            made["key"] = section_key(near_row.get("Token"),
                                      far_row.get("Token"))
            made["name"] = sections[-1].get("name", "the new section")

        if not self._pick_contracts("Add a section: choose its two contracts",
                                    accept):
            return
        # Focus it, so the limits card below is editing the thing just made
        # rather than the section that happened to be selected before.
        self._restart_engine(focus_key=made.get("key"))
        self._section_note(
            f"{made.get('name', 'Added')} added and switched off. Its limits "
            "are below: set them, then press Enable.", T.WARN)
        self.log.info(f"Section added: {made.get('name')}. "
                      f"{len(self.cfg.sections)} configured.")

    def _remove_section(self) -> None:

        if self._busy():
            return
        from tkinter import messagebox

        view = self._focused()
        if view is None:
            return
        if not messagebox.askyesno(
                "Remove section",
                f"Remove {view.name}?" + os.linesep * 2
                + "This stops it rolling and forgets what it has rolled so "
                  "far. It changes nothing at the exchange.",
                parent=self, default="no"):
            return
        try:
            sections = editing.remove(self.cfg, view.key)
        except editing.EditError as exc:
            self._section_note(str(exc), T.DANGER)
            return
        editing.apply(self.cfg, sections, self.config_path)
        self.log.warn(f"Section removed: {view.name}")
        self._restart_engine()
        self._section_note(f"{view.name} removed.", T.MUTED)

    def _toggle_section(self) -> None:

        if self._busy():
            return
        view = self._focused()
        if view is None:
            return
        if not view.enabled:
            # Asked in operator's words before the config layer refuses in
            # the file's words. Same rule, a sentence you can act on.
            why = editing.why_not_enable(self.cfg, view.key)
            if why:
                self._section_note(why, T.DANGER)
                return
        try:
            sections = editing.enable(self.cfg, view.key, not view.enabled)
        except editing.EditError as exc:
            self._section_note(str(exc), T.DANGER)
            return
        editing.apply(self.cfg, sections, self.config_path)
        self.log.info(f"{view.name} "
                      f"{'disabled' if view.enabled else 'enabled'}.")
        self._restart_engine(focus_key=view.key)
        self._section_note(
            f"{view.name} {'disabled' if view.enabled else 'enabled'}.",
            T.MUTED)

    def _restart_engine(self, focus_key=None) -> None:
        """Put a change to the sections into force.

        This used to call on_change_contracts, which opens the contract
        picker. So adding a section opened a SECOND picker straight after the
        first, and the engine was never told anything had changed -- it went
        on running the list it started with, and the strip went on drawing it.
        Every button then acted on a section that was no longer there.

        Always disarms: the set of things that could trade has changed, and
        whoever armed was authorising the old set.
        """
        self.engine.disarm("sections changed")
        self.engine.reload_sections()
        # Show whatever the operator just acted on, or fall back to the first.
        views = [s.key for s in self.engine.sections]
        self.focus_key = focus_key if focus_key in views else (
            self.focus_key if self.focus_key in views else None)
        # Draw FIRST. The limit cards are rebuilt from whichever section the
        # snapshot says is on screen, so rebuilding them against the old
        # snapshot filled them from the old section -- and Set limits then
        # wrote those limits into the new section's ladder.
        self._draw(self.engine.snapshot())
        self._rebuild_rung_rows()
        self._reset_limit()

    LADDER_COLUMNS = 4

    def _build_ladder(self, parent) -> None:
        """A roll cost card per limit, so several can be read at once.

        The single ROLL COST card answers "where is the market against ONE
        limit". An operator willing to do ten thousand at thirty basis points
        and twenty at fifty needs that question answered for each of them at
        the same time, side by side, rather than by retyping one box and
        losing the previous answer.

        Each card carries the same figures the main one does -- what the limit
        is worth in rupees, the far ask that would satisfy it, how far away the
        market is -- plus what it would trade and how much of it is done. A
        card with no quantity is a watch line: compared, never traded.
        """
        self.ladder_card = T.card(parent, padx=T.PAD_L, pady=self.gap)
        box = self.ladder_card.inner

        head = tk.Frame(box, bg=T.SURFACE)
        head.pack(fill="x")
        self.ladder_heading = tk.Label(
            head, text="ROLL COST AT EACH LIMIT", bg=T.SURFACE, fg=T.FAINT,
            font=self.fonts.label, anchor="w")
        self.ladder_heading.pack(side="left")
        self.ladder_total = tk.Label(head, text="", bg=T.SURFACE, fg=T.MUTED,
                                     font=self.fonts.ui_small, anchor="e")
        self.ladder_total.pack(side="right")

        self.ladder_grid = tk.Frame(box, bg=T.SURFACE)
        self.ladder_grid.pack(fill="x", pady=(T.PAD_S, 0))
        for column in range(self.LADDER_COLUMNS):
            self.ladder_grid.columnconfigure(column, weight=1, uniform="rung")

        controls = tk.Frame(box, bg=T.SURFACE)
        controls.pack(fill="x", pady=(T.PAD_M, 0))
        T.Button(controls, "Add limit", self._add_rung, self.fonts,
                 width=110, height=30).pack(side="left")
        T.Button(controls, "Set limits", self._apply_ladder, self.fonts,
                 kind="primary", width=110, height=30).pack(side="left",
                                                            padx=T.PAD_XS)
        # Wrapped, not truncated: a refusal that stops mid-sentence tells the
        # operator a limit is wrong without telling them what to do about it.
        self.ladder_note = tk.Label(controls, text="", bg=T.SURFACE, fg=T.FAINT,
                                    font=self.fonts.ui_small, anchor="w",
                                    justify="left", wraplength=620)
        self.ladder_note.pack(side="left", padx=T.PAD_S, fill="x", expand=True)

        self.ladder_empty = tk.Label(
            box, bg=T.SURFACE, fg=T.MUTED, font=self.fonts.ui_small,
            anchor="w", justify="left", wraplength=1000,
            text=("No limits set, so this roll uses the single roll limit "
                  "above and rolls the whole position one clip at a time."
                  + os.linesep +
                  "Add limit makes a card here: a limit with a quantity is "
                  "traded at that limit and no looser, and several of them "
                  "add up. Leave the quantity blank to price and compare a "
                  "limit without ever trading it."))

        self.rung_rows = []
        self._ladder_shown = False
        self._ladder_empty_shown = False
        self._rebuild_rung_rows()

    def _draw_ladder_invitation(self) -> None:
        """Say what the empty card is for, rather than showing two buttons.

        Tracked with a flag rather than winfo_ismapped(), which still reads
        false immediately after a pack and would have this re-pack -- and so
        re-order the card against ladder_grid -- on every tick.
        """
        want = not self.rung_rows
        if want == self._ladder_empty_shown:
            return
        self._ladder_empty_shown = want
        if want:
            self.ladder_empty.pack(fill="x", pady=(T.PAD_S, 0),
                                   before=self.ladder_grid)
        else:
            self.ladder_empty.pack_forget()

    # ---- one card per limit -----------------------------------------------
    def _rebuild_rung_rows(self) -> None:
        """One card per limit the engine is actually using."""
        for row in list(self.rung_rows):
            self._forget_row(row)
        self.rung_rows = []

        section = self._focused_section()
        ladder = getattr(section, "ladder", None)
        for rung in (ladder.rungs if ladder else []):
            self._add_row(f"{rung.bps.normalize():f}", str(rung.qty), rung.key)

        for bps in (getattr(section, "watch_limits", None) or []):
            key = f"{bps.normalize():f}"
            self._add_row(key, "", key, watch=True)
        self._reflow_rows()

    def _forget_row(self, row) -> None:
        row["card"].destroy()

    def _add_row(self, bps: str = "", qty: str = "", key=None,
                 watch: bool = False) -> None:
        card = T.card(self.ladder_grid, padx=T.PAD_M, pady=T.PAD_S)
        inner = card.inner
        row = {"card": card, "key": key, "watch": watch,
               "bps": tk.StringVar(value=bps), "qty": tk.StringVar(value=qty),
               "cells": {}}

        # ---- the limit, big and editable, the way the main card has it ----
        top = tk.Frame(inner, bg=T.SURFACE)
        top.pack(fill="x")
        tk.Label(top, text="LIMIT", bg=T.SURFACE, fg=T.FAINT,
                 font=self.fonts.label, anchor="w").pack(side="left")
        row["remove"] = T.Button(top, "x", lambda r=row: self._remove_rung(r),
                                 self.fonts, width=24, height=20)
        row["remove"].pack(side="right")

        entry_row = tk.Frame(inner, bg=T.SURFACE)
        entry_row.pack(fill="x", pady=(2, 0))
        row["bps_box"] = T.entry(entry_row, self.fonts, textvariable=row["bps"],
                                 width=5)
        row["bps_box"].pack(side="left", ipady=3)
        tk.Label(entry_row, text="bps", bg=T.SURFACE, fg=T.MUTED,
                 font=self.fonts.ui_small).pack(side="left", padx=(T.PAD_XS, 0))
        row["cells"]["rupees"] = tk.Label(entry_row, text="--", bg=T.SURFACE,
                                          fg=T.TEXT, font=self.fonts.mono_medium,
                                          anchor="e")
        row["cells"]["rupees"].pack(side="right")

        # ---- the verdict, which is what the eye goes to --------------------
        row["cells"]["status"] = tk.Label(
            inner, text="--", bg=T.SURFACE, fg=T.MUTED,
            font=self.fonts.mono_medium, anchor="w")
        row["cells"]["status"].pack(fill="x", pady=(T.PAD_S, 0))

        row["cells"]["target"] = tk.Label(
            inner, text="", bg=T.SURFACE, fg=T.FAINT,
            font=self.fonts.ui_small, anchor="w")
        row["cells"]["target"].pack(fill="x")

        row["cells"]["gap"] = tk.Label(inner, text="", bg=T.SURFACE, fg=T.FAINT,
                                       font=self.fonts.ui_small, anchor="w")
        row["cells"]["gap"].pack(fill="x")

        tk.Frame(inner, bg=T.BORDER, height=1).pack(fill="x", pady=T.PAD_S)

        # ---- what it would trade ------------------------------------------
        size_row = tk.Frame(inner, bg=T.SURFACE)
        size_row.pack(fill="x")
        tk.Label(size_row, text="QTY", bg=T.SURFACE, fg=T.FAINT,
                 font=self.fonts.label).pack(side="left")
        row["qty_box"] = T.entry(size_row, self.fonts, textvariable=row["qty"],
                                 width=8)
        row["qty_box"].pack(side="right", ipady=2)

        row["cells"]["rolled"] = tk.Label(inner, text="", bg=T.SURFACE,
                                          fg=T.FAINT, font=self.fonts.ui_small,
                                          anchor="w")
        row["cells"]["rolled"].pack(fill="x", pady=(2, 0))

        for box in (row["bps_box"], row["qty_box"]):
            box.bind("<Return>", lambda _e: self._apply_ladder())
            box.bind("<Escape>", lambda _e: self._rebuild_rung_rows())

        self.rung_rows.append(row)

    def _reflow_rows(self) -> None:
        """Lay the cards out, wrapping onto further lines as they are added."""
        if getattr(self, "ladder_empty", None) is not None:
            self._draw_ladder_invitation()
        for index, row in enumerate(self.rung_rows):
            row["card"].grid(row=index // self.LADDER_COLUMNS,
                             column=index % self.LADDER_COLUMNS,
                             sticky="nsew", padx=(0, T.PAD_S),
                             pady=(0, T.PAD_S))

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
        self._ladder_note("removed; press Set limits to apply", T.WARN)

    def _ladder_note(self, text: str, colour: str) -> None:
        self.ladder_note.configure(text=text, fg=colour)

    def _show_ladder(self) -> None:
        if not self._ladder_shown:
            self.ladder_card.pack(fill="x", pady=self.gap,
                                  before=self.actions_bar)
            self._ladder_shown = True

    # ---- applying ---------------------------------------------------------
    def _typed_ladder(self):
        """The cards, split into rungs and watch lines. None on a complaint.

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
                continue                      # a card left blank is nothing
            if qty:
                rungs.append({"bps": bps, "qty": qty})
            else:
                watch.append(bps)
        return rungs, watch

    def _apply_ladder(self) -> None:
        """Validate what has been typed and put it into force.

        The same validation config.json goes through, so a set of limits that
        would make the app unsafe is refused rather than accepted. Applying
        always disarms: a typed digit must not fire a roll on the next tick.
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

        section = self._focused_section()
        ceiling = self.engine._tenor_ceiling_bps(
            section.cfg if section is not None else None, section)
        if wanted and ceiling is None and self.cfg.limit_mode == "bps":
            # Without a tenor there is no ceiling, and without a ceiling a rung
            # could quietly be looser than the limit for this roll. Refusing is
            # the safe direction, and it clears as soon as the contracts load.
            self._ladder_note(
                "the tenor is not known yet, so a limit cannot be checked "
                "against the roll limit", T.DANGER)
            return

        try:
            built = parse_ladder(wanted, lot_size=self.cfg.expected_lot_size,
                                 ceiling_bps=ceiling)
            # Watch lines are deliberately NOT ceiling-checked. They cannot
            # trade, so they cannot loosen anything, and that is exactly what
            # makes them the safe place to ask what a wider limit would look
            # like -- the single roll limit is not, because it IS the ceiling.
            parse_watch(watch)
            if self.cfg.sections and section is not None:
                # Several sections: these limits belong to the one on screen,
                # and editing it must not disturb the others.
                new_sections = editing.set_limits(self.cfg, section.key,
                                                  wanted, watch)
                candidate = replace(self.cfg, sections=new_sections)
            else:
                new_sections = None
                candidate = replace(self.cfg, limit_ladder=wanted,
                                    watch_limits=watch)
            candidate.validate()
        except Exception as exc:
            first = str(exc).splitlines()[-1].strip(" -")
            self._ladder_note(first, T.DANGER)
            return

        # Compare the limits, not the text. config.json holds qty as a number
        # and the box hands back a string, so comparing the raw entries made
        # every press look like a change: disarming and re-saving each time.
        current = getattr(section, "ladder", None)
        same_rungs = ([(r.bps, r.qty) for r in (current.rungs if current else [])]
                      == [(r.bps, r.qty) for r in built.rungs])
        was_watching = parse_watch(
            getattr(section.cfg, "watch_limits", None) if section is not None
            else self.cfg.watch_limits)
        if same_rungs and parse_watch(watch) == was_watching:
            self._ladder_note("unchanged", T.MUTED)
            return

        was_armed = self.engine.armed
        self.engine.disarm("limits changed")
        if new_sections is not None:
            self.cfg.sections = new_sections
        else:
            self.cfg.limit_ladder = wanted
            self.cfg.watch_limits = watch
        # The section these limits belong to, not sections[0]. Leaving this
        # off wrote the limits to config.json and then rebuilt a different
        # section, so they vanished off the screen.
        self.engine.rebuild_ladder(section)
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
        """Fill each card in with what the market is doing against its limit.

        Always on screen. It used to hide itself when there were no limits,
        which hid the Add limit button with it -- so the only way to make a
        limit was a control you could not reach until you already had one.
        """
        self._show_ladder()
        self._draw_ladder_invitation()

        views = {v.rung.key: v for v in (getattr(decision, "rungs", None) or [])}
        watched = {v.rung.key: v
                   for v in (getattr(decision, "watch_rungs", None) or [])}
        active = getattr(decision, "active_rung", None)
        active_key = active.rung.key if active else None
        lot_size = getattr(decision, "lot_size", 1000) or 1000

        done = total = 0
        for row in self.rung_rows:
            source = watched if row.get("watch") else views
            view = source.get(row["key"]) if row["key"] else None
            cells = row["cells"]

            if view is None:
                # Typed but not applied yet, or no quote to price it against.
                cells["rupees"].configure(text="--", fg=T.MUTED)
                cells["status"].configure(text="not set", fg=T.FAINT)
                cells["target"].configure(text="")
                cells["gap"].configure(text="")
                cells["rolled"].configure(text="")
                continue

            total += view.rung.qty
            done += view.done

            if view.watch:
                # Priced for comparison and never traded, so it is shown in the
                # colour of information rather than of action.
                colour = T.ACCENT_TEXT if view.qualifies else T.MUTED
                status = "in range" if view.qualifies else "too dear"
            elif view.exhausted:
                colour, status = T.FAINT, "done"
            elif view.rung.key == active_key:
                colour, status = T.WARN, "READY"
            elif view.qualifies:
                colour, status = T.SUCCESS, "in range"
            else:
                colour, status = T.MUTED, "too dear"

            cells["rupees"].configure(
                text=money(view.limit_rupees) if view.limit_rupees is not None
                else "--", fg=T.TEXT)
            cells["status"].configure(text=status, fg=colour)
            cells["target"].configure(
                text=f"far ask at or below {money(view.required_far_ask)}"
                if view.required_far_ask is not None else "")

            if view.distance_bps is None:
                cells["gap"].configure(text="")
            else:
                inside = view.distance_bps < 0
                cells["gap"].configure(
                    text=(f"{money(abs(view.distance_bps), 1)} bps "
                          + ("inside the limit" if inside else "away")),
                    fg=colour)

            if view.watch:
                cells["rolled"].configure(text="watching only", fg=T.FAINT)
            else:
                lots = view.rung.qty // lot_size if lot_size else 0
                cells["rolled"].configure(
                    text=f"{view.done:,} of {view.rung.qty:,} rolled"
                         + (f"  ({lots} lot{'s' if lots != 1 else ''})"
                            if lots else ""),
                    fg=T.FAINT)

        if total:
            left = max(0, total - done)
            self.ladder_total.configure(
                text=f"{done:,} of {total:,} rolled, {left:,} left"
                     f"   clip {self.cfg.clip_qty:,}")
        elif self.rung_rows:
            self.ladder_total.configure(text="no quantity set to trade")
        else:
            # The invitation below already says there are no limits; saying
            # it twice in different words reads as two different problems.
            self.ladder_total.configure(text="")

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
            # A file asking for live is not a confirmation, so the session
            # starts dry -- but silently overriding what the file says would
            # be its own kind of lie.
            if getattr(self.cfg, "live_in_file", False):
                self.mode_pill.set("DRY RUN (file said live)", T.WARN, "#2a2008")
                self.log.warn(
                    "config.json asks for live orders. This session started in "
                    "DRY RUN because live was not confirmed for it. Press "
                    "Go live... to send real orders.")
            else:
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
        """Spell out what the limit works out to, in both units.

        Two numbers on this card are both called a limit, and they are not the
        same thing: the box holds the ceiling for this tenor, while the rule
        may be working a tighter rung from the limits below. A box reading 30
        above a line reading "25 bps" looks like a contradiction unless it
        says which is which.
        """
        detail = getattr(self._last_decision, "limit_detail", None)
        if detail is None:
            self._limit_note("waiting for a quote", T.FAINT)
            return

        text = detail.describe()
        active = getattr(self._last_decision, "active_rung", None)
        typed = (self.limit_var.get() or "").strip()
        if active is not None and typed:
            try:
                differs = D(typed) != active.rung.bps
            except Exception:
                differs = False
            if differs:
                text += (f"{os.linesep}working the "
                         f"{active.rung.bps.normalize():f} bps rung below; "
                         f"{typed} is this tenor's ceiling")
        self._limit_note(text, T.FAINT)

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

    def _show_help(self) -> None:
        """What the screen means, in the operator's words.

        Every topic in rollover/help.py comes from a question that was
        actually asked while using this, not from a guess about what might
        be unclear.
        """
        from . import help as helptext

        if getattr(self, "_help_window", None) is not None:
            try:
                if self._help_window.winfo_exists():
                    self._help_window.lift()
                    return
            except Exception:
                pass

        window = tk.Toplevel(self)
        self._help_window = window
        window.title("What am I looking at?")
        window.configure(bg=T.BG)
        window.geometry("760x680")
        window.transient(self)

        def forget():
            self._help_window = None
            window.destroy()

        window.protocol("WM_DELETE_WINDOW", forget)
        window.bind("<Escape>", lambda _e: forget())

        holder = tk.Frame(window, bg=T.BG)
        holder.pack(fill="both", expand=True, padx=T.PAD_L, pady=T.PAD_L)

        bar = ttk.Scrollbar(holder, orient="vertical",
                            style="App.Vertical.TScrollbar")
        bar.pack(side="right", fill="y")
        text = tk.Text(holder, bg=T.SURFACE, fg=T.TEXT, bd=0,
                       highlightthickness=0, wrap="word", padx=T.PAD_L,
                       pady=T.PAD_L, font=self.fonts.ui,
                       yscrollcommand=bar.set, spacing1=2, spacing3=8)
        text.pack(side="left", fill="both", expand=True)
        bar.configure(command=text.yview)

        text.tag_configure("heading", foreground=T.ACCENT_TEXT,
                           font=self.fonts.ui_medium, spacing1=14, spacing3=6)
        text.tag_configure("body", foreground=T.MUTED, lmargin1=0, lmargin2=0)

        for heading, paragraphs in helptext.TOPICS:
            text.insert("end", heading + os.linesep, "heading")
            for paragraph in paragraphs:
                text.insert("end", paragraph + os.linesep, "body")
        text.configure(state="disabled")

        T.Button(window, "Close", forget, self.fonts, kind="primary",
                 width=120, height=32).pack(pady=(0, T.PAD_L))

    def _change_contracts(self) -> None:
        if self._busy():
            return
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

        self._last_snapshot = snap
        self.state_pill.set(snap.state.lower(),
                            STATE_COLOUR.get(snap.state, T.MUTED))
        self._refresh_mode(snap.dry_run)
        self._draw_strip(snap)

        # The cards below belong to whichever section is selected in the strip.
        # With one section that is the only one, and the window looks exactly
        # as it did before sections existed.
        shown = self._focused()
        if shown is not None:
            near, far = shown.near, shown.far
            near_q, far_q = shown.near_quote, shown.far_quote
            decision, report = shown.decision, shown.report
            note = shown.halted_reason or snap.halted_reason or shown.note or snap.note
        else:
            near, far = snap.near, snap.far
            near_q, far_q = snap.near_quote, snap.far_quote
            decision, report = snap.decision, snap.report
            note = snap.halted_reason or snap.note

        self._draw_ladder(decision)

        source = snap.quote_source or "connecting"
        self.source_pill.set(source,
                             T.SUCCESS if source == "live feed" else T.WARN)
        self.note.configure(text=note,
                            fg=T.DANGER if snap.halted_reason else T.MUTED)
        self.halt_button.set_enabled(bool(snap.halted_reason))

        for key, info, quote in (("near", near, near_q), ("far", far, far_q)):
            self._draw_leg(self.leg_widgets[key], info, quote, key)

        self._draw_cost(snap, decision)
        self._draw_gates(report)

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

    def _draw_cost(self, snap, decision=None) -> None:
        dec = decision if decision is not None else snap.decision
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
        # Per lot, because that is the number an operator holds in their head
        # and it does not depend on whether a clip happens to be sized right
        # now. The cost of the clip itself, when there is one, goes beside it.
        clip_cost = dec.cost_for_clip
        self.cost_rupees.configure(
            text=(f"{money(bps, 1)} bps" if bps is not None else "")
                 + f"     Rs {money(dec.cost_per_lot, 2)} per lot"
                 + (f"     Rs {money(clip_cost, 2)} for this clip"
                    if clip_cost is not None else ""))

        first = self._last_decision is None
        self._last_decision = dec
        if first:
            self._reset_limit()
        else:
            self._describe_limit()

        self.stat_labels["worst"].configure(text=money(dec.worst_case))
        self.stat_labels["sell"].configure(text=money(dec.sell_limit))
        self.stat_labels["buy"].configure(text=money(dec.buy_limit))
        # Zero means no clip is sized, which reads as a broken number rather
        # than as "nothing is being sent".
        self.stat_labels["qty"].configure(
            text=f"{dec.qty:,}" if dec.qty else "--")

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
        # Against a count that only ever goes UP, not against how many lines
        # the buffer is holding. recent() is capped at 300 and the buffer
        # keeps 500, so "how many are there" stops changing at 300 -- and
        # from the 300th line of a session this returned early every time:
        # the pane froze and no alert sounded again for the rest of the run.
        # Both counters come from the Logbook under its own lock.
        seq = self.log.sequence
        if seq == self._last_log_seq:
            return
        self._last_log_seq = seq
        entries = self.log.recent(300)

        # Anything that halts, or the cost finally clearing, is worth hearing.
        # The operator is not necessarily looking at the screen.
        alerts = self.log.alert_sequence
        if alerts > self._alerts_seen:
            # From the Logbook, so it is still right after the alert has
            # scrolled out of the 300 lines on screen.
            newest = self.log.last_alert
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
