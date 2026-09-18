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
from . import __version__, updater
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
        if self.cfg.dry_run:
            self.mode_pill.set("DRY RUN, nothing sent", T.WARN, "#2a2008")
        else:
            self.mode_pill.set("LIVE ORDERS", T.DANGER, "#2a0d0b")

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

    def _build_actions(self, parent) -> None:
        bar = tk.Frame(parent, bg=T.BG)
        bar.pack(fill="x", pady=(0, self.gap))

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

        self.arm_timer = tk.Label(bar, text="", bg=T.BG, fg=T.WARN,
                                  font=self.fonts.mono_medium)
        self.arm_timer.pack(side="right", padx=T.PAD_S)

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
        self.engine.disarm("window closing")
        self.engine.stop()
        self._cancel_refresh()
        self.destroy()
