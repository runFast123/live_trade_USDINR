"""The login window.

Credentials are typed here rather than being required in config.json, so each
operator signs in with their own. The network work runs on a worker thread and
reports back through a queue, so the window never freezes and a slow or failed
login can always be cancelled.

The OTP step adapts: if the broker hands the code back itself the login goes
straight through, and only if it does not does the window ask for a code.
"""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import Optional

from . import theme as T
from .broker import Broker, BrokerError

BASE_URLS = (
    ("finxomne (default)", "https://finxomne.choiceindia.com"),
    ("finx", "https://finx.choiceindia.com"),
)


class LoginWindow(tk.Toplevel):
    """`self.ok` is True once a session exists."""

    def __init__(self, master, cfg, log, base_dir: str, config_path: str):
        super().__init__(master)
        self.cfg = cfg
        self.log = log
        self.base_dir = base_dir
        self.config_path = config_path
        self.session_path = os.path.join(base_dir, "session.json")

        self.broker: Optional[Broker] = None
        self.ok = False
        self._events: "queue.Queue[tuple]" = queue.Queue()
        self._busy = False

        self.title("Sign in to Choice")
        self.configure(bg=T.BG)
        # Height is resizable because the window now scrolls: on a short screen
        # the operator can still reach the buttons.
        self.resizable(False, True)

        T.apply_icon(self)
        self.fonts = T.Fonts()
        T.apply_ttk_theme(self, self.fonts)

        self._build()
        self._fit_to_content()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Return>", lambda _e: self._on_login())
        self.bind("<Escape>", lambda _e: self._cancel())
        self._pump_id = self.after(120, self._pump)

    # ------------------------------------------------------------------ build
    def _field(self, parent, row: int, label: str, value: str,
               secret: bool = False) -> tk.Entry:
        tk.Label(parent, text=label.upper(), bg=T.SURFACE, fg=T.FAINT,
                 font=self.fonts.label, anchor="w").grid(
                     row=row * 2, column=0, sticky="w", pady=(T.PAD_M, 2))
        box = T.entry(parent, self.fonts, secret=secret, width=34)
        box.grid(row=row * 2 + 1, column=0, sticky="we", ipady=7)
        box.insert(0, value or "")
        return box

    def _build(self) -> None:
        self.scroller = T.ScrollFrame(self)
        self.scroller.pack(fill="both", expand=True)
        shell = tk.Frame(self.scroller.body, bg=T.BG, padx=T.PAD_L, pady=T.PAD_L)
        shell.pack(fill="both", expand=True)

        holder = T.card(shell, padx=T.PAD_XL, pady=T.PAD_L)
        holder.pack(fill="both", expand=True)
        box = holder.inner

        tk.Label(box, text="Sign in to Choice", bg=T.SURFACE, fg=T.TEXT,
                 font=self.fonts.title, anchor="w").pack(fill="x")
        tk.Label(box, text=f"{self.cfg.underlying} rollover watcher",
                 bg=T.SURFACE, fg=T.MUTED, font=self.fonts.subtitle,
                 anchor="w").pack(fill="x", pady=(2, T.PAD_S))

        form = tk.Frame(box, bg=T.SURFACE)
        form.pack(fill="x")
        form.columnconfigure(0, weight=1)

        self.vendor_entry = self._field(form, 0, "Vendor ID", self.cfg.vendor_id)
        self.key_entry = self._field(form, 1, "API key", self.cfg.api_key, secret=True)
        self.mobile_entry = self._field(form, 2, "Mobile number", self.cfg.mobile_no)

        tk.Label(form, text="SERVER", bg=T.SURFACE, fg=T.FAINT,
                 font=self.fonts.label, anchor="w").grid(
                     row=6, column=0, sticky="w", pady=(T.PAD_M, 2))
        self.server_var = tk.StringVar(value=BASE_URLS[0][0])
        for title, url in BASE_URLS:
            if url == self.cfg.base_url:
                self.server_var.set(title)
        ttk.Combobox(form, textvariable=self.server_var, state="readonly",
                     style="App.TCombobox", font=self.fonts.ui,
                     values=[t for t, _ in BASE_URLS]).grid(
                         row=7, column=0, sticky="we", ipady=4)

        # OTP row, revealed only when the broker asks for one
        self.otp_label = tk.Label(form, text="ONE TIME PASSWORD", bg=T.SURFACE,
                                  fg=T.WARN, font=self.fonts.label, anchor="w")
        self.otp_entry = T.entry(form, self.fonts, width=34)
        self.otp_entry.configure(highlightbackground=T.WARN, highlightcolor=T.WARN)

        options = tk.Frame(box, bg=T.SURFACE)
        options.pack(fill="x", pady=(T.PAD_M, 0))
        self.remember_var = tk.BooleanVar(value=bool(self.cfg.vendor_id))
        self.fresh_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text="Remember these on this machine",
                        variable=self.remember_var,
                        style="App.TCheckbutton").pack(anchor="w")
        ttk.Checkbutton(options, text="Force a fresh login, ignore today's session",
                        variable=self.fresh_var,
                        style="App.TCheckbutton").pack(anchor="w", pady=(2, 0))

        self.status = tk.Label(box, text="", bg=T.SURFACE, fg=T.MUTED,
                               font=self.fonts.ui, anchor="w", justify="left",
                               wraplength=400)
        self.status.pack(fill="x", pady=(T.PAD_M, T.PAD_XS))

        buttons = tk.Frame(box, bg=T.SURFACE)
        buttons.pack(fill="x", pady=(T.PAD_S, 0))
        self.login_button = T.Button(buttons, "Log in", self._on_login, self.fonts,
                                     kind="primary", width=190, height=42)
        self.login_button.pack(side="left")
        T.Button(buttons, "Cancel", self._cancel, self.fonts,
                 width=110, height=42).pack(side="left", padx=T.PAD_S)

        tk.Label(box, text="Remembering writes the API key to config.json in plain text.",
                 bg=T.SURFACE, fg=T.FAINT, font=self.fonts.ui_small, anchor="w",
                 wraplength=400, justify="left").pack(fill="x", pady=(T.PAD_M, 0))

        self.vendor_entry.focus_set()

    def _fit_to_content(self) -> None:
        """Size the window to its content, capped at the screen.

        A scrolling region has no natural size of its own, so without this the
        window would open at the canvas default rather than around the form.
        """
        self.update_idletasks()
        body = self.scroller.body
        width = body.winfo_reqwidth()
        height = min(body.winfo_reqheight(), self.winfo_screenheight() - 140)
        self.geometry(f"{width}x{height}")
        T.centre(self)

    # -------------------------------------------------------------- behaviour
    def _set_status(self, text: str, colour: str = T.MUTED) -> None:
        self.status.configure(text=text, fg=colour)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.login_button.set_enabled(not busy)
        if busy:
            self.login_button.set_text("Working...")
        else:
            self.login_button.set_text("Verify OTP" if self._otp_visible else "Log in")

    def _show_otp(self) -> None:
        self.otp_label.grid(row=8, column=0, sticky="w", pady=(T.PAD_M, 2))
        self.otp_entry.grid(row=9, column=0, sticky="we", ipady=7)
        self.otp_entry.focus_set()
        self.login_button.set_text("Verify OTP")
        self._fit_to_content()

    @property
    def _otp_visible(self) -> bool:
        return bool(self.otp_entry.winfo_manager())

    def _collect(self) -> bool:
        """Copy what was typed into the config. False if something is missing."""
        self.cfg.vendor_id = self.vendor_entry.get().strip()
        self.cfg.api_key = self.key_entry.get().strip()
        self.cfg.mobile_no = self.mobile_entry.get().strip()
        for title, url in BASE_URLS:
            if title == self.server_var.get():
                self.cfg.base_url = url

        missing = []
        if not self.cfg.vendor_id:
            missing.append("vendor ID")
        if not self.cfg.api_key:
            missing.append("API key")
        if not self.cfg.mobile_no:
            missing.append("mobile number")
        if missing:
            self._set_status("Still needed: " + ", ".join(missing), T.DANGER)
            return False
        return True

    def _on_login(self) -> None:
        if self._busy:
            return

        if self._otp_visible:
            otp = self.otp_entry.get().strip()
            if not otp:
                self._set_status("Enter the one time password.", T.DANGER)
                return
            self._set_busy(True)
            self._set_status("Checking the code...")
            self._run(self._work_verify, otp)
            return

        if not self._collect():
            return
        self.broker = Broker(self.cfg, self.log)
        self._set_busy(True)
        self._set_status("Contacting Choice...")
        # Read the checkbox here: a Tk variable may only be touched on the main
        # thread, so the worker is handed a plain bool.
        self._run(self._work_start, bool(self.fresh_var.get()))

    def _run(self, target, *args) -> None:
        threading.Thread(target=target, args=args, daemon=True).start()

    # ------------------------------------------------------------ worker side
    def _work_start(self, force_fresh: bool) -> None:
        try:
            self.broker.build_client()
            if not force_fresh and self.broker.resume(self.session_path):
                self.broker.load_scrip_master()
                self._events.put(("done", "Reused today's saved session."))
                return
            otp = self.broker.request_otp()
            if otp is None:
                self._events.put(("need_otp", None))
                return
            self.broker.submit_otp(otp, self.session_path)
            self._events.put(("done", "Logged in."))
        except BrokerError as exc:
            self._events.put(("error", str(exc)))
        except Exception as exc:
            self._events.put(("error", f"{type(exc).__name__}: {exc}"))

    def _work_verify(self, otp: str) -> None:
        try:
            self.broker.submit_otp(otp, self.session_path)
            self._events.put(("done", "Logged in."))
        except BrokerError as exc:
            self._events.put(("error", str(exc)))
        except Exception as exc:
            self._events.put(("error", f"{type(exc).__name__}: {exc}"))

    # --------------------------------------------------------------- main side
    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "done":
                    self._on_success(payload)
                elif kind == "need_otp":
                    self._set_busy(False)
                    self._set_status(
                        "Choice did not send the code back. Enter the one time "
                        "password from your authenticator app.", T.WARN)
                    self._show_otp()
                elif kind == "error":
                    self._set_busy(False)
                    self._set_status(payload, T.DANGER)
                    self.log.error(f"Login failed: {payload}")
        except queue.Empty:
            pass
        finally:
            # Only reschedule while the window is still there, otherwise the
            # pending callback fires against a destroyed widget on close.
            if self.winfo_exists():
                self._pump_id = self.after(150, self._pump)

    def _on_success(self, message: str) -> None:
        self.log.info(message)
        if self.remember_var.get():
            self._remember()
        self.ok = True
        self._close()

    def _close(self) -> None:
        if self._pump_id is not None:
            try:
                self.after_cancel(self._pump_id)
            except tk.TclError:
                pass
            self._pump_id = None
        self.destroy()

    def _remember(self) -> None:
        """Write the credentials back to config.json, keeping everything else."""
        try:
            self.cfg.save(self.config_path)
            self.log.info(f"Saved credentials to {self.config_path}")
        except OSError as exc:
            self.log.warn(f"Could not save config.json: {exc}")

    def _cancel(self) -> None:
        self.ok = False
        self._close()
