"""Choosing the two contracts.

Shown after login when the tokens are not set, and again whenever the operator
presses Change contracts. The list comes from today's scrip master, which is
also where the expiry, lot size and tick are read from, so what is picked here
is what the gates will later verify against.

Segment 13 holds over fourteen thousand rows and nearly all of them are options,
so finding the right two contracts is the whole job of this window: options are
filtered out entirely, month-end contracts can be shown on their own, the search
box matches on token and name, and every column sorts.
"""
from __future__ import annotations

import tkinter as tk
from datetime import date, datetime
from tkinter import ttk
from typing import Dict, List, Optional

from . import theme as T

COLUMNS = (
    ("token", "Token", 90, "w"),
    ("contract", "Contract", 210, "w"),
    ("expiry", "Expiry", 120, "w"),
    ("days", "Days", 70, "e"),
    ("kind", "Type", 100, "w"),
    ("lot", "Lot", 80, "e"),
    ("tick", "Tick", 90, "e"),
)


class ContractWindow(tk.Toplevel):
    """`self.ok` is True once both legs are chosen and written to config.json."""

    def __init__(self, master, cfg, log, broker, config_path: str,
                 apply=None, title=None):
        """`apply` receives the two chosen contract rows instead of the config
        being written directly.

        Without it the window does what it always did: set the near and far
        legs at the top level and save. With it, the caller decides what the
        choice means -- adding a section, or changing one section's legs
        without touching the others.
        """
        super().__init__(master)
        self.cfg = cfg
        self.log = log
        self.broker = broker
        self.config_path = config_path
        self.apply = apply
        self._title_override = title

        self.ok = False
        self.chosen = None
        self.rows: List[Dict] = []
        self.by_token: Dict[str, Dict] = {}
        self.near: Optional[Dict] = None
        self.far: Optional[Dict] = None
        self._sort_key = "expiry"
        self._sort_reverse = False

        self.title(self._title_override or "Choose the two contracts")
        self.configure(bg=T.BG)
        width, height = T.fit(self, 1060, 780)
        self.geometry(f"{width}x{height}+30+24")
        self.minsize(900, 600)

        T.apply_icon(self)
        self.fonts = T.Fonts()
        T.apply_ttk_theme(self, self.fonts)

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _e: self._cancel())
        self.after(50, self._load)

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        self.scroller = T.ScrollFrame(self)
        self.scroller.pack(fill="both", expand=True)
        shell = tk.Frame(self.scroller.body, bg=T.BG, padx=T.PAD_L, pady=T.PAD_M)
        shell.pack(fill="both", expand=True)

        tk.Label(shell, text="Choose the two contracts", bg=T.BG, fg=T.TEXT,
                 font=self.fonts.title, anchor="w").pack(fill="x")
        tk.Label(shell, text="The near leg is the one you sell, the far leg the one "
                             "you buy. Options are hidden; only futures can be a leg.",
                 bg=T.BG, fg=T.MUTED, font=self.fonts.ui, anchor="w").pack(
                     fill="x", pady=(2, T.PAD_M))

        self._build_filters(shell)
        self._build_table(shell)
        self._build_picks(shell)
        self._build_footer(shell)

    def _build_filters(self, parent) -> None:
        holder = T.card(parent, padx=T.PAD_M, pady=T.PAD_S)
        holder.pack(fill="x")
        bar = holder.inner

        tk.Label(bar, text="SEARCH", bg=T.SURFACE, fg=T.FAINT,
                 font=self.fonts.label).pack(side="left", padx=(0, T.PAD_S))

        self.filter_var = tk.StringVar()
        box = T.entry(bar, self.fonts, textvariable=self.filter_var, width=30)
        box.pack(side="left", fill="x", expand=True, ipady=6)
        box.bind("<Escape>", lambda _e: self.filter_var.set(""))
        self.filter_var.trace_add("write", lambda *_: self._refresh_tree())
        self.search_box = box

        T.Button(bar, "Clear", lambda: self.filter_var.set(""), self.fonts,
                 width=76, height=32).pack(side="left", padx=T.PAD_S)

        self.kind_var = tk.StringVar(value="month")
        for label, value in (("Month end", "month"), ("All futures", "all")):
            ttk.Radiobutton(bar, text=label, variable=self.kind_var, value=value,
                            command=self._refresh_tree,
                            style="App.TRadiobutton").pack(side="left",
                                                           padx=(T.PAD_S, 0))

        self.count_label = tk.Label(bar, text="", bg=T.SURFACE, fg=T.MUTED,
                                    font=self.fonts.ui_small)
        self.count_label.pack(side="right")

    def _build_table(self, parent) -> None:
        holder = T.card(parent, padx=T.PAD_XS, pady=T.PAD_XS)
        holder.pack(fill="both", expand=True, pady=T.PAD_M)
        box = holder.inner

        self.tree = ttk.Treeview(box, style="App.Treeview", show="headings",
                                 columns=[c[0] for c in COLUMNS],
                                 selectmode="browse")
        for key, title, width, anchor in COLUMNS:
            self.tree.heading(key, text=title, anchor="w",
                              command=lambda k=key: self._sort_by(k))
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=(key == "contract"))
        self.tree.pack(side="left", fill="both", expand=True)

        bar = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview,
                            style="App.Vertical.TScrollbar")
        bar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=bar.set)

        self.tree.tag_configure("near", background="#0f2a1a", foreground=T.BUY)
        self.tree.tag_configure("far", background="#2b1d0d", foreground=T.SELL)
        self.tree.tag_configure("monthly", foreground=T.TEXT)
        self.tree.tag_configure("weekly", foreground=T.MUTED)

        self.tree.bind("<Double-Button-1>", lambda _e: self._set_leg("near"))
        self.tree.bind("<Return>", lambda _e: self._set_leg("near"))
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._hint())

    def _build_picks(self, parent) -> None:
        wrap = tk.Frame(parent, bg=T.BG)
        wrap.pack(fill="x")
        wrap.columnconfigure(0, weight=1, uniform="pick")
        wrap.columnconfigure(1, weight=1, uniform="pick")

        self.pick_widgets = {}
        for column, (leg, title, action, colour) in enumerate((
                ("near", "NEAR LEG", "Set as NEAR", T.BUY),
                ("far", "FAR LEG", "Set as FAR", T.SELL))):
            holder = T.card(wrap, padx=T.PAD_M, pady=T.PAD_S)
            holder.grid(row=0, column=column, sticky="nsew",
                        padx=(0, T.PAD_S) if column == 0 else (T.PAD_S, 0))
            box = holder.inner

            head = tk.Frame(box, bg=T.SURFACE)
            head.pack(fill="x")
            tk.Label(head, text=title, bg=T.SURFACE, fg=colour,
                     font=self.fonts.label).pack(side="left")
            tk.Label(head, text="sell" if leg == "near" else "buy", bg=T.SURFACE,
                     fg=T.FAINT, font=self.fonts.ui_small).pack(side="left",
                                                                padx=T.PAD_S)

            name = tk.Label(box, text="not chosen", bg=T.SURFACE, fg=T.FAINT,
                            font=self.fonts.mono_medium, anchor="w")
            name.pack(fill="x", pady=(T.PAD_XS, 0))
            meta = tk.Label(box, text="", bg=T.SURFACE, fg=T.MUTED,
                            font=self.fonts.ui_small, anchor="w")
            meta.pack(fill="x")

            T.Button(box, action, lambda l=leg: self._set_leg(l), self.fonts,
                     width=150, height=34).pack(anchor="w", pady=(T.PAD_S, 0))
            self.pick_widgets[leg] = {"name": name, "meta": meta}

    def _build_footer(self, parent) -> None:
        self.status = tk.Label(parent, text="", bg=T.BG, fg=T.MUTED,
                               font=self.fonts.ui, anchor="w", justify="left",
                               wraplength=980)
        self.status.pack(fill="x", pady=(T.PAD_M, T.PAD_XS))

        bar = tk.Frame(parent, bg=T.BG)
        bar.pack(fill="x", pady=(T.PAD_XS, 0))

        self.save_button = T.Button(bar, "Save and continue", self._save, self.fonts,
                                    kind="primary", width=190, height=42)
        self.save_button.pack(side="left")
        self.save_button.set_enabled(False)

        T.Button(bar, "Cancel", self._cancel, self.fonts,
                 width=110, height=42).pack(side="left", padx=T.PAD_S)
        T.Button(bar, "Suggest the usual roll", self._suggest, self.fonts,
                 width=200, height=42).pack(side="right")
        T.Button(bar, "Clear both", self._clear, self.fonts,
                 width=120, height=42).pack(side="right", padx=T.PAD_S)

    # ------------------------------------------------------------------- data
    def _load(self) -> None:
        try:
            self.rows = self.broker.search(self.cfg.underlying)
        except Exception as exc:
            self._say(f"Could not read the scrip master: {exc}", T.DANGER)
            return
        if not self.rows:
            self._say(f"No futures matching {self.cfg.underlying!r} in segment "
                      f"{self.cfg.segment_id}.", T.DANGER)
            return

        self.by_token = {r["Token"]: r for r in self.rows}
        if self.cfg.near_token in self.by_token:
            self.near = self.by_token[self.cfg.near_token]
        if self.cfg.far_token in self.by_token:
            self.far = self.by_token[self.cfg.far_token]

        self._redraw_picks()
        if not (self.near or self.far):
            self._say(f"{len(self.rows)} futures loaded. Pick the near leg, then the "
                      "far leg, or press Suggest the usual roll.")

    @staticmethod
    def _days_to(expiry: str) -> Optional[int]:
        if expiry == "?":
            return None
        try:
            return (datetime.strptime(expiry, "%Y-%m-%d").date() - date.today()).days
        except ValueError:
            return None

    def _visible_rows(self) -> List[Dict]:
        needle = self.filter_var.get().strip().upper()
        wanted_kind = self.kind_var.get()
        out = []
        for row in self.rows:
            if wanted_kind == "month" and row.get("Kind") != "monthly":
                continue
            if needle and needle not in row["SecDesc"].upper() and needle not in row["Token"]:
                continue
            out.append(row)

        def key(row):
            if self._sort_key == "token":
                return (len(row["Token"]), row["Token"])
            if self._sort_key == "contract":
                return row["SecDesc"]
            if self._sort_key == "days":
                days = self._days_to(row["Expiry"])
                return (days is None, days or 0)
            if self._sort_key == "kind":
                return row.get("Kind", "")
            if self._sort_key == "lot":
                return int(row.get("LotSize") or 0)
            if self._sort_key == "tick":
                return str(row.get("Tick"))
            return (row["Expiry"] == "?", row["Expiry"])

        out.sort(key=key, reverse=self._sort_reverse)
        return out

    def _refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        visible = self._visible_rows()
        for row in visible:
            days = self._days_to(row["Expiry"])
            token = row["Token"]
            tags = [row.get("Kind", "monthly")]
            if self.near and token == self.near["Token"]:
                tags.append("near")
            if self.far and token == self.far["Token"]:
                tags.append("far")
            self.tree.insert(
                "", "end", iid=token, tags=tuple(tags),
                values=(token, row["SecDesc"], row["Expiry"],
                        "--" if days is None else days,
                        row.get("Kind", ""), row.get("LotSize", ""),
                        row.get("Tick", "")))

        hidden = len(self.rows) - len(visible)
        self.count_label.configure(
            text=f"{len(visible)} shown" + (f", {hidden} hidden" if hidden else ""))

    def _sort_by(self, key: str) -> None:
        if self._sort_key == key:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_key, self._sort_reverse = key, False
        self._refresh_tree()

    # ---------------------------------------------------------------- picking
    def _selected(self) -> Optional[Dict]:
        picks = self.tree.selection()
        if not picks:
            self._say("Select a contract in the list first.", T.WARN)
            return None
        return self.by_token.get(picks[0])

    def _set_leg(self, leg: str) -> None:
        row = self._selected()
        if row is None:
            return
        other = "far" if leg == "near" else "near"
        if getattr(self, other) and getattr(self, other)["Token"] == row["Token"]:
            setattr(self, other, None)      # never let one contract be both legs
        setattr(self, leg, row)
        self._redraw_picks()

    def _clear(self) -> None:
        self.near = self.far = None
        self._redraw_picks()

    def _suggest(self) -> None:
        """The usual roll: the next month-end contract, into the one after it."""
        monthly = [r for r in self.rows
                   if r.get("Kind") == "monthly" and r["Expiry"] != "?"]
        upcoming = sorted((r for r in monthly if (self._days_to(r["Expiry"]) or -1) >= 0),
                          key=lambda r: r["Expiry"])
        if len(upcoming) < 2:
            self._say("There are not two month-end contracts left to roll between.",
                      T.WARN)
            return
        self.near, self.far = upcoming[0], upcoming[1]
        self.kind_var.set("month")
        self._redraw_picks()
        self._say(f"Suggested {self.near['SecDesc']} into {self.far['SecDesc']}. "
                  "Change either leg if that is not the roll you want.", T.ACCENT)

    def _hint(self) -> None:
        picks = self.tree.selection()
        row = self.by_token.get(picks[0]) if picks else None
        if row and not (self.near and self.far):
            self._say(f"{row['SecDesc']} selected. Assign it to a leg below, or "
                      "double-click to make it the near leg.")

    def _redraw_picks(self) -> None:
        for leg, colour in (("near", T.BUY), ("far", T.SELL)):
            row = getattr(self, leg)
            cells = self.pick_widgets[leg]
            if row is None:
                cells["name"].configure(text="not chosen", fg=T.FAINT)
                cells["meta"].configure(text="")
            else:
                days = self._days_to(row["Expiry"])
                cells["name"].configure(text=row["SecDesc"], fg=colour)
                cells["meta"].configure(
                    text=f"token {row['Token']}   expires {row['Expiry']}"
                         + (f"   {days} days" if days is not None else "")
                         + f"   lot {row['LotSize']}")

        self._refresh_tree()
        self._validate()

    def _validate(self) -> bool:
        """The same checks the gates apply later, so a bad pair is caught here."""
        if self.near is None or self.far is None:
            self.save_button.set_enabled(False)
            self._say(f"Now choose the {'near' if self.near is None else 'far'} leg.")
            return False

        problems = []
        if self.near["Token"] == self.far["Token"]:
            problems.append("both legs are the same contract")
        if self.near["Expiry"] == "?" or self.far["Expiry"] == "?":
            problems.append("an expiry could not be read from the scrip master")
        elif self.far["Expiry"] <= self.near["Expiry"]:
            problems.append(f"the far expiry {self.far['Expiry']} does not come after "
                            f"the near expiry {self.near['Expiry']}")
        for leg, row in (("near", self.near), ("far", self.far)):
            if int(row.get("LotSize") or 0) != self.cfg.expected_lot_size:
                problems.append(f"the {leg} lot size is {row.get('LotSize')}, but the "
                                f"config expects {self.cfg.expected_lot_size}")

        if problems:
            self.save_button.set_enabled(False)
            self._say("; ".join(problems), T.DANGER)
            return False

        gap = (self._days_to(self.far["Expiry"]) or 0) - (self._days_to(self.near["Expiry"]) or 0)
        self.save_button.set_enabled(True)
        self._say(f"Ready: sell {self.near['SecDesc']}, buy {self.far['SecDesc']}, "
                  f"{gap} days apart.", T.SUCCESS)
        return True

    # ----------------------------------------------------------------- saving
    def _save(self) -> None:
        if not self._validate():
            return

        if self.apply is not None:
            # The caller decides what this choice means. It raises to refuse,
            # with a sentence worth showing.
            try:
                self.apply(self.near, self.far)
            except Exception as exc:
                self._say(str(exc), T.DANGER)
                return
            self.chosen = (self.near, self.far)
            self.ok = True
            self.destroy()
            return

        self.cfg.near_token = self.near["Token"]
        self.cfg.far_token = self.far["Token"]
        self.cfg.near_expiry = self.near["Expiry"]
        self.cfg.far_expiry = self.far["Expiry"]
        try:
            self.cfg.validate()
        except Exception as exc:
            self._say(str(exc), T.DANGER)
            return
        try:
            self.cfg.save(self.config_path)
            self.log.info(f"Contracts saved to {self.config_path}: "
                          f"near {self.cfg.near_token} ({self.cfg.near_expiry}), "
                          f"far {self.cfg.far_token} ({self.cfg.far_expiry})")
        except OSError as exc:
            self._say(f"Could not write config.json: {exc}", T.DANGER)
            return
        self.ok = True
        self.destroy()

    def _say(self, text: str, colour: str = T.MUTED) -> None:
        self.status.configure(text=text, fg=colour)

    def _cancel(self) -> None:
        self.ok = False
        self.destroy()
