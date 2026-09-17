"""One visual language for every window.

Tkinter gives you grey boxes and square buttons unless you build on top of it,
so this module holds the design tokens and the handful of widgets the app needs:
rounded buttons, cards, status pills and a styled table. Every window imports
from here, so a colour or a spacing is defined once.

Numbers use a tabular monospace face throughout, because a price that shifts
sideways as digits change is hard to read at a glance.
"""
from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable, Optional

# ---------------------------------------------------------------- palette
#
# Taken from choiceindia.com: the navy #0f1621 and the blue #2777f3 carry the
# whole brand, with #ffce02, #45b644 and #ef404a for warning, good and bad.
#
# Two of the brand colours are too dark to sit on a dark background as small
# text: #2777f3 lands at 4.34:1 and #667485 at 3.80:1, both under the 4.5:1
# minimum. Those two keep their exact values for fills and hairlines, where
# contrast is not read as text, and get a lightened pair for type.

BRAND_NAVY = "#0f1621"    # exact, from the logo
BRAND_BLUE = "#2777f3"    # exact, from the logo
BRAND_GOLD = "#ffce02"
BRAND_GREEN = "#45b644"
BRAND_RED = "#ef404a"
BRAND_TEAL = "#0cbcb7"
BRAND_GREY = "#667485"

BG = "#080d13"          # window background, a shade under the brand navy
SURFACE = BRAND_NAVY    # cards
SURFACE_2 = "#161f2c"   # inputs, table rows
SURFACE_3 = "#1e2836"   # hover
BORDER = "#222d3b"
BORDER_SOFT = "#18202c"

TEXT = "#eef3f8"        # 16.3:1 on surface
MUTED = "#8b9bb0"       #  6.4:1
FAINT = "#7d8ca0"       #  5.3:1, the readable form of the brand grey
HAIRLINE = BRAND_GREY   # borders and rules only, never text

ACCENT = BRAND_BLUE     # button fills
ACCENT_TEXT = "#4d94f7" #  6.0:1, the readable form of the brand blue
ACCENT_HOVER = "#4d94f7"
SUCCESS = BRAND_GREEN
SUCCESS_HOVER = "#52cc51"
WARN = BRAND_GOLD
WARN_HOVER = "#ffd93a"
DANGER = BRAND_RED
DANGER_HOVER = "#ff5d66"

BUY = BRAND_GREEN       # near leg, the side being sold out of
SELL = BRAND_GOLD       # far leg, the side being bought into

# Tick flash: a price that moves is briefly tinted, the way every market data
# screen does it, so a change is noticed rather than merely rendered.
UP = BRAND_GREEN
DOWN = BRAND_RED
UP_TINT = "#10271a"
DOWN_TINT = "#2b1215"
FLASH_SECONDS = 0.9

# ---------------------------------------------------------------- spacing
PAD_XS, PAD_S, PAD_M, PAD_L, PAD_XL = 4, 8, 14, 20, 28
RADIUS = 10


def is_compact(window: tk.Misc) -> bool:
    """A laptop screen cannot spare the padding a large monitor can."""
    return window.winfo_screenheight() < 900


class Fonts:
    """Built once per window, since Tk fonts need a live interpreter."""

    def __init__(self, compact: bool = False) -> None:
        self.compact = compact
        ui = self._pick(("Segoe UI Variable Text", "Segoe UI", "Inter", "Helvetica"))
        mono = self._pick(("Cascadia Mono", "Consolas", "JetBrains Mono", "Courier New"))

        self.ui = tkfont.Font(family=ui, size=10)
        self.ui_medium = tkfont.Font(family=ui, size=10, weight="bold")
        self.ui_small = tkfont.Font(family=ui, size=9)
        self.label = tkfont.Font(family=ui, size=8, weight="bold")
        self.title = tkfont.Font(family=ui, size=16, weight="bold")
        self.subtitle = tkfont.Font(family=ui, size=11)

        self.mono = tkfont.Font(family=mono, size=10)
        self.mono_medium = tkfont.Font(family=mono, size=11, weight="bold")
        self.mono_large = tkfont.Font(family=mono, size=14 if compact else 15,
                                      weight="bold")
        self.mono_hero = tkfont.Font(family=mono, size=26 if compact else 34,
                                     weight="bold")

    @staticmethod
    def _pick(names) -> str:
        available = {name.lower() for name in tkfont.families()}
        for name in names:
            if name.lower() in available:
                return name
        return names[-1]


def round_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kw):
    """A rounded rectangle, which Tk's canvas does not provide."""
    r = min(r, abs(x2 - x1) / 2, abs(y2 - y1) / 2)
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kw)


class Button(tk.Canvas):
    """A flat rounded button with hover and disabled states.

    Tk's own button cannot be rounded and keeps a platform border, which is most
    of why a Tkinter app looks like it was built in 1998.
    """

    def __init__(self, parent, text: str, command: Callable[[], None],
                 fonts: Fonts, kind: str = "ghost", width: int = 150,
                 height: int = 38, icon: str = ""):
        # Fill, hover fill, label. Dark labels on the bright brand fills read
        # far better than white does on gold or green.
        self.palette = {
            "primary": (ACCENT, ACCENT_HOVER, "#ffffff"),
            "success": (SUCCESS, SUCCESS_HOVER, "#05160a"),
            "warn": (WARN, WARN_HOVER, "#1a1400"),
            "danger": (DANGER, DANGER_HOVER, "#1f0405"),
            "ghost": (SURFACE_2, SURFACE_3, TEXT),
        }[kind]

        parent_bg = parent.cget("bg") if "bg" in parent.keys() else BG
        # takefocus so the button is reachable by Tab, which a bare Canvas is not.
        super().__init__(parent, width=width, height=height, bg=parent_bg,
                         highlightthickness=0, bd=0, cursor="hand2", takefocus=1)

        self._command = command
        self._enabled = True
        self._label = f"{icon}  {text}".strip() if icon else text
        self._fonts = fonts
        # Never call these _w or _h: Tkinter stores the widget's Tcl path name
        # in self._w, and overwriting it breaks every later call on the widget.
        self._width, self._height = width, height

        self._shape = round_rect(self, 1, 1, width - 1, height - 1, RADIUS,
                                 fill=self.palette[0], outline="")
        # Drawn only while focused, so keyboard users can see where they are.
        self._focus_ring = round_rect(self, 2, 2, width - 2, height - 2, RADIUS,
                                      fill="", outline="", width=2)
        self._text = self.create_text(width / 2, height / 2, text=self._label,
                                      fill=self.palette[2], font=fonts.ui_medium)

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<Return>", self._activate)
        self.bind("<KP_Enter>", self._activate)
        self.bind("<space>", self._activate)

    def _on_enter(self, _e=None):
        if self._enabled:
            self.itemconfigure(self._shape, fill=self.palette[1])

    def _on_leave(self, _e=None):
        if self._enabled:
            self.itemconfigure(self._shape, fill=self.palette[0])

    def _on_press(self, _e=None):
        if self._enabled:
            self.move(self._text, 0, 1)

    def _on_release(self, _e=None):
        if not self._enabled:
            return
        self.move(self._text, 0, -1)
        self._command()

    def _on_focus_in(self, _e=None):
        if self._enabled:
            self.itemconfigure(self._focus_ring, outline=TEXT)

    def _on_focus_out(self, _e=None):
        self.itemconfigure(self._focus_ring, outline="")

    def _activate(self, _e=None):
        """Keyboard activation, so the button is not mouse only."""
        if self._enabled:
            self._command()
        return "break"

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow", takefocus=enabled)
        if not enabled:
            self.itemconfigure(self._focus_ring, outline="")
        self.itemconfigure(self._shape,
                           fill=self.palette[0] if enabled else SURFACE_2)
        self.itemconfigure(self._text,
                           fill=self.palette[2] if enabled else FAINT)

    def set_text(self, text: str) -> None:
        self._label = text
        self.itemconfigure(self._text, text=text)


class Pill(tk.Canvas):
    """A small rounded status badge."""

    def __init__(self, parent, fonts: Fonts, width: int = 120, height: int = 26):
        parent_bg = parent.cget("bg") if "bg" in parent.keys() else BG
        super().__init__(parent, width=width, height=height, bg=parent_bg,
                         highlightthickness=0, bd=0)
        self._shape = round_rect(self, 1, 1, width - 1, height - 1, height / 2,
                                 fill=SURFACE_2, outline="")
        self._dot = self.create_oval(12, height / 2 - 3, 18, height / 2 + 3,
                                     fill=MUTED, outline="")
        self._text = self.create_text(26, height / 2, text="", anchor="w",
                                      fill=TEXT, font=fonts.ui_medium)

    def set(self, text: str, colour: str, background: Optional[str] = None) -> None:
        self.itemconfigure(self._text, text=text, fill=colour)
        self.itemconfigure(self._dot, fill=colour)
        self.itemconfigure(self._shape, fill=background or SURFACE_2)


class ScrollFrame(tk.Frame):
    """A vertically scrollable region.

    Windows are sized to the screen, so on a short display the content below
    the fold used to be simply unreachable: the save and cancel buttons sat
    past the bottom edge with no way to get at them. Everything now lives in
    one of these, so the window is always usable however small the screen.

    The scrollbar only appears when there is something to scroll, and the
    wheel is bound while the pointer is over the region rather than globally,
    so it does not steal scrolling from a table underneath.
    """

    def __init__(self, parent, bg: str = BG):
        super().__init__(parent, bg=bg)
        self._bg = bg

        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical",
                                       command=self.canvas.yview,
                                       style="App.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self._on_scroll)

        self.canvas.pack(side="left", fill="both", expand=True)
        # The scrollbar is packed on demand by _on_scroll.

        self.body = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window((0, 0), window=self.body,
                                                 anchor="nw")

        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    def _on_body_configure(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._fit()

    def _on_canvas_configure(self, event) -> None:
        # Keep the content the full width of the viewport so nothing reflows
        # sideways as the scrollbar appears and disappears.
        self.canvas.itemconfigure(self._window, width=event.width)
        self._fit()

    def _fit(self) -> None:
        """Stretch the content to the viewport when there is room to spare.

        Without this the body would only ever be its requested height, so a
        pane set to expand would collapse to its minimum even on a large
        screen. With it, the region behaves like a normal frame when the
        content fits and scrolls only when it does not.
        """
        viewport = self.canvas.winfo_height()
        wanted = self.body.winfo_reqheight()
        self.canvas.itemconfigure(self._window, height=max(viewport, wanted))

    def _on_scroll(self, first: str, last: str) -> None:
        needed = not (float(first) <= 0.0 and float(last) >= 1.0)
        if needed and not self.scrollbar.winfo_ismapped():
            self.scrollbar.pack(side="right", fill="y")
        elif not needed and self.scrollbar.winfo_ismapped():
            self.scrollbar.pack_forget()
        self.scrollbar.set(first, last)

    def _bind_wheel(self, _event=None) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self, _event=None) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event) -> None:
        first, last = self.canvas.yview()
        if first <= 0.0 and last >= 1.0:
            return
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def scroll_to_top(self) -> None:
        self.canvas.yview_moveto(0.0)


def card(parent, padx: int = PAD_M, pady: int = PAD_M) -> tk.Frame:
    """A surface panel. Tk has no shadows or radii on frames, so a card is
    distinguished by its fill and a hairline border."""
    outer = tk.Frame(parent, bg=BORDER_SOFT, bd=0, highlightthickness=0)
    inner = tk.Frame(outer, bg=SURFACE, padx=padx, pady=pady)
    inner.pack(fill="both", expand=True, padx=1, pady=1)
    outer.inner = inner          # callers pack into .inner
    return outer


def section_label(parent, text: str, fonts: Fonts) -> tk.Label:
    return tk.Label(parent, text=text.upper(), bg=parent.cget("bg"), fg=FAINT,
                    font=fonts.label, anchor="w")


def entry(parent, fonts: Fonts, textvariable=None, secret: bool = False,
          width: int = 28) -> tk.Entry:
    box = tk.Entry(parent, textvariable=textvariable, bg=SURFACE_2, fg=TEXT,
                   font=fonts.mono, insertbackground=ACCENT, relief="flat",
                   highlightthickness=1, highlightbackground=BORDER,
                   highlightcolor=ACCENT, width=width,
                   disabledbackground=SURFACE_2, disabledforeground=FAINT,
                   show="•" if secret else "")
    return box


def apply_ttk_theme(root: tk.Misc, fonts: Fonts) -> None:
    """Style the ttk widgets to match, since they ignore tk colours."""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure("App.Treeview",
                    background=SURFACE_2, fieldbackground=SURFACE_2,
                    foreground=TEXT, rowheight=25 if fonts.compact else 30,
                    borderwidth=0, font=fonts.mono)
    style.configure("App.Treeview.Heading",
                    background=SURFACE, foreground=FAINT, relief="flat",
                    borderwidth=0, font=fonts.label, padding=(10, 9))
    style.map("App.Treeview.Heading",
              background=[("active", SURFACE_3)], foreground=[("active", TEXT)])
    style.map("App.Treeview",
              background=[("selected", "#1d3a5f")],
              foreground=[("selected", TEXT)])
    style.layout("App.Treeview", [("App.Treeview.treearea", {"sticky": "nswe"})])

    style.configure("App.Vertical.TScrollbar",
                    background=SURFACE_3, troughcolor=SURFACE, borderwidth=0,
                    arrowcolor=MUTED, relief="flat", width=10)
    style.map("App.Vertical.TScrollbar",
              background=[("active", FAINT), ("pressed", MUTED)])

    style.configure("App.TRadiobutton", background=SURFACE, foreground=TEXT,
                    font=fonts.ui, focuscolor=SURFACE)
    style.map("App.TRadiobutton",
              background=[("active", SURFACE)],
              foreground=[("selected", ACCENT), ("active", TEXT)])

    style.configure("App.TCheckbutton", background=SURFACE, foreground=TEXT,
                    font=fonts.ui, focuscolor=SURFACE)
    style.map("App.TCheckbutton",
              background=[("active", SURFACE)],
              foreground=[("active", TEXT)])

    style.configure("App.TCombobox", fieldbackground=SURFACE_2,
                    background=SURFACE_2, foreground=TEXT, arrowcolor=MUTED,
                    borderwidth=0, relief="flat", padding=6)
    root.option_add("*TCombobox*Listbox.background", SURFACE_2)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")


def asset_path(name: str) -> str:
    """Locate a bundled asset, both when frozen and when run from source."""
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", name)


def apply_icon(window: tk.Misc) -> None:
    """Give a window the application icon, quietly doing nothing if it is missing."""
    try:
        window.iconbitmap(asset_path("icon.ico"))
    except Exception:
        pass


def centre(window: tk.Misc, fraction: float = 3.0) -> None:
    window.update_idletasks()
    w, h = window.winfo_reqwidth(), window.winfo_reqheight()
    x = max(0, (window.winfo_screenwidth() - w) // 2)
    y = max(0, int((window.winfo_screenheight() - h) / fraction))
    window.geometry(f"+{x}+{y}")


def fit(window: tk.Misc, want_w: int, want_h: int,
        margin_w: int = 60, margin_h: int = 140) -> tuple:
    """Size a window to the screen it is actually on."""
    w = min(want_w, window.winfo_screenwidth() - margin_w)
    h = min(want_h, window.winfo_screenheight() - margin_h)
    return w, h
