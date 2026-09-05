#!/usr/bin/env python3
"""
FlintDock - desktop control panel for a Minecraft Bedrock server.

Launch the packaged FlintDock.exe or run this source entry point with Python.
First-run setup selects a server stored OUTSIDE the application directory.
The historical source filename is retained for developer/test compatibility.

The launcher does not talk to bedrock_server.exe directly. It runs
server_manager.py as a child process and drives it, so scheduled restarts,
player warnings and clean shutdowns all behave exactly as they do when you
run the manager by hand.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from app_paths import worker_command, CODE_ROOT, PRODUCT_NAME, VERSION
from portal_art import portal_mark, draw_shapes, apply_window_icon
from bedrock_storage import (atomic_json, atomic_text, create_backup,
                             restore_backup, verify_backup, operation_lock, world_path)
from bedrock_runtime import ManagerClient
from launcher_features import FeatureMixin
from launcher_theme import (BG, SIDEBAR, PANEL, CARD, INPUT, LINE, FG, FG_DIM, FG_FAINT,
                            GREEN, BLUE, AMBER, RED, PURPLE, FOCUS, SELECTED, HOVER,
                            PORTAL, IGNITION, IGNITION_HOVER,
                            icon as draw_icon, WorldArtwork, FlowRow)

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox, filedialog

try:
    import launcher_health as health
    HEALTH_OK = True
except Exception:
    HEALTH_OK = False
    health = None

try:
    import bedrock_update
    UPDATE_OK = True
except Exception:            # updates are optional; the rest still runs
    UPDATE_OK = False
    bedrock_update = None

try:
    from player_history import PlayerHistory, KIND_LABEL, fmt_span
    HISTORY_OK = True
except Exception:            # the module is optional - the rest still runs
    HISTORY_OK = False
    KIND_LABEL = {}

APP_NAME = PRODUCT_NAME
POLL_STATS_MS = 3000
POLL_QUEUE_MS = 120
MAX_CONSOLE_LINES = 5000

IS_WIN = sys.platform == "win32"

# ---------------------------------------------------------------------------
# theme
# ---------------------------------------------------------------------------

# Colors and icon artwork are shared with feature dialogs in launcher_theme.py.

UI_FAMILY = "Segoe UI" if IS_WIN else "DejaVu Sans"
MONO_FAMILY = "Consolas" if IS_WIN else "DejaVu Sans Mono"

# name -> (family, base size, weight). Real tkfont.Font objects are built once
# the root window exists; changing their size updates every widget live.
FONT_SPECS = {
    "F_UI":    (UI_FAMILY, 10, "normal"),
    "F_SMALL": (UI_FAMILY, 9, "normal"),
    "F_LABEL": (UI_FAMILY, 8, "bold"),
    "F_NAV":   (UI_FAMILY, 10, "normal"),
    "F_TITLE": (UI_FAMILY, 22, "bold"),
    "F_BRAND": (UI_FAMILY, 12, "bold"),
    "F_STAT":  (UI_FAMILY, 23, "bold"),
    "F_METRIC": (UI_FAMILY, 16, "bold"),
    "F_MONO":  (MONO_FAMILY, 10, "normal"),
}

# placeholders until init_fonts() runs
F_UI = F_SMALL = F_LABEL = F_NAV = F_TITLE = F_BRAND = F_STAT = F_METRIC = F_MONO = (UI_FAMILY, 10)
_FONTS: dict[str, tkfont.Font] = {}


def init_fonts(scale: float = 1.0) -> None:
    """Create the shared Font objects. Call once, after Tk exists."""
    globals_ = globals()
    for name, (family, size, weight) in FONT_SPECS.items():
        f = tkfont.Font(family=family, size=max(7, round(size * scale)),
                        weight=weight)
        _FONTS[name] = f
        globals_[name] = f


def apply_font_scale(scale: float) -> None:
    """Resize every shared font in place; widgets update themselves."""
    for name, (_family, size, _weight) in FONT_SPECS.items():
        f = _FONTS.get(name)
        if f is not None:
            f.configure(size=max(7, round(size * scale)))


def round_rect(cv: tk.Canvas, x1, y1, x2, y2, r, **kw):
    """Rounded rectangle as a smoothed polygon - tkinter has no native one."""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


class RoundButton(tk.Canvas):
    """Flat rounded button with hover and disabled states."""

    # Text brightness is what tells a user a button is clickable. Every
    # enabled style therefore uses full-strength FG; only the disabled state
    # dims the label, and it also drops to a flatter background so the two
    # can never be confused.
    STYLES = {
        "primary": (IGNITION, IGNITION_HOVER, BG),
        "danger":  ('#452b31', '#603940', '#ffb6b6'),
        "accent":  ('#3d2857', '#543775', '#e7cfff'),
        "ghost":   (CARD, HOVER, FG),
        "quiet":   (PANEL, HOVER, FG_DIM),
    }
    DISABLED = ('#221b2b', '#9b8ca8')

    def __init__(self, master, text, command=None, kind="ghost",
                 width=110, height=36, bg=PANEL, radius=8, font=None):
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, takefocus=1)
        self.command = command
        self._focus = False
        self.kind = kind
        self.radius = radius
        self._enabled = True
        self._hover = False
        self._text = text
        self._font = font or F_UI
        self._shape = None
        self._label = None
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        # keyboard: Tab to reach it, Enter or Space to press it
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<Return>", self._on_click)
        self.bind("<KP_Enter>", self._on_click)
        self.bind("<space>", self._on_click)
        self._draw()

    def _colours(self):
        base, hover, fg = self.STYLES.get(self.kind, self.STYLES["ghost"])
        if not self._enabled:
            return self.DISABLED
        return (hover if self._hover else base), fg

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            w, h = int(self["width"]), int(self["height"])
        fill, fg = self._colours()
        self._shape = round_rect(self, 1, 1, w - 1, h - 1, self.radius,
                                 fill=fill, outline=LINE if self.kind in ('ghost', 'quiet') else '')
        if self._focus and self._enabled:
            round_rect(self, 2, 2, w - 2, h - 2, self.radius,
                       fill="", outline=FOCUS, width=2)
        self._label = self.create_text(w / 2, h / 2 + 1, text=self._text,
                                       fill=fg, font=self._font)

    def _on_enter(self, _e):
        if self._enabled:
            self._hover = True
            self._draw()
            self.configure(cursor="hand2")

    def _on_leave(self, _e):
        self._hover = False
        self._draw()
        self.configure(cursor="")

    def _on_click(self, _e=None):
        if not self._enabled:
            return "break"
        self.focus_set()
        if self.command:
            self.command()
        return "break"

    def _on_focus_in(self, _e):
        self._focus = True
        self._draw()

    def _on_focus_out(self, _e):
        self._focus = False
        self._draw()

    def set_enabled(self, value: bool) -> None:
        self._enabled = bool(value)
        self.configure(takefocus=1 if value else 0)
        self._hover = False
        self._draw()

    def set_text(self, text: str) -> None:
        self._text = text
        self._draw()


class FilterChip(tk.Canvas):
    """A rounded toggle with a live count. Used for the console level filters.

    Off is drawn flat and dim rather than hidden, so the bar always shows what
    exists - a chip reading "Errors 3" that you have switched off is a very
    different thing from no errors at all.
    """

    def __init__(self, master, label, colour=BLUE, command=None, bg=PANEL,
                 width=88, height=26):
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, takefocus=1)
        self.label = label
        self.colour = colour
        self.command = command
        self.on = True
        self.count = 0
        self._hover = False
        self._focus = False
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._click)
        self.bind("<Return>", self._click)
        self.bind("<space>", self._click)
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        self._draw()

    def _focus_in(self, _e):
        self._focus = True
        self._draw()

    def _focus_out(self, _e):
        self._focus = False
        self._draw()

    def set_count(self, n: int) -> None:
        if n != self.count:
            self.count = n
            self._draw()

    def set_on(self, value: bool) -> None:
        self.on = bool(value)
        self._draw()

    def _enter(self, _e):
        self._hover = True
        self.configure(cursor="hand2")
        self._draw()

    def _leave(self, _e):
        self._hover = False
        self.configure(cursor="")
        self._draw()

    def _click(self, _e=None):
        self.on = not self.on
        self.focus_set()
        self._draw()
        if self.command:
            self.command()
        return "break"

    def _fit(self) -> int:
        """Chips size to their own text, so counts never clip."""
        text = f"{self.label} {self.count}" if self.count else self.label
        try:
            return max(56, F_SMALL.measure(text) + 26)
        except Exception:
            return max(56, 8 * len(text) + 26)

    def _draw(self):
        want = self._fit()
        if int(self["width"]) != want:
            self.configure(width=want)
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            w, h = want, int(self["height"])
        if self.on:
            fill = SELECTED if not self._hover else HOVER
            dot, fg = self.colour, FG
        else:
            fill = INPUT if not self._hover else HOVER
            dot, fg = FG_FAINT, FG_FAINT
        round_rect(self, 1, 1, w - 1, h - 1, h / 2, fill=fill, outline="")
        if self._focus:
            round_rect(self, 2, 2, w - 2, h - 2, h / 2, fill="", outline=FOCUS,
                       width=2)
        self.create_oval(11, h / 2 - 3, 17, h / 2 + 3, fill=dot, outline="")
        text = f"{self.label} {self.count}" if self.count else self.label
        self.create_text(23, h / 2 + 1, text=text, anchor="w", fill=fg,
                         font=F_SMALL)


class NoticeBell(tk.Canvas):
    """Header bell with an unread count.

    The badge colour is the worst unread severity, so a red dot means
    something actually broke rather than just "there is news".
    """

    def __init__(self, master, command=None, bg=BG, width=44, height=32):
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, takefocus=1)
        self.command = command
        self.count = 0
        self.worst = "info"
        self._hover = False
        self._focus = False
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._click)
        self.bind("<Return>", self._click)
        self.bind("<space>", self._click)
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        self._draw()

    def set_state(self, count: int, worst: str = "info") -> None:
        if (count, worst) != (self.count, self.worst):
            self.count, self.worst = count, worst
            self._draw()

    def _enter(self, _e):
        self._hover = True
        self.configure(cursor="hand2")
        self._draw()

    def _leave(self, _e):
        self._hover = False
        self.configure(cursor="")
        self._draw()

    def _focus_in(self, _e):
        self._focus = True
        self._draw()

    def _focus_out(self, _e):
        self._focus = False
        self._draw()

    def _click(self, _e=None):
        self.focus_set()
        if self.command:
            self.command()
        return "break"

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            w, h = int(self["width"]), int(self["height"])
        round_rect(self, 0, 3, w, h - 3, 13,
                   fill="#252c3a" if self._hover else CARD, outline="")
        if self._focus:
            round_rect(self, 1, 4, w - 1, h - 4, 13, fill="", outline=FOCUS,
                       width=2)

        cx, cy = w / 2, h / 2
        ink = FG if self.count else FG_DIM
        # a bell: dome, rim, clapper
        self.create_arc(cx - 7, cy - 8, cx + 7, cy + 6, start=0, extent=180,
                        style="arc", outline=ink, width=1.6)
        self.create_line(cx - 8, cy + 5, cx + 8, cy + 5, fill=ink, width=1.6)
        self.create_line(cx - 7, cy - 1, cx - 7, cy + 5, fill=ink, width=1.6)
        self.create_line(cx + 7, cy - 1, cx + 7, cy + 5, fill=ink, width=1.6)
        self.create_oval(cx - 1.5, cy + 6, cx + 1.5, cy + 9, fill=ink,
                         outline="")

        if self.count:
            colour = {"error": RED, "warn": AMBER,
                      "ok": GREEN}.get(self.worst, BLUE)
            label = "9+" if self.count > 9 else str(self.count)
            bx = w - 11
            self.create_oval(bx - 8, 3, bx + 8, 19, fill=colour, outline=BG,
                             width=1.5)
            self.create_text(bx, 11, text=label, fill="#0d1017", font=F_LABEL)

class StatCard(tk.Canvas):
    """Compact telemetry tile with a quiet border and an accessible value."""
    def __init__(self, master, caption, colour=BLUE, height=100):
        super().__init__(master, width=1, height=height, bg=BG,
                         highlightthickness=0, bd=0, takefocus=0)
        self.caption, self.colour = caption, colour
        self.value, self.meter = "—", None
        self.bind("<Configure>", lambda e: self._draw())

    def set(self, value, meter=None):
        if self.value == value and self.meter == meter:
            return
        self.value, self.meter = value, meter
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            return
        round_rect(self, 1, 1, w-1, h-1, 10, fill=PANEL, outline=LINE)
        self.create_text(14, 18, text=self.caption, anchor="w", width=max(40, w-26),
                         fill=FG_DIM, font=F_SMALL)
        font = F_STAT if w >= 160 else F_METRIC
        if font.measure(str(self.value)) > w-28:
            font = F_UI
        self.create_text(14, 53, text=str(self.value), anchor="w", fill=FG, font=font)
        self.create_line(14, h-15, w-14, h-15, fill=LINE, width=3, capstyle="round")
        if self.meter is not None:
            end = 14 + (w-28)*max(0, min(1, self.meter))
            if end > 14:
                self.create_line(14, h-15, end, h-15, fill=self.colour, width=3, capstyle="round")
        else:
            self.create_oval(w-20, 49, w-15, 54, fill=self.colour, outline="")


class NavItem(tk.Frame):
    """Grouped navigation with consistent line icons and keyboard focus."""
    def __init__(self, master, icon, text, command, shortcut=""):
        super().__init__(master, bg=SIDEBAR, height=34, highlightthickness=1,
                         highlightbackground=SIDEBAR)
        self.command, self.icon_name = command, icon
        self.active = False
        self.bar = tk.Frame(self, bg=SIDEBAR, width=3)
        self.bar.pack(side="left", fill="y", padx=(0, 8), pady=8)
        self.ico = tk.Canvas(self, width=28, height=28, bg=SIDEBAR, highlightthickness=0)
        self.ico.pack(side="left", padx=(0, 6))
        self.lbl = tk.Label(self, text=text, bg=SIDEBAR, fg=FG_DIM, font=F_NAV, anchor="w")
        self.lbl.pack(side="left", fill="x", expand=True)
        self.shortcut = tk.Label(self, text=shortcut, bg=SIDEBAR, fg=FG_FAINT, font=F_SMALL)
        self.shortcut.pack(side="right", padx=10)
        for widget in (self, self.ico, self.lbl, self.shortcut):
            widget.bind("<Button-1>", self._click)
            widget.bind("<Enter>", self._enter)
            widget.bind("<Leave>", self._leave)
            widget.configure(cursor="hand2")
        self.configure(takefocus=1)
        self.bind("<Return>", self._click)
        self.bind("<space>", self._click)
        self.bind("<FocusIn>", lambda e: self.configure(highlightbackground=FOCUS))
        self.bind("<FocusOut>", lambda e: self.set_active(self.active))
        self.pack_propagate(False)
        self.set_active(False)

    def _click(self, _e=None):
        self.focus_set()
        self.command()
        return "break"

    def _paint(self, bg, fg, bar):
        for widget in (self, self.ico, self.lbl, self.shortcut):
            widget.configure(bg=bg)
        self.configure(highlightbackground=bg)
        self.lbl.configure(fg=fg)
        self.shortcut.configure(fg=PORTAL if self.active else FG_FAINT)
        self.bar.configure(bg=bar)
        self.ico.delete("all")
        draw_icon(self.ico, self.icon_name, color=PORTAL if self.active else fg)

    def _enter(self, _e):
        if not self.active:
            self._paint(HOVER, FG, HOVER)

    def _leave(self, _e):
        self.set_active(self.active)

    def set_active(self, value):
        self.active = value
        self._paint(SELECTED if value else SIDEBAR, FG if value else FG_DIM,
                    PORTAL if value else SIDEBAR)


# ---------------------------------------------------------------------------
# log parsing
# ---------------------------------------------------------------------------

BG_PANEL = PANEL
BG_INPUT = INPUT
ACCENT = BLUE
OK = GREEN
WARN = AMBER
BAD = RED
FONT_UI = F_UI
FONT_MONO = F_MONO
FONT_BIG = F_STAT

RE_CONNECT = re.compile(r"Player connected:\s*([^,]+),")
RE_DISCONNECT = re.compile(r"Player disconnected:\s*([^,]+),")
RE_SPAWN = re.compile(r"Player Spawned:\s*(\S+)")
RE_NEXT_RESTART = re.compile(r"next restart at (\d{4}-\d{2}-\d{2} \d{2}:\d{2})")
RE_STARTED = re.compile(r"\bServer started\b")
RE_STOPPED = re.compile(r"Quit correctly|server exited cleanly")
RE_VERSION = re.compile(r"\bVersion:\s*([\d.]+)")
RE_PACK = re.compile(r"Pack Stack - \[\d+\]\s*(.+?)\s*\(id:")
RE_STRIP = re.compile(r"§.")


def clean(text: str) -> str:
    return RE_STRIP.sub("", text)


# ---------------------------------------------------------------------------
# console log model
#
# A raw line arriving from the manager looks like this:
#
#   2026-08-28T06:00:10 [2026-08-28 06:00:10:775 WARN] [Scripting] [MGR]|sync|
#   \_ manager stamp __/ \__ the server's own stamp and level __/
#
# Two timestamps for the same second, and a level the console used to throw
# away in favour of guessing from substrings - which mis-tagged every INFO line
# with the word "error" in it. parse_line() takes the level from where the
# server actually put it, keeps one clock, and returns a record the view can
# filter without re-reading the text.
# ---------------------------------------------------------------------------

RE_MGR_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2}:\d{2})\s?")
RE_BDS_STAMP = re.compile(r"^\[(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2}):\d+\s+"
                          r"(INFO|WARN|ERROR)\]\s*")
RE_SCRIPT_TAG = re.compile(r"^\[Scripting\]\s*")
RE_MGR_CHANNEL = re.compile(r"^\[MGR\]\|([a-z]+)\|(.*)$", re.S)
RE_MANAGER_TAG = re.compile(r"^\[manager[^\]]*\]")
RE_BANNER = re.compile(r"^#+$|^#.*#$")

# Printed on every single start, and nobody has ever needed to read it. "Hide
# noise" drops these; the raw text stays in the buffer and comes back the
# moment the toggle goes off. On a real 1309-line session log these plus the
# blank lines are about 40% of everything the server printed.
NOISE_EXACT = frozenset((
    "Script event mgrback:info has been sent",
    "No targets matched selector",
    "Server Telemetry is currently not enabled.",
    "Enabling this telemetry helps us improve the game.",
    "To enable this feature, add the line 'emit-server-telemetry=true'",
    "to the server.properties file in the handheld/src-server directory",
))
NOISE_PREFIX = (
    "================ TELEMETRY MESSAGE",
    "=====================================",
)

# tags the launcher passes to log_line() itself -> a level
TAG_LEVEL = {"err": "err", "warn": "warn", "mgr": "mgr", "ok": "ok",
             "dim": "cmd", "": "info"}

LEVEL_LABEL = {"info": "INFO", "ok": "OK", "warn": "WARN", "err": "ERROR",
               "mgr": "MGR", "event": "PLAY", "cmd": "YOU"}

# chips in the filter bar: key -> (label, the levels it covers). "cmd" is
# deliberately absent - what you typed yourself is never filtered away.
FILTER_GROUPS = (
    ("info",  "Info",     ("info", "ok")),
    ("warn",  "Warnings", ("warn",)),
    ("err",   "Errors",   ("err",)),
    ("mgr",   "Manager",  ("mgr",)),
    ("event", "Players",  ("event",)),
)
LEVEL_GROUP = {lvl: key for key, _label, levels in FILTER_GROUPS
               for lvl in levels}


def now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def guess_level(body: str) -> str:
    """Only for lines the server printed without a level bracket."""
    low = body.lower()
    if "error" in low or "failed" in low or "exception" in low:
        return "err"
    if "warn" in low:
        return "warn"
    if "player connected" in low or "player spawned" in low:
        return "ok"
    return "info"


def is_noise(body: str) -> bool:
    if not body:
        return True
    if body in NOISE_EXACT or RE_BANNER.match(body):
        return True
    return body.startswith(NOISE_PREFIX)


def humanise_event(payload: str) -> str:
    """[MGR]|ev|{"p":"Steve","k":"death","d":"fell"} -> 'Steve died - fell'."""
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    who = str(data.get("p") or "?")
    kind = str(data.get("k") or "")
    detail = str(data.get("d") or "").strip()
    if kind == "chat":
        return f"{who}: {detail}"
    verb = KIND_LABEL.get(kind, kind or "did something")
    return f"{who} {verb}" + (f" - {detail}" if detail else "")


def _rec(ts, level, source, text, raw, noise) -> dict:
    return {"ts": ts, "level": level, "source": source, "text": text,
            "raw": raw, "noise": noise}


def parse_line(raw: str, tag: str = "") -> dict:
    """One console line -> a record. Never raises; the console must not die."""
    try:
        text = clean(raw).rstrip()
    except Exception:
        text = str(raw)

    # an explicit tag means the launcher wrote the line, not the server
    if tag:
        return _rec(now_hms(), TAG_LEVEL.get(tag, "info"), "launcher",
                    text.strip(), raw, False)

    ts, level, source = "", "", "server"

    m = RE_MGR_STAMP.match(text)
    if m:
        ts = m.group(1)
        text = text[m.end():]

    m = RE_BDS_STAMP.match(text)
    if m:
        ts = m.group(2)
        level = {"INFO": "info", "WARN": "warn", "ERROR": "err"}[m.group(3)]
        text = text[m.end():]

    m = RE_SCRIPT_TAG.match(text)
    if m:
        source = "script"
        text = text[m.end():]

    body = text.strip()

    # the behaviour pack's channel: [MGR]|<cmd>|<payload>
    m = RE_MGR_CHANNEL.match(body)
    if m:
        cmd, payload = m.group(1), m.group(2).strip()
        if cmd == "ev":
            human = humanise_event(payload)
            if human:
                return _rec(ts, "event", "link", human, raw, False)
        body = f"link {cmd}" + (f" - {payload}" if payload else "")
        return _rec(ts, "mgr", "link", body, raw, False)

    if RE_MANAGER_TAG.match(body):
        return _rec(ts, "mgr", "manager", body, raw, False)

    if not level:
        level = guess_level(body)
    return _rec(ts, level, source, body, raw, is_noise(body))

def short_time(ts: str) -> str:
    """'2026-08-27 19:30:00' -> '08-27 19:30:00' - the year is never in doubt."""
    return ts[5:] if len(ts) >= 16 and ts[:2] == "20" else ts


LAUNCHER_CONFIG = "launcher_ui.json"


def load_ui_config(root: Path) -> dict:
    defaults = {"geometry": "", "page": "dashboard", "scale": 1.0,
                "backup_before_restart": False,
                "console_hide_noise": True, "console_raw": False,
                "console_wrap": True, "console_levels_off": [],
                "console_history": [],
                "update_check": "launch", "update_last_check": "",
                "update_scheduled": None, "update_ignored": [], 'table_sorts': {}}
    try:
        data = json.loads((root / LAUNCHER_CONFIG).read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            defaults.update({k: v for k, v in data.items() if k in defaults})
    except (OSError, json.JSONDecodeError):
        pass
    try:
        defaults['scale'] = max(0.8, min(1.6, float(defaults['scale'])))
    except (ValueError, TypeError):
        defaults['scale'] = 1.0
    for key in ('console_history', 'console_levels_off', 'update_ignored'):
        value = defaults[key]
        defaults[key] = [v for v in value if isinstance(v, str)][:60] if isinstance(value, list) else []
    if not isinstance(defaults['geometry'], str):
        defaults['geometry'] = ''
    if not isinstance(defaults['page'], str):
        defaults['page'] = 'dashboard'
    if not isinstance(defaults['update_scheduled'], dict):
        defaults['update_scheduled'] = None
    return defaults


def save_ui_config(root: Path, cfg: dict) -> None:
    try:
        atomic_json(root / LAUNCHER_CONFIG, cfg)
    except OSError:
        pass


_logged_once: set[str] = set()


def log_once(app, message: str) -> None:
    """Report a recurring failure a single time instead of every line."""
    if message in _logged_once:
        return
    _logged_once.add(message)
    try:
        app.log_line(f"[launcher] {message}", "err")
    except Exception:
        pass
    # this only fires once per distinct message, so it cannot spam the bell
    try:
        app.notify("error", "Something is failing repeatedly", message,
                   "launcher")
    except Exception:
        pass


def human_delta(delta: timedelta) -> str:
    total = max(0, int(delta.total_seconds()))
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def parse_times(raw: str) -> list[str] | None:
    """'06:00, 14:00' -> ['06:00','14:00']; None if anything is malformed."""
    parts = [p for p in re.split(r"[,\s]+", raw.strip()) if p]
    out = []
    for p in parts:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", p)
        if not m:
            return None
        h, mi = int(m.group(1)), int(m.group(2))
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            return None
        out.append(f"{h:02d}:{mi:02d}")
    return sorted(set(out)) or None


# ---------------------------------------------------------------------------
# machine stats
# ---------------------------------------------------------------------------

class Stats:
    """Polls the bedrock_server process for CPU and memory."""

    def __init__(self):
        self.cpu_percent = 0.0
        self.mem_gb = 0.0
        self.total_gb = 0.0
        self.free_gb = 0.0
        self.found = False
        self._last_cpu_s = None
        self._last_at = None
        self._cores = os.cpu_count() or 1
        self._poll_lock = threading.Lock()
        self._pids = ()
        self.error = ''

    def poll(self) -> None:
        if not self._poll_lock.acquire(blocking=False):
            return
        try:
            if IS_WIN:
                self._poll_windows()
            else:
                self._poll_posix()
            self.error = ''
        except Exception as exc:
            self.error = str(exc)
        finally:
            self._poll_lock.release()

    def _poll_windows(self) -> None:
        from bedrock_metrics import windows_snapshot
        data = windows_snapshot()
        self.total_gb = data['total'] / 1024**3
        self.free_gb = data['free'] / 1024**3
        pids = tuple(data['pids'])
        if self._pids != pids:
            self._last_cpu_s = None
            self.cpu_percent = 0.0
            self._pids = pids
        self.found = bool(data.get("found"))
        if not self.found:
            self.cpu_percent, self.mem_gb = 0.0, 0.0
            self._last_cpu_s = None
            return
        self.mem_gb = float(data.get("ws", 0)) / (1024 ** 3)
        self._update_cpu(float(data.get("cpu", 0.0)))

    def _poll_posix(self) -> None:
        pid = None
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if "bedrock_server" in (entry / "comm").read_text():
                    pid = entry.name
                    break
            except OSError:
                continue
        try:
            info = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, _, v = line.partition(":")
                info[k] = float(v.strip().split()[0]) / 1048576.0
            self.total_gb = info.get("MemTotal", 0.0)
            self.free_gb = info.get("MemAvailable", 0.0)
        except OSError:
            pass
        self.found = pid is not None
        if not self.found:
            self.cpu_percent, self.mem_gb = 0.0, 0.0
            self._last_cpu_s = None
            return
        stat = (Path("/proc") / pid / "stat").read_text().split()
        ticks = os.sysconf("SC_CLK_TCK")
        self.mem_gb = int(stat[23]) * os.sysconf("SC_PAGE_SIZE") / (1024 ** 3)
        self._update_cpu((int(stat[13]) + int(stat[14])) / ticks)

    def _update_cpu(self, cpu_seconds: float) -> None:
        nowt = time.monotonic()
        if self._last_cpu_s is not None and nowt > self._last_at:
            delta = cpu_seconds - self._last_cpu_s
            elapsed = nowt - self._last_at
            self.cpu_percent = max(0.0, min(100.0, 100.0 * delta / elapsed / self._cores))
        self._last_cpu_s = cpu_seconds
        self._last_at = nowt


# ---------------------------------------------------------------------------
# the manager child process
# ---------------------------------------------------------------------------

class ManagerProcess:
    def __init__(self, root: Path, out_queue: queue.Queue):
        self.root = root
        self.q = out_queue
        self.proc: subprocess.Popen | None = None

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.running():
            return
        self.proc = subprocess.Popen(
            worker_command('server_manager', '--server', self.root),
            cwd=str(self.root),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            encoding='utf-8', errors='replace',
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            for line in self.proc.stdout:
                self.q.put(("line", line.rstrip("\n")))
        except (ValueError, OSError):
            pass
        self.q.put(("exit", ""))

    def send(self, text: str) -> bool:
        if not self.running() or not self.proc or not self.proc.stdin:
            return False
        try:
            self.proc.stdin.write(text.rstrip("\n") + "\n")
            self.proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def shutdown(self, wait: int = 120) -> None:
        if not self.running():
            return
        self.send("!quit")
        try:
            self.proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            self.q.put(('error', 'The server manager is still stopping. Its process was kept alive so it can finish saving the world.'))


# ---------------------------------------------------------------------------
# the application
# ---------------------------------------------------------------------------

class Launcher(FeatureMixin, tk.Tk):
    def __init__(self, root_dir: Path, *, app_update_background=True):
        super().__init__()
        self.root_dir = root_dir
        self.ui = load_ui_config(root_dir)
        init_fonts(float(self.ui.get("scale", 1.0)))
        self.title(f"{APP_NAME} - {root_dir.name}")
        apply_window_icon(self)
        self.geometry('1280x860')
        try:
            saved = self.ui.get('geometry', '')
            match = re.match(r'^(\d+)x(\d+)', saved)
            if match:
                width = min(max(980, int(match[1])), self.winfo_screenwidth())
                height = min(max(660, int(match[2])), self.winfo_screenheight() - 70)
                self.geometry(f'{width}x{height}')
        except (ValueError, tk.TclError):
            pass
        self.minsize(980, 660)
        self.configure(bg=BG)

        self.q: queue.Queue = queue.Queue()
        self.manager = ManagerClient(root_dir, self.q)
        self.stats = Stats()

        self.players: set[str] = set()
        self.packs: list[str] = []
        self.server_version = "-"
        self.started_at: datetime | None = None
        self.next_restart: datetime | None = None
        self.server_up = False
        self.status_text = "Stopped"
        self.current_page = "console"
        self.max_players = self._read_max_players()
        self.history = None
        self.history_error = ""
        if HISTORY_OK:
            try:
                self.history = PlayerHistory(root_dir)
            except Exception as exc:
                # a broken database must not stop the launcher from running
                self.history_error = str(exc)
        self.hist_player: str | None = None
        # parsed console records; see parse_line()
        self.notices: list[dict] = []
        self.notice_panel = None
        self.notice_host = None
        # the update page is built during _build_ui() and reads these, so
        # they cannot wait for _init_update_schedule() afterwards
        self.check_mode = str(self.ui.get("update_check", "launch"))
        self.scheduled_update = self.ui.get("update_scheduled") or None
        self._install_stage = ""
        self._install_since = None
        self._check_quiet = False
        self._closing = False
        self._maintenance = ''
        self._health_busy = False
        self._stopping_on_purpose = False
        self.ignored_versions = {str(v) for v in
                                 self.ui.get("update_ignored", []) or []}
        self._timers: dict[str, str] = {}
        self.console_buffer: list[dict] = []
        self._counts: dict[str, int] = {}
        self._noise_counts: dict[str, int] = {}
        self._tail_start = None    # Text index of the last drawn record
        self._tail_rec = None

        self.notices = self._load_notices()
        self._build_style()
        self._build_ui()
        self._paint_bell()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._bind_shortcuts()
        self._set_state("Stopped", RED)
        self._repeat("queue", POLL_QUEUE_MS, self._drain_queue)
        self._repeat('stats', 500, self._tick_stats)
        self._repeat("clock", 1000, self._tick_clock)
        self.load_schedule()
        self.refresh_mods()
        if UPDATE_OK:
            self._init_update_schedule()
        self.install_features()
        from launcher_app_updates import AppUpdateController
        self.app_updates = AppUpdateController(self, background=app_update_background,
            root=None if app_update_background else root_dir / '.diagnostic-app-updates')
        self.manager.attach()

    # -- styling ---------------------------------------------------------

    NAV_ORDER = ["dashboard", "console", "players", "history",
                 "schedule", "mods", "backups", "settings", "update"]

    def action_palette(self):
        """Searchable page navigation and common actions (Ctrl+K / F1)."""
        existing = getattr(self, '_palette', None)
        if existing and existing.winfo_exists():
            existing.lift()
            return
        dialog = self._palette = tk.Toplevel(self)
        dialog.title('Find an action')
        dialog.configure(bg=PANEL)
        dialog.transient(self)
        dialog.geometry(f'620x440+{self.winfo_rootx()+200}+{self.winfo_rooty()+100}')
        dialog.minsize(480, 350)
        tk.Label(dialog, text='What would you like to do?', font=F_TITLE,
                 bg=PANEL, fg=FG).pack(anchor='w', padx=20, pady=(18, 10))
        search = ttk.Entry(dialog)
        search.pack(fill='x', padx=20, pady=(0, 12))
        choices = tk.Listbox(dialog, bg=INPUT, fg=FG, selectbackground='#2b4660',
                             selectforeground=FG, font=F_UI, bd=0, highlightthickness=0,
                             activestyle='none', exportselection=False)
        choices.pack(fill='both', expand=True, padx=20)
        tk.Label(dialog, text='Type to filter  •  ↑ ↓ choose  •  Enter run  •  Esc close',
                 bg=PANEL, fg=FG_DIM, font=F_SMALL).pack(anchor='w', padx=20, pady=14)
        actions = [(f'Open {key.title()}', lambda k=key: self.show_page(k)) for key in self.NAV_ORDER]
        actions += [('Start server', self.do_start), ('Stop server', self.do_stop),
                    ('Restart with player warning', self.do_restart),
                    ('Back up world now', self.backup_now),
                    ('Export world backup…', self.backup_world),
                    ('Re-run health checks', lambda: (self.show_page('dashboard'), self.refresh_health())),
                    ('Copy diagnostic report', self.copy_diagnostics),
                    ('Open server folder', self.open_folder),
                    ('Larger text', lambda: self.bump_scale(.1)),
                    ('Smaller text', lambda: self.bump_scale(-.1)),
                    ('Reset text size', lambda: self.set_scale(1.0))]
        actions += [('Complete restore point', self.complete_backup),
                    ('Admin quick commands', self.open_admin_quick_commands),
                    ('Server command reference', self.command_help_dialog),
                    ('Recover interrupted operation', self.recovery_dialog),
                    ('Mod profiles and comparison', self.profiles_dialog),
                    ('Check pack dependencies', self.dependencies_dialog),
                    ('Preview settings changes', self.save_props),
                    ('Undo settings change', self.undo_props),
                    ('Troubleshooting report', self.incident_dialog),
                    ('Rehearse selected update', self.rehearse_update),
                    ('Maintenance scheduling', self.maintenance_options)]
        visible = []
        def filter_actions(_event=None):
            terms = search.get().casefold().split()
            visible[:] = [item for item in actions if all(t in item[0].casefold() for t in terms)]
            choices.delete(0, 'end')
            for label, _ in visible:
                choices.insert('end', '  ' + label)
            if visible:
                choices.selection_set(0)
        def move(direction):
            if not visible:
                return 'break'
            selected = choices.curselection()
            index = max(0, min(len(visible)-1, (selected[0] if selected else 0) + direction))
            choices.selection_clear(0, 'end')
            choices.selection_set(index)
            choices.see(index)
            return 'break'
        def run(_event=None):
            selected = choices.curselection()
            if selected:
                action = visible[selected[0]][1]
                dialog.destroy()
                action()
            return 'break'
        search.bind('<KeyRelease>', lambda e: filter_actions() if e.keysym not in ('Up', 'Down', 'Return') else None)
        search.bind('<Down>', lambda e: move(1))
        search.bind('<Up>', lambda e: move(-1))
        dialog.bind('<Return>', run)
        choices.bind('<Double-Button-1>', run)
        dialog.bind('<Escape>', lambda e: dialog.destroy())
        filter_actions()
        search.focus_set()

    def report_callback_exception(self, exc_type, exc_value, tb):
        report = ''.join(traceback.format_exception(exc_type, exc_value, tb))
        try:
            with (self.root_dir / 'launcher_errors.log').open('a', encoding='utf-8') as stream:
                stream.write(f'\n{datetime.now().isoformat()}\n{report}')
            self.task_status.set('An action failed. Details saved to launcher_errors.log.')
            self.log_line(f'[launcher] {exc_type.__name__}: {exc_value}', 'err')
        except Exception:
            pass

    def _bind_shortcuts(self) -> None:
        for i, key in enumerate(self.NAV_ORDER, start=1):
            self.bind_all(f"<Control-Key-{i}>", lambda e, k=key: self.show_page(k))
        self.bind_all("<F5>", lambda e: self.refresh_current())
        self.bind_all("<Control-l>", lambda e: self.clear_console())
        self.bind_all("<Control-f>", lambda e: self.focus_search())
        self.bind_all("<Control-plus>", lambda e: self.bump_scale(+0.1))
        self.bind_all("<Control-equal>", lambda e: self.bump_scale(+0.1))
        self.bind_all("<Control-minus>", lambda e: self.bump_scale(-0.1))
        self.bind_all("<Control-Key-0>", lambda e: self.set_scale(1.0))
        self.bind_all('<Control-k>', lambda e: self.action_palette())
        self.bind_all('<F1>', lambda e: self.action_palette())

    def refresh_current(self) -> None:
        page = getattr(self, "current_page", "dashboard")
        if page == "dashboard":
            self.refresh_health()
        elif page == "mods":
            self.refresh_mods()
        elif page == "history" and self.history:
            self.refresh_history()
        elif page == 'players':
            self.player_directory.refresh()
        elif page == "backups":
            self.refresh_backups()
        elif page == "settings":
            self.load_props()
        elif page == "update" and UPDATE_OK:
            self.check_updates()

    def focus_search(self) -> None:
        page = getattr(self, "current_page", "")
        if page == "console":
            self.console_search.focus_set()
        elif page == "history":
            self.hist_search.focus_set()
        elif page == 'players':
            self.player_directory.search.focus_set()
        elif page == "settings":
            self.prop_search.focus_set()

    def bump_scale(self, delta: float) -> None:
        self.set_scale(float(self.ui.get("scale", 1.0)) + delta)

    def set_scale(self, scale: float) -> None:
        scale = max(0.8, min(1.6, round(scale, 2)))
        self.ui["scale"] = scale
        apply_font_scale(scale)
        ttk.Style(self).configure('Treeview', rowheight=max(30, round(36 * scale)))
        for card in getattr(self, 'cards', {}).values():
            card._draw()
        if getattr(self, "console", None) is not None:
            self._sync_gutter()
        if hasattr(self, "scale_label"):
            self.scale_label.configure(text=f"{int(scale*100)}%")
        save_ui_config(self.root_dir, self.ui)

    def _read_max_players(self) -> int:
        try:
            for line in self.props_path.read_text(encoding="utf-8-sig",
                                                  errors="replace").splitlines():
                st = line.strip()
                if st.startswith("#") or "=" not in st:
                    continue
                k, _, v = st.partition("=")
                if k.strip() == "max-players":
                    return max(1, int(v.strip()))
        except (OSError, ValueError):
            pass
        return 10

    def _build_style(self) -> None:
        for option, value in {'*Menu.background': PANEL, '*Menu.foreground': FG,
                              '*Menu.activeBackground': SELECTED, '*Menu.activeForeground': FG,
                              '*TCombobox*Listbox.background': INPUT,
                              '*TCombobox*Listbox.foreground': FG,
                              '*TCombobox*Listbox.selectBackground': SELECTED}.items():
            self.option_add(option, value)
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=PANEL, foreground=FG, font=F_UI)
        st.configure("TFrame", background=PANEL)
        st.configure("TLabel", background=PANEL, foreground=FG)
        st.configure("Dim.TLabel", background=PANEL, foreground=FG_DIM, font=F_SMALL)
        st.configure("Title.TLabel", background=PANEL, foreground=FG, font=F_TITLE)
        st.configure("Cap.TLabel", background=PANEL, foreground=FG_DIM, font=F_LABEL)
        st.configure("TEntry", fieldbackground=INPUT, foreground=FG,
                     insertcolor=FG, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
                     borderwidth=1, padding=(10, 8))
        st.map('TEntry', bordercolor=[('focus', PORTAL)])
        st.configure('TButton', background=CARD, foreground=FG, bordercolor=LINE,
                     lightcolor=LINE, darkcolor=LINE, padding=(12, 8), relief='flat')
        st.map('TButton', background=[('pressed', SELECTED), ('active', HOVER)],
               foreground=[('disabled', FG_FAINT)])
        st.configure('TCombobox', fieldbackground=INPUT, background=CARD, foreground=FG,
                     arrowcolor=FG_DIM, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE, padding=7)
        st.map('TCombobox', fieldbackground=[('readonly', INPUT)], foreground=[('readonly', FG)])
        st.configure('TCheckbutton', background=PANEL, foreground=FG_DIM, padding=4)
        st.map('TCheckbutton', background=[('active', PANEL)], foreground=[('active', FG)])
        st.configure('Horizontal.TProgressbar', background=PORTAL, troughcolor=INPUT,
                     bordercolor=INPUT, lightcolor=PORTAL, darkcolor=PORTAL, thickness=5)
        st.configure("TSeparator", background=LINE)
        st.configure("Treeview", background=INPUT, fieldbackground=INPUT,
                     foreground=FG, borderwidth=0, relief="flat", rowheight=36,
                     font=F_UI, bordercolor=INPUT, lightcolor=INPUT,
                     darkcolor=INPUT)
        st.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        st.configure("Treeview.Heading", background=CARD, foreground=FG_DIM,
                     borderwidth=0, font=F_LABEL, padding=(10, 11))
        st.map("Treeview.Heading", background=[("active", PANEL)])
        st.map("Treeview", background=[("selected", SELECTED)],
               foreground=[("selected", FG)])
        for orient in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            st.configure(orient, background=LINE, troughcolor=INPUT,
                         bordercolor=INPUT, lightcolor=LINE,
                         darkcolor=LINE, arrowcolor=FG_DIM,
                         borderwidth=0, relief="flat", arrowsize=12)
            st.map(orient, background=[("active", FG_FAINT)])

    # -- shell -----------------------------------------------------------

    def _build_ui(self) -> None:
        self.configure(bg=BG)
        shell = tk.Frame(self, bg=BG)
        shell.pack(fill="both", expand=True)

        self._build_sidebar(shell)

        right = tk.Frame(shell, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        self._build_header(right)
        self._build_statbar(right)

        footer = tk.Frame(right, bg=BG)
        footer.pack(side='bottom', fill='x', padx=20, pady=(0, 10))
        self.task_status = tk.StringVar(value='Ready  •  Ctrl+K actions  •  F5 refresh  •  Ctrl+1–9 pages')
        self.task_label = tk.Label(footer, textvariable=self.task_status, bg=BG,
                                   fg=FG_DIM, font=F_SMALL, anchor='w')
        self.task_label.pack(side='left', fill='x', expand=True)
        self.task_progress = ttk.Progressbar(footer, mode='indeterminate', length=125)

        self.body = tk.Frame(right, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        self.body.pack(fill="both", expand=True, padx=18, pady=(0, 16))

        self.pages: dict[str, tk.Frame] = {}
        self._build_dashboard_page()
        self._build_console_page()
        self._build_players_page()
        self._build_history_page()
        self._build_schedule_page()
        self._build_mods_page()
        self._build_backups_page()
        self._build_settings_page()
        self._build_update_page()
        self.show_page(self.ui.get("page", "dashboard"))

    def _build_sidebar(self, parent) -> None:
        rail = self.sidebar = tk.Frame(parent, bg=SIDEBAR, width=208)
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)
        brand = tk.Frame(rail, bg=SIDEBAR)
        brand.pack(fill="x", padx=18, pady=(14, 8))
        mark = tk.Canvas(brand, width=38, height=46, bg=SIDEBAR, highlightthickness=0)
        mark.pack(side="left", padx=(0, 10))
        draw_shapes(mark, portal_mark(), 0, 0, 38, 46, viewbox=(64, 64))
        words = tk.Frame(brand, bg=SIDEBAR)
        words.pack(side="left")
        tk.Label(words, text="FLINTDOCK", bg=SIDEBAR, fg=FG, font=F_BRAND, anchor="w").pack(anchor="w")
        tk.Label(words, text="IGNITE YOUR WORLD", bg=SIDEBAR, fg=IGNITION,
                 font=(UI_FAMILY, 7, 'bold')).pack(anchor="w", pady=(3, 0))

        foot = tk.Frame(rail, bg=SIDEBAR)
        foot.pack(side="bottom", fill="x", padx=16, pady=8)
        RoundButton(foot, "Find an action   Ctrl+K", self.action_palette, kind="ghost",
                    width=176, bg=SIDEBAR, font=F_SMALL).pack(fill="x", pady=(0, 4))
        RoundButton(foot, "Open server folder", self.open_folder, kind="quiet",
                    width=176, height=26, bg=SIDEBAR, font=F_SMALL).pack(fill="x", pady=(0, 8))
        tk.Frame(foot, bg=LINE, height=1).pack(fill="x", pady=(0, 8))
        tk.Label(foot, text=f'FlintDock {VERSION}', bg=SIDEBAR, fg=PORTAL,
                 font=F_SMALL, anchor='w').pack(fill='x')
        self.lbl_version = tk.Label(foot, text="Bedrock —", bg=SIDEBAR, fg=FG_DIM, font=F_SMALL, anchor="w")
        self.lbl_version.pack(fill="x")
        self.lbl_world = tk.Label(foot, text="", bg=SIDEBAR, fg=FG_FAINT, font=F_SMALL, anchor="w", wraplength=170)
        self.lbl_world.pack(fill="x", pady=(3, 0))

        self.nav = {}
        groups = [("MONITOR", [("dashboard", "Overview"), ("console", "Console"), ("players", "Players"), ("history", "Activity")]),
                  ("MANAGE", [("schedule", "Schedule"), ("mods", "Addons"), ("backups", "Backups")]),
                  ("CONFIGURE", [("settings", "Settings"), ("update", "Updates")])]
        for title, entries in groups:
            tk.Label(rail, text=title, bg=SIDEBAR, fg=FG_FAINT, font=F_LABEL,
                     anchor="w").pack(fill="x", padx=22, pady=(7, 4))
            for key, label in entries:
                item = NavItem(rail, key, label, lambda k=key: self.show_page(k),
                               str(self.NAV_ORDER.index(key)+1))
                item.pack(fill="x", padx=10, pady=1)
                self.nav[key] = item

    def server_display_name(self) -> str:
        try:
            for line in self.props_path.read_text(encoding="utf-8-sig",
                                                  errors="replace").splitlines():
                st = line.strip()
                if st.startswith("#") or "=" not in st:
                    continue
                k, _, v = st.partition("=")
                if k.strip() == "server-name" and v.strip():
                    return v.strip()
        except OSError:
            pass
        return self.root_dir.name

    def _build_header(self, parent) -> None:
        head = tk.Frame(parent, bg=BG)
        head.pack(fill="x", padx=20, pady=(20, 16))
        context = tk.Frame(head, bg=BG)
        context.pack(side="left", padx=(0, 12))
        tk.Label(context, text="LOCAL SERVER", bg=BG, fg=FG_FAINT, font=F_LABEL,
                 anchor="w").pack(anchor="w")
        name = self.server_display_name()
        self.header_name = tk.Label(context, text=name if len(name) <= 19 else name[:18]+"…",
                                     bg=BG, fg=FG, font=F_BRAND, anchor="w")
        self.header_name.pack(anchor="w", pady=(4, 0))
        self.pill = tk.Canvas(head, width=128, height=36, bg=BG, highlightthickness=0)
        self.pill.pack(side="left", padx=(0, 10))
        self.bell = NoticeBell(head, self.toggle_notices, bg=BG)
        self.bell.pack(side="right", padx=(10, 0))
        self.btn_start = RoundButton(head, "Ignite server", self.do_start,
                                     kind="primary", width=126, height=40, bg=BG)
        self.btn_start.pack(side="right", padx=(8, 0))
        self.btn_restart = RoundButton(head, "Restart", self.do_restart,
                                       kind="ghost", width=88, height=40, bg=BG)
        self.btn_restart.pack(side="right", padx=(8, 0))
        self.btn_stop = RoundButton(head, "Stop", self.do_stop,
                                    kind="danger", width=72, height=40, bg=BG)
        self.btn_stop.pack(side="right")
        def fit_header(event):
            if event.width < 800:
                context.pack_forget()
            elif not context.winfo_manager():
                context.pack(side='left', padx=(0, 12), before=self.pill)
        head.bind('<Configure>', fit_header)

    def _paint_pill(self, text: str, colour: str) -> None:
        self.pill.delete("all")
        label = {"Stopped": "Offline", "Running": "Online"}.get(text, text)
        round_rect(self.pill, 1, 4, 127, 32, 14, fill=PANEL, outline=LINE)
        self.pill.create_oval(12, 14, 19, 21, fill=colour, outline="")
        self.pill.create_text(27, 18, text=label, anchor="w", fill=colour, font=F_SMALL)
        if hasattr(self, "world_state"):
            self.world_state.configure(text={"Stopped": "Ready for a spark.", "Running": "Portal lit · Online",
                "Starting": "Igniting your server…", "Stopping": "Saving your world…"}.get(text, text),
                                       fg=FG_DIM)
        if hasattr(self, 'world_artwork'):
            self.world_artwork.set_state(text)

    def _build_statbar(self, parent) -> None:
        strip = tk.Frame(parent, bg=BG)
        strip.pack(fill="x", padx=18, pady=(0, 14))
        self.stat_strip = strip
        self.cards = {}
        for key, caption, colour in (
            ("players", "Players online", GREEN),
            ("cpu", "Server CPU", BLUE),
            ("mem", "Server RAM", PURPLE),
            ("free", "System RAM free", BLUE),
            ("uptime", "Uptime", AMBER),
            ("next", "Next restart", AMBER),
        ):
            card = StatCard(strip, caption, colour)
            self.cards[key] = card
        self._stat_columns = 0
        strip.bind('<Configure>', self._layout_stats)

    def _layout_stats(self, event=None):
        columns = 6 if self.stat_strip.winfo_width() >= 680 else 3
        if columns == self._stat_columns:
            return
        self._stat_columns = columns
        for col in range(6):
            self.stat_strip.columnconfigure(col, weight=1 if col < columns else 0, uniform='stats')
        for i, card in enumerate(self.cards.values()):
            card.grid(row=i // columns, column=i % columns, sticky='nsew',
                      padx=(0, 8 if i % columns < columns - 1 else 0), pady=(0, 6))

    # -- page plumbing ----------------------------------------------------

    def _page(self, key: str) -> tk.Frame:
        frame = tk.Frame(self.body, bg=PANEL)
        self.pages[key] = frame
        return frame

    def show_page(self, key: str) -> None:
        if key not in self.pages:
            key = 'dashboard'
        for name, frame in self.pages.items():
            frame.pack_forget()
            if name in self.nav:
                self.nav[name].set_active(name == key)
        if key not in self.pages:
            key = "dashboard"
        self.pages[key].pack(fill="both", expand=True)
        self.current_page = key
        self.ui["page"] = key
        if key == "history" and self.history:
            self.refresh_history()
        elif key == 'players':
            self.player_directory.refresh()
        elif key == "dashboard":
            self.refresh_health()
        elif key == "backups":
            self.refresh_backups()
        elif key == "mods":
            self.refresh_mods()
        elif key == "update":
            self.refresh_catalogue()
            self.refresh_update()

    def _page_head(self, parent, title, subtitle=""):
        header = tk.Frame(parent, bg=PANEL)
        header.pack(fill="x", padx=24, pady=(20, 14))
        ttk.Label(header, text=title, style="Title.TLabel").pack(anchor="w")
        if subtitle:
            sub = ttk.Label(header, text=subtitle, style="Dim.TLabel", justify="left")
            sub.pack(anchor="w", fill="x", pady=(5, 0))
            header.bind("<Configure>", lambda e: sub.configure(wraplength=max(200, e.width)))
        bar = tk.Frame(header, bg=PANEL)
        bar.pack(fill="x", pady=(12, 0))
        return bar

    # -- dashboard --------------------------------------------------------

    LEVEL_COLOUR = {"ok": GREEN, "warn": AMBER, "fail": RED, "info": BLUE}
    LEVEL_MARK = {"ok": "OK", "warn": "CHECK", "fail": "PROBLEM", "info": "NOTE"}

    def _build_dashboard_page(self) -> None:
        page = self._page("dashboard")
        self._page_head(page, "Ignite your world.",
                        "Your Bedrock server, brought together in FlintDock.")
        wrap = tk.Frame(page, bg=PANEL)
        wrap.pack(fill="both", expand=True, padx=24, pady=(0, 20))

        hero = tk.Frame(wrap, bg=PANEL)
        hero.pack(fill="x")
        hero.columnconfigure(0, weight=3, uniform="overview")
        hero.columnconfigure(1, weight=2, uniform="overview")
        overview = tk.Frame(hero, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        overview.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        top = tk.Frame(overview, bg=CARD, height=108)
        top.pack(fill="x")
        top.pack_propagate(False)
        self.world_artwork = WorldArtwork(top, width=130, height=108)
        self.world_artwork.pack(side="right")
        text = tk.Frame(top, bg=CARD)
        text.pack(side="left", fill="both", expand=True, padx=(18, 0), pady=10)
        tk.Label(text, text="SERVER WORLD", bg=CARD, fg=PORTAL, font=F_LABEL,
                 anchor="w").pack(fill="x")
        world = self.level_name()
        world_name = tk.Label(text, text=world, bg=CARD, fg=FG, font=F_BRAND,
                              anchor="w", wraplength=240)
        world_name.pack(fill="x", pady=(7, 4))
        text.bind('<Configure>', lambda e: (world_name.configure(wraplength=max(80, e.width)),
                                          self.world_state.configure(wraplength=max(80, e.width))))
        self.world_state = tk.Label(text, text="Ready for a spark.", bg=CARD,
                                    fg=FG_DIM, font=F_SMALL, anchor="w")
        self.world_state.pack(fill="x")
        self.overview_label = tk.Label(overview, text="Stopped", bg=CARD,
                                       fg=FG_FAINT, font=F_SMALL, anchor="w")
        self.overview_label.pack(fill="x", padx=18, pady=(0, 7))
        self.performance_host = tk.Frame(hero, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        self.performance_host.grid(row=0, column=1, sticky="nsew")
        tk.Label(self.performance_host, text="PERFORMANCE", bg=CARD, fg=FG_DIM,
                 font=F_LABEL, anchor="w").pack(fill="x", padx=16, pady=(14, 0))

        checks = tk.Frame(wrap, bg=PANEL)
        checks.pack(fill="x", pady=(18, 8))
        tk.Label(checks, text="Server health", bg=PANEL, fg=FG, font=F_BRAND).pack(side="left")
        RoundButton(checks, "Run checks", self.refresh_health, kind="accent",
                    width=104, height=32, bg=PANEL).pack(side="right")
        RoundButton(checks, "Copy report", self.copy_diagnostics, kind="quiet",
                    width=104, height=32, bg=PANEL).pack(side="right", padx=8)
        self.health_issues_only = tk.BooleanVar(value=False)
        tk.Checkbutton(checks, text="Issues only", variable=self.health_issues_only,
                       command=lambda: self._render_health(getattr(self, "_health_results", [])),
                       bg=PANEL, fg=FG_DIM, selectcolor=INPUT, activebackground=PANEL,
                       activeforeground=FG, font=F_SMALL, bd=0).pack(side="left", padx=16)
        self.health_summary = tk.Label(wrap, text="Running checks…", bg=PANEL,
                                       fg=FG_DIM, font=F_SMALL, anchor="w")
        self.health_summary.pack(fill="x", pady=(0, 10))
        border = tk.Frame(wrap, bg=LINE)
        border.pack(fill="both", expand=True)
        inner = tk.Frame(border, bg=INPUT)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        canvas = tk.Canvas(inner, bg=INPUT, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(inner, command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.health_host = tk.Frame(canvas, bg=INPUT)
        self._health_window = canvas.create_window((0, 0), window=self.health_host, anchor="nw")
        self.health_canvas = canvas
        self.health_host.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(self._health_window, width=e.width))
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            canvas.bind_all(seq, self._scroll_health, add="+")

    def _scroll_health(self, event):
        if getattr(self, "current_page", "") != "dashboard":
            return
        delta = 0
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        elif getattr(event, "delta", 0):
            delta = -1 if event.delta > 0 else 1
        if delta:
            self.health_canvas.yview_scroll(delta, "units")

    def refresh_health(self) -> None:
        if self._health_busy:
            return
        if not HEALTH_OK:
            self.health_summary.configure(
                text="launcher_health.py is missing, so checks are unavailable.")
            return
        self.health_summary.configure(text="Running checks...")
        self._health_busy = True

        def work():
            try:
                results = health.run_checks(self.root_dir)
            except Exception as exc:
                results = [health.Result("warn", "Checks failed to run", str(exc))]
            self.q.put(("health", results))

        threading.Thread(target=work, daemon=True).start()

    def _render_health(self, results) -> None:
        self._health_busy = False
        self._health_results = results
        for child in self.health_host.winfo_children():
            child.destroy()
        counts = {"ok": 0, "warn": 0, "fail": 0, "info": 0}
        for r in results:
            counts[r.level] = counts.get(r.level, 0) + 1
        if counts["fail"]:
            # the bell dedupes on title, so a steady state raises one notice
            # and a changing count raises a fresh one
            named = "; ".join(r.title for r in results if r.level == "fail")
            self.notify("error",
                        f"{counts['fail']} problem(s) on the dashboard",
                        named[:240], "health")

        bits = []
        if counts["fail"]:
            bits.append(f"{counts['fail']} problem(s)")
        if counts["warn"]:
            bits.append(f"{counts['warn']} to check")
        if counts["info"]:
            bits.append(f"{counts['info']} note(s)")
        bits.append(f"{counts['ok']} fine")
        headline = "Everything looks healthy." if not counts["fail"] and not counts["warn"] \
            else "  |  ".join(bits)
        self.health_summary.configure(
            text=f"{headline}          checked {datetime.now():%H:%M:%S}",
            fg=RED if counts["fail"] else (AMBER if counts["warn"] else GREEN))

        for r in results:
            if self.health_issues_only.get() and r.level not in ('fail', 'warn'):
                continue
            colour = self.LEVEL_COLOUR.get(r.level, FG_DIM)
            row = tk.Frame(self.health_host, bg=INPUT)
            row.pack(fill="x", padx=12, pady=(10, 0))

            chip = tk.Canvas(row, width=76, height=20, bg=INPUT,
                             highlightthickness=0)
            chip.pack(side="left", anchor="n", pady=2)
            round_rect(chip, 0, 2, 74, 20, 5, fill=colour, outline="")
            chip.create_text(37, 11, text=self.LEVEL_MARK.get(r.level, "?"),
                             fill="#10131a", font=F_LABEL)

            text = tk.Frame(row, bg=INPUT)
            text.pack(side="left", fill="x", expand=True, padx=(10, 0))
            tk.Label(text, text=r.title, bg=INPUT, fg=FG, font=F_UI,
                     anchor="w", justify="left").pack(fill="x")
            if r.detail:
                tk.Label(text, text=r.detail, bg=INPUT, fg=FG_DIM, font=F_SMALL,
                         anchor="w", justify="left", wraplength=680).pack(fill="x")
            if r.fix:
                tk.Label(text, text=r.fix, bg=INPUT, fg=colour, font=F_SMALL,
                         anchor="w", justify="left", wraplength=680).pack(
                             fill="x", pady=(2, 0))
            if r.action in ("open_mods", "open_settings", "open_backups"):
                target = r.action.split("_", 1)[1]
                RoundButton(row, f"Go to {target.title()}",
                            lambda t=target: self.show_page(t),
                            kind="quiet", width=118, height=28, bg=INPUT,
                            font=F_SMALL).pack(side="right", anchor="n", padx=8)
            elif r.action in ('open_recovery', 'open_dependencies'):
                action = self.recovery_dialog if r.action == 'open_recovery' else self.dependencies_dialog
                RoundButton(row, 'Review', action, kind='quiet', width=118, height=28,
                            bg=INPUT, font=F_SMALL).pack(side='right', anchor='n', padx=8)
            tk.Frame(self.health_host, bg=LINE, height=1).pack(fill="x",
                                                               padx=12, pady=(10, 0))

    def copy_diagnostics(self) -> None:
        if not HEALTH_OK:
            return
        try:
            report = health.as_text(self.root_dir)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not build the report:\n{exc}")
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(report)
            where = "copied to the clipboard"
        except tk.TclError:
            where = "could not reach the clipboard"
        path = self.root_dir / "diagnostics.txt"
        try:
            path.write_text(report, encoding="utf-8")
            where += f" and saved to {path.name}"
        except OSError:
            pass
        self.log_line(f"[launcher] diagnostics {where}", "ok")
        self.health_summary.configure(text=f"Diagnostics {where}.")

    # -- update -----------------------------------------------------------

    def _build_update_page(self) -> None:
        page = self._page("update")
        bar = self._page_head(page, "Update",
                              "Which build of Bedrock this server runs, and "
                              "how to change it.")
        self.btn_check = RoundButton(bar, "Check for updates", self.check_updates,
                                     kind="quiet", width=142, bg=PANEL)
        self.btn_check.pack(side="right")
        ttk.Button(bar, text='FlintDock launcher updates…',
                   command=lambda: self.app_updates.show()).pack(side='left')

        if not UPDATE_OK:
            tk.Label(page, text="bedrock_update.py is missing from the server "
                                "folder, so updates are unavailable.",
                     bg=PANEL, fg=AMBER, font=F_UI,
                     anchor="w").pack(fill="x", padx=20, pady=8)
            return

        # -- three-up version summary ----------------------------------
        strip = tk.Frame(page, bg=LINE)
        strip.pack(fill="x", padx=20)
        inner = tk.Frame(strip, bg=PANEL)
        inner.pack(fill="x", padx=1, pady=1)

        self.ver_labels = {}
        for key, caption in (("installed", "INSTALLED"),
                             ("stable", "LATEST STABLE"),
                             ("preview", "LATEST PREVIEW")):
            cell = tk.Frame(inner, bg=CARD)
            cell.pack(side="left", fill="both", expand=True, padx=1, pady=1)
            tk.Label(cell, text=caption, bg=CARD, fg=FG_DIM, font=F_LABEL,
                     anchor="w").pack(fill="x", padx=14, pady=(12, 2))
            value = tk.Label(cell, text="-", bg=CARD, fg=FG, font=F_TITLE,
                             anchor="w")
            value.pack(fill="x", padx=14)
            note = tk.Label(cell, text="", bg=CARD, fg=FG_FAINT, font=F_SMALL,
                            anchor="w", wraplength=210, justify="left")
            note.pack(fill="x", padx=14, pady=(1, 12))
            self.ver_labels[key] = (value, note)

        # -- choose a version -------------------------------------------
        pick = tk.Frame(page, bg=PANEL)
        pick.pack(fill="both", expand=True, padx=20, pady=(16, 0))

        head = tk.Frame(pick, bg=PANEL)
        head.pack(fill="x", pady=(0, 7))
        tk.Label(head, text="Choose a version", bg=PANEL, fg=FG_DIM,
                 font=F_LABEL, anchor="w").pack(side="left")
        self.family_row = tk.Frame(head, bg=PANEL)
        self.family_row.pack(side="right")
        self.family_chips = {}
        self.family_filter = "all"

        border = tk.Frame(pick, bg=LINE)
        border.pack(fill="both", expand=True)
        holder = tk.Frame(border, bg=INPUT)
        holder.pack(fill="both", expand=True, padx=1, pady=1)

        self.version_tree = ttk.Treeview(
            holder, columns=("version", "channel", "size", "status"),
            show="headings", height=8, selectmode="browse")
        for column, title, width, anchor in (
                ("version", "VERSION", 130, "w"),
                ("channel", "CHANNEL", 90, "w"),
                ("size", "SIZE", 80, "e"),
                ("status", "STATUS", 260, "w")):
            self.version_tree.heading(column, text=title)
            self.version_tree.column(column, width=width, anchor=anchor,
                                     stretch=(column == "status"))
        bar_v = ttk.Scrollbar(holder, command=self.version_tree.yview)
        self.version_tree.configure(yscrollcommand=bar_v.set)
        bar_v.pack(side="right", fill="y")
        self.version_tree.pack(side="left", fill="both", expand=True)
        self.version_tree.bind("<<TreeviewSelect>>",
                               lambda e: self._on_version_select())
        self.version_tree.bind("<Double-1>", lambda e: self.run_update())

        self.version_tree.tag_configure("installed", foreground=GREEN)
        self.version_tree.tag_configure("newer", foreground=BLUE)
        self.version_tree.tag_configure("older", foreground=AMBER)
        self.version_tree.tag_configure("plain", foreground=FG)
        self.version_tree.tag_configure("ignored", foreground=FG_FAINT)

        # -- anything not listed ----------------------------------------
        manual = tk.Frame(pick, bg=PANEL)
        manual.pack(fill="x", pady=(10, 0))
        tk.Label(manual, text="Not listed?", bg=PANEL, fg=FG_FAINT,
                 font=F_SMALL).pack(side="left", padx=(0, 8))
        box = tk.Frame(manual, bg=INPUT)
        box.pack(side="left")
        self.version_entry = tk.Entry(box, bg=INPUT, fg=FG, font=F_MONO,
                                      borderwidth=0, highlightthickness=0,
                                      insertbackground=FG, width=16)
        self.version_entry.pack(side="left", padx=9, ipady=6)
        self.version_entry.bind("<Return>", lambda e: self.verify_version())
        RoundButton(manual, "Check", self.verify_version, kind="quiet",
                    width=70, height=28, bg=PANEL,
                    font=F_SMALL).pack(side="left", padx=(7, 0))
        self.version_note = tk.Label(manual, text="", bg=PANEL, fg=FG_FAINT,
                                     font=F_SMALL, anchor="w")
        self.version_note.pack(side="left", padx=(11, 0))

        # -- progress ---------------------------------------------------
        self.update_bar = tk.Canvas(page, height=6, bg=PANEL,
                                    highlightthickness=0, bd=0)
        self.update_bar.pack(fill="x", padx=20, pady=(18, 6))
        self.update_status = tk.Label(page, text="", bg=PANEL, fg=FG_DIM,
                                      font=F_SMALL, anchor="w")
        self.update_status.pack(fill="x", padx=20)
        self._update_frac = 0.0
        self.update_bar.bind("<Configure>", lambda e: self._paint_update_bar())

        # -- go ---------------------------------------------------------
        go = tk.Frame(page, bg=PANEL)
        go.pack(fill="x", padx=20, pady=(16, 0))
        self.btn_install = RoundButton(go, "Download and install",
                                       self.run_update, kind="primary",
                                       width=176, bg=PANEL)
        self.btn_install.pack(side="left")
        self.btn_ignore = RoundButton(go, "Ignore this version",
                                      self.toggle_ignore_version, kind="quiet",
                                      width=150, bg=PANEL)
        self.btn_ignore.pack(side="left", padx=(8, 0))
        self.install_note = tk.Label(go, text="", bg=PANEL, fg=FG_FAINT,
                                     font=F_SMALL, anchor="w")
        self.install_note.pack(side="left", padx=(12, 0))

        sched = tk.Frame(page, bg=PANEL)
        sched.pack(fill="x", padx=20, pady=(16, 0))

        auto = tk.Frame(sched, bg=PANEL)
        auto.pack(fill="x", pady=(0, 8))
        tk.Label(auto, text="Check automatically", bg=PANEL, fg=FG_DIM,
                 font=F_LABEL).pack(side="left", padx=(0, 10))
        self.check_chips = {}
        for key, label in (("off", "Never"), ("launch", "On launch"),
                           ("daily", "Daily"), ("weekly", "Weekly")):
            chip = FilterChip(auto, label, colour=BLUE,
                              command=lambda k=key: self.set_check_mode(k))
            chip.pack(side="left", padx=(0, 5))
            self.check_chips[key] = chip

        later = tk.Frame(sched, bg=PANEL)
        later.pack(fill="x")
        tk.Label(later, text="Install later", bg=PANEL, fg=FG_DIM,
                 font=F_LABEL).pack(side="left", padx=(0, 10))
        box = tk.Frame(later, bg=INPUT)
        box.pack(side="left")
        self.schedule_at = tk.Entry(box, bg=INPUT, fg=FG, font=F_MONO,
                                    borderwidth=0, highlightthickness=0,
                                    insertbackground=FG, width=7)
        self.schedule_at.insert(0, "04:00")
        self.schedule_at.pack(side="left", padx=9, ipady=6)
        RoundButton(later, "Schedule", self.schedule_update, kind="quiet",
                    width=88, height=28, bg=PANEL,
                    font=F_SMALL).pack(side="left", padx=(7, 0))
        self.btn_unschedule = RoundButton(later, "Cancel",
                                          self.cancel_scheduled_update,
                                          kind="quiet", width=70, height=28,
                                          bg=PANEL, font=F_SMALL)
        self.btn_unschedule.pack(side="left", padx=(6, 0))
        self.schedule_note = tk.Label(later, text="", bg=PANEL, fg=FG_FAINT,
                                      font=F_SMALL, anchor="w")
        self.schedule_note.pack(side="left", padx=(12, 0))

        notes = tk.Frame(page, bg=PANEL)
        notes.pack(fill="both", expand=True, padx=20, pady=(18, 18))
        for text in (
            "The world is zipped into backups/ before anything is written.",
            "server.properties, allowlist.json, permissions.json and worlds/ "
            "are never overwritten. Your packs and this tooling are left alone.",
            "The folder name stays as it is. bedrock_version.json records what "
            "is actually installed.",
            "Ignoring a version stops the notifications for that build "
            "only - a later release still gets through. 'Never' above turns "
            "checking off altogether.",
            "A scheduled install only runs while the launcher is open. It "
            "stops the server, installs, and starts it again.",
            "Going back a version is one-way in practice: Bedrock upgrades a "
            "world the first time a newer build opens it, and older builds "
            "then refuse to load it.",
        ):
            line = tk.Frame(notes, bg=PANEL)
            line.pack(fill="x", pady=2)
            tk.Label(line, text="•", bg=PANEL, fg=FG_FAINT,
                     font=F_SMALL).pack(side="left", anchor="n", padx=(0, 7))
            tk.Label(line, text=text, bg=PANEL, fg=FG_FAINT, font=F_SMALL,
                     anchor="w", justify="left", wraplength=740).pack(side="left",
                                                                     fill="x")

        self._update_busy = False
        self._available: dict = {}
        self._catalogue: list = []
        self.refresh_catalogue()
        self.refresh_update()

    # -- notifications -----------------------------------------------------
    #
    # One place for anything the launcher wants to tell you when you were not
    # looking at the page it happened on. The console already carries the
    # running commentary; this is only for things worth interrupting for -
    # an update, a failed backup, a server that stopped on its own.

    NOTICE_COLOUR = {"error": RED, "warn": AMBER, "ok": GREEN, "info": BLUE}
    NOTICE_RANK = {"info": 0, "ok": 1, "warn": 2, "error": 3}
    MAX_NOTICES = 60

    def notify(self, kind: str, title: str, detail: str = "",
               source: str = "") -> None:
        """Record something worth surfacing. Safe to call from any thread
        that owns the Tk loop; worker threads should go through the queue."""
        notice = {
            "kind": kind if kind in self.NOTICE_COLOUR else "info",
            "title": str(title),
            "detail": str(detail or ""),
            "source": str(source or ""),
            "at": datetime.now().replace(microsecond=0).isoformat(sep=" "),
            "read": False,
        }
        # collapse an identical unread notice rather than stacking duplicates
        for existing in self.notices:
            if (not existing["read"] and existing["title"] == notice["title"]
                    and existing["source"] == notice["source"]):
                existing.update(at=notice["at"], detail=notice["detail"])
                self._paint_bell()
                self._save_notices()
                return

        self.notices.insert(0, notice)
        del self.notices[self.MAX_NOTICES:]
        self._paint_bell()
        self._save_notices()
        if self.notice_panel is not None and self.notice_panel.winfo_exists():
            self._fill_notice_panel()

    def _unread(self) -> list:
        return [n for n in self.notices if not n.get("read")]

    def _paint_bell(self) -> None:
        if not hasattr(self, "bell"):
            return
        unread = self._unread()
        worst = "info"
        for notice in unread:
            if self.NOTICE_RANK.get(notice["kind"], 0) > self.NOTICE_RANK.get(worst, 0):
                worst = notice["kind"]
        self.bell.set_state(len(unread), worst)

    def _notices_path(self) -> Path:
        return self.root_dir / "notifications.json"

    def _load_notices(self) -> list:
        try:
            data = json.loads(self._notices_path().read_text(encoding="utf-8-sig"))
            if isinstance(data, list):
                return [n for n in data if isinstance(n, dict) and "title" in n]
        except (OSError, ValueError, TypeError):
            pass
        return []

    def _save_notices(self) -> None:
        try:
            atomic_json(self._notices_path(), self.notices[:self.MAX_NOTICES])
        except OSError:
            pass

    # -- the panel ---------------------------------------------------------

    def toggle_notices(self) -> None:
        if self.notice_panel is not None and self.notice_panel.winfo_exists():
            self.notice_panel.destroy()
            self.notice_panel = None
            return
        self._open_notice_panel()

    def _open_notice_panel(self) -> None:
        panel = tk.Toplevel(self)
        self.notice_panel = panel
        panel.overrideredirect(True)
        panel.configure(bg=LINE)
        panel.attributes("-topmost", True)

        self.update_idletasks()
        width, height = 430, 470
        x = self.bell.winfo_rootx() + self.bell.winfo_width() - width
        y = self.bell.winfo_rooty() + self.bell.winfo_height() + 6
        x = max(self.winfo_rootx() + 8, x)
        panel.geometry(f"{width}x{height}+{x}+{y}")

        shell = tk.Frame(panel, bg=PANEL)
        shell.pack(fill="both", expand=True, padx=1, pady=1)

        head = tk.Frame(shell, bg=PANEL)
        head.pack(fill="x", padx=14, pady=(12, 8))
        tk.Label(head, text="NOTIFICATIONS", bg=PANEL, fg=FG_DIM,
                 font=F_LABEL).pack(side="left")
        RoundButton(head, "Clear all", self.clear_notices, kind="quiet",
                    width=76, height=24, bg=PANEL,
                    font=F_SMALL).pack(side="right")
        RoundButton(head, "Mark read", self.mark_notices_read, kind="quiet",
                    width=82, height=24, bg=PANEL,
                    font=F_SMALL).pack(side="right", padx=(0, 6))

        body = tk.Frame(shell, bg=INPUT)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        canvas = tk.Canvas(body, bg=INPUT, highlightthickness=0, bd=0)
        bar = ttk.Scrollbar(body, command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.notice_host = tk.Frame(canvas, bg=INPUT)
        window = canvas.create_window((0, 0), window=self.notice_host,
                                      anchor="nw")
        self.notice_host.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(
            -1 * (e.delta // 120), "units"), add="+")

        self._fill_notice_panel()
        panel.bind("<Escape>", lambda e: self.toggle_notices())
        panel.bind("<FocusOut>", self._notice_focus_out)
        panel.focus_set()
        self.after(400, self.mark_notices_read)

    def _notice_focus_out(self, _evt=None) -> None:
        # closing on focus loss makes it behave like a real menu
        panel = self.notice_panel
        if panel is None or not panel.winfo_exists():
            return
        self.after(120, lambda: self._close_if_unfocused(panel))

    def _close_if_unfocused(self, panel) -> None:
        try:
            if not panel.winfo_exists():
                return
            if panel.focus_displayof() is None:
                panel.destroy()
                self.notice_panel = None
        except (tk.TclError, KeyError):
            pass

    def _fill_notice_panel(self) -> None:
        host = getattr(self, "notice_host", None)
        if host is None or not host.winfo_exists():
            return
        for child in host.winfo_children():
            child.destroy()

        if not self.notices:
            tk.Label(host, text="Nothing to report.", bg=INPUT, fg=FG_FAINT,
                     font=F_UI).pack(pady=28)
            return

        for notice in self.notices:
            row = tk.Frame(host, bg=INPUT)
            row.pack(fill="x", padx=12, pady=(11, 0))

            top = tk.Frame(row, bg=INPUT)
            top.pack(fill="x")
            dot = tk.Canvas(top, width=9, height=9, bg=INPUT,
                            highlightthickness=0)
            dot.pack(side="left", anchor="n", pady=5)
            colour = self.NOTICE_COLOUR.get(notice["kind"], BLUE)
            dot.create_oval(1, 1, 8, 8, fill=colour,
                            outline="" if not notice.get("read") else "")
            if notice.get("read"):
                dot.itemconfigure(1, fill=FG_FAINT)

            tk.Label(top, text=notice["title"], bg=INPUT,
                     fg=FG if not notice.get("read") else FG_DIM,
                     font=F_UI, anchor="w", justify="left",
                     wraplength=300).pack(side="left", padx=(8, 0))
            tk.Label(top, text=self._ago(notice["at"]), bg=INPUT, fg=FG_FAINT,
                     font=F_SMALL).pack(side="right", anchor="n", pady=2)

            if notice["detail"]:
                tk.Label(row, text=notice["detail"], bg=INPUT, fg=FG_FAINT,
                         font=F_SMALL, anchor="w", justify="left",
                         wraplength=372).pack(fill="x", padx=(17, 0), pady=(2, 0))
            tk.Frame(row, bg=LINE, height=1).pack(fill="x", pady=(11, 0))

    @staticmethod
    def _ago(stamp: str) -> str:
        try:
            when = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return ""
        seconds = int((datetime.now() - when).total_seconds())
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        return f"{seconds // 86400}d ago"

    def mark_notices_read(self) -> None:
        changed = False
        for notice in self.notices:
            if not notice.get("read"):
                notice["read"] = True
                changed = True
        if changed:
            self._paint_bell()
            self._save_notices()
            self._fill_notice_panel()

    def clear_notices(self) -> None:
        self.notices.clear()
        self._paint_bell()
        self._save_notices()
        self._fill_notice_panel()

    def toggle_ignore_version(self) -> None:
        """Silence one build, or start hearing about it again."""
        version = self._selected_version()
        if not version:
            messagebox.showinfo(APP_NAME, "Pick a version from the list first.")
            return
        if version in self.ignored_versions:
            self.ignored_versions.discard(version)
            self.log_line(f"[launcher] {version} is no longer ignored", "mgr")
        else:
            self.ignored_versions.add(version)
            self.log_line(f"[launcher] ignoring {version}", "mgr")
            # a notice about it now would be exactly what was just declined
            for notice in list(self.notices):
                if notice["source"] == "update" and version in notice["title"]:
                    self.notices.remove(notice)
            self._paint_bell()
            self._save_notices()
        self.ui["update_ignored"] = sorted(self.ignored_versions)
        save_ui_config(self.root_dir, self.ui)
        self.refresh_catalogue()

    # -- scheduled updates -------------------------------------------------
    #
    # Two separate things, deliberately:
    #
    #   the check    cheap, one request, safe to run unattended. It only ever
    #                raises a notification - it never installs anything.
    #   the install  stops the server, backs the world up, extracts, starts
    #                again. Only ever happens at a time you picked, for a
    #                version you picked.
    #
    # Both are driven from _tick_updates(), which runs every half minute off
    # the Tk loop. Nothing here runs if the launcher is closed.

    CHECK_EVERY = {"off": None, "launch": None, "daily": 86400,
                   "weekly": 604800}

    def _init_update_schedule(self) -> None:
        """State is set in __init__; this only starts the clock."""
        if self.check_mode not in self.CHECK_EVERY:
            self.check_mode = "launch"
        if self.check_mode == "launch":
            self._repeat("launch-check", 6000,
                         lambda: None if self._closing
                         else self.check_updates(quiet=True))
        self._repeat("updates", 4000, self._tick_updates)

    def _last_check(self):
        try:
            return datetime.strptime(str(self.ui.get("update_last_check", "")),
                                     "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return None

    def _check_is_due(self) -> bool:
        if not UPDATE_OK or self.check_mode == "off":
            return False
        last = self._last_check()
        if last is None:
            return True
        if self.check_mode == "launch":
            return False          # the one at startup already happened
        every = self.CHECK_EVERY[self.check_mode]
        return (datetime.now() - last).total_seconds() >= every

    def _tick_updates(self) -> None:
        if getattr(self, "_closing", False):
            return
        try:
            if self._check_is_due() and not self._update_busy:
                self.check_updates(quiet=True)
            self._tick_scheduled_install()
        except Exception as exc:
            log_once(self, f"update schedule error: {exc}")
        self._repeat("updates", 30000, self._tick_updates)

    # -- the scheduled install as a small state machine --------------------

    def _tick_scheduled_install(self) -> None:
        job = self.scheduled_update
        if not job or not UPDATE_OK:
            return
        try:
            jobs_file = self.root_dir / 'maintenance_jobs.json'
            jobs = json.loads(jobs_file.read_text()) if jobs_file.exists() else {}
            result = jobs.get(job['version'] + '@' + job['at'])
            if result:
                self.notify('ok' if result['state'] == 'completed' else 'warn',
                            'Scheduled update ' + result['state'], result.get('detail', ''), 'update')
                self.cancel_scheduled_update(quiet=True)
                return
        except (OSError, ValueError, KeyError):
            pass
        if self.manager.running():
            return  # The detached manager owns this schedule while running.
        try:
            due = datetime.strptime(job["at"], "%Y-%m-%d %H:%M")
        except (ValueError, TypeError, KeyError):
            self.cancel_scheduled_update(quiet=True)
            return

        if self._install_stage:
            self._advance_install(job)
            return
        if datetime.now() < due:
            return

        # more than two hours late means the launcher was closed over the
        # window; do not surprise anyone by starting it now
        if (datetime.now() - due).total_seconds() > 7200:
            self.notify("warn", f"Missed the scheduled update to {job['version']}",
                        f"It was set for {job['at']} and the launcher was not "
                        "running. Nothing was installed.", "update")
            self.cancel_scheduled_update(quiet=True)
            return

        self._begin_scheduled_install(job)

    def _begin_scheduled_install(self, job: dict) -> None:
        if self._maintenance or self._update_busy:
            return
        self._stopping_on_purpose = True
        self._check_quiet = True
        self.log_line(f"[launcher] scheduled update to {job['version']} "
                      "starting", "mgr")
        self.notify("info", f"Installing {job['version']}",
                    "The scheduled update has started. The world is backed up "
                    "first.", "update")
        self._install_since = datetime.now()
        if self.manager.running() or getattr(self.stats, "found", False):
            self._install_stage = "stopping"
            self.log_line("[launcher] stopping the server for the update", "mgr")
            if self.manager.running():
                self.send_manager("!quit")
        else:
            self._install_stage = "installing"
            self._run_scheduled_install(job)

    def _advance_install(self, job: dict) -> None:
        stage = self._install_stage
        waited = (datetime.now() - (self._install_since or datetime.now())).total_seconds()

        if stage == "stopping":
            if not self.manager.running() and not getattr(self.stats, "found", False):
                self._install_stage = "installing"
                self._run_scheduled_install(job)
            elif waited > 300:
                self._install_stage = ""
                self.notify("error", f"Could not install {job['version']}",
                            "The server would not stop within five minutes, so "
                            "the update was abandoned. Nothing was changed.",
                            "update")
                self.cancel_scheduled_update(quiet=True)
        elif stage == "restarting":
            self._install_stage = ""
            self.do_start()

    def _run_scheduled_install(self, job: dict) -> None:
        version = job["version"]
        restart = bool(job.get("restart", True))
        self._update_busy = True
        self._task_cancel.clear()
        self._task_phase = 'Preparing'

        def report(done, total, label):
            self.report_stage(done, total, label)
            frac = (done / total) if total else 0.0
            self.q.put(("update_progress",
                        (frac, f"{label}  {done * 100 // total}%" if total
                         else label)))

        def work():
            try:
                archive = bedrock_update.download(version, self.root_dir, report)
                result = bedrock_update.apply_update(self.root_dir, archive,
                                                     version, report)
                result["scheduled"] = True
                result["restart"] = restart
                self.q.put(("update_done", result))
            except Exception as exc:
                self.q.put(("update_fail", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    # -- setting and clearing the schedule ---------------------------------

    def set_check_mode(self, mode: str) -> None:
        self.check_mode = mode if mode in self.CHECK_EVERY else "off"
        self.ui["update_check"] = self.check_mode
        save_ui_config(self.root_dir, self.ui)
        for key, chip in getattr(self, "check_chips", {}).items():
            chip.set_on(key == self.check_mode)

    def schedule_update(self) -> None:
        version = self._pending_version()
        if not version:
            messagebox.showinfo(APP_NAME, "Pick a version from the list first.")
            return
        raw = self.schedule_at.get().strip()
        try:
            hour, minute = (int(p) for p in raw.split(":"))
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError
        except (ValueError, TypeError):
            messagebox.showinfo(APP_NAME,
                                "Give a time as HH:MM, for example 04:00.")
            return

        when = datetime.now().replace(hour=hour, minute=minute, second=0,
                                      microsecond=0)
        if when <= datetime.now():
            when += timedelta(days=1)

        here = bedrock_update.installed_version(self.root_dir)
        older = here and bedrock_update.compare_versions(version, here) < 0
        message = (f"Install {version} at {when:%H:%M} on "
                   f"{when:%A %d %B}?\n\n"
                   "The launcher must still be running. The server is stopped, "
                   "the world backed up, the build installed, and the server "
                   "started again.")
        if older:
            message += (f"\n\nThis is OLDER than {here}, and going back is "
                        "one-way once a world has loaded on a newer build.")
        if not messagebox.askyesno(APP_NAME, message):
            return

        self.scheduled_update = {"version": version,
                                 "at": when.strftime("%Y-%m-%d %H:%M"),
                                 "restart": True}
        self.ui["update_scheduled"] = self.scheduled_update
        save_ui_config(self.root_dir, self.ui)
        self.notify("info", f"{version} scheduled for {when:%H:%M}",
                    f"On {when:%A %d %B}. The launcher has to be open for it "
                    "to run.", "update")
        self.log_line(f"[launcher] {version} scheduled for "
                      f"{when:%Y-%m-%d %H:%M}", "ok")
        self.refresh_update()

    def cancel_scheduled_update(self, quiet: bool = False) -> None:
        job = self.scheduled_update
        self.scheduled_update = None
        self._install_stage = ""
        self.ui["update_scheduled"] = None
        save_ui_config(self.root_dir, self.ui)
        if job and not quiet:
            self.log_line(f"[launcher] scheduled update to {job['version']} "
                          "cancelled", "mgr")
        self.refresh_update()

    def _schedule_summary(self) -> str:
        job = self.scheduled_update
        if not job:
            return "nothing scheduled"
        try:
            when = datetime.strptime(job["at"], "%Y-%m-%d %H:%M")
        except (ValueError, TypeError, KeyError):
            return "nothing scheduled"
        if self._install_stage:
            return f"installing {job['version']} now ({self._install_stage})"
        return f"{job['version']} at {when:%H:%M}, {when:%a %d %b}"

    # -- the version picker ------------------------------------------------

    @staticmethod
    def _mb(size: int) -> str:
        """Sizes for the picker. A partial download is '<1 MB', not '0 MB'."""
        if not size:
            return "-"
        return f"{size / 1048576:.0f} MB" if size >= 1048576 else "<1 MB"


    def _refresh_catalogue_legacy(self) -> None:
        """Repaint the version list. Reads no network - catalogue() merges
        the known-good list, the cache, downloads/ and whatever the links
        API last reported."""
        if not UPDATE_OK or not hasattr(self, "version_tree"):
            return
        rows = bedrock_update.catalogue(self.root_dir, self._available)
        self._catalogue = rows

        families = ["all"] + sorted({r["family"] for r in rows},
                                    key=bedrock_update.parse_version,
                                    reverse=True)
        if list(self.family_chips) != families:
            for chip in self.family_chips.values():
                chip.destroy()
            self.family_chips = {}
            for family in families:
                chip = FilterChip(self.family_row,
                                  "All" if family == "all" else family,
                                  colour=BLUE,
                                  command=lambda f=family: self.set_family(f))
                chip.pack(side="left", padx=(5, 0))
                self.family_chips[family] = chip
        if self.family_filter not in self.family_chips:
            self.family_filter = "all"
        for family, chip in self.family_chips.items():
            chip.set_on(family == self.family_filter)

        keep = self.version_tree.selection()
        wanted = self.version_tree.item(keep[0], "values")[0] if keep else None
        self.version_tree.delete(*self.version_tree.get_children())

        for row in rows:
            if self.family_filter != "all" and row["family"] != self.family_filter:
                continue
            marks = []
            if row["installed"]:
                marks.append("installed now")
            elif row["relation"] > 0:
                marks.append("newer than yours")
            elif row["relation"] < 0:
                marks.append("older - one way, see below")
            if row["downloaded"]:
                marks.append("already downloaded")
            if row["version"] in self.ignored_versions:
                marks.append("ignored")
            tag = ("ignored" if row["version"] in self.ignored_versions
                   else "installed" if row["installed"]
                   else "newer" if row["relation"] > 0
                   else "older" if row["relation"] < 0 else "plain")
            self.version_tree.insert(
                "", "end", values=(
                    row["version"],
                    row["channel"],
                    self._mb(row["size"]),
                    " · ".join(marks)),
                tags=(tag,))

        if wanted:
            for item in self.version_tree.get_children():
                if self.version_tree.item(item, "values")[0] == wanted:
                    self.version_tree.selection_set(item)
                    break
        self._on_version_select()

    def set_family(self, family: str) -> None:
        self.family_filter = family
        self.refresh_catalogue()

    def _selected_version(self) -> str:
        if not hasattr(self, "version_tree"):
            return ""
        picked = self.version_tree.selection()
        if not picked:
            return ""
        return self.version_tree.item(picked[0], "values")[0]

    def _on_version_select(self) -> None:
        version = self._selected_version()
        here = bedrock_update.installed_version(self.root_dir)
        if not version:
            self.btn_install.set_text("Download and install")
        elif version == here:
            self.btn_install.set_text("Reinstall " + version)
        elif bedrock_update.compare_versions(version, here) < 0:
            self.btn_install.set_text("Go back to " + version)
        else:
            self.btn_install.set_text("Install " + version)
        self.refresh_update()

    # -- update page state -------------------------------------------------

    def _paint_update_bar(self) -> None:
        canvas = self.update_bar
        canvas.delete("all")
        width = canvas.winfo_width()
        if width <= 1:
            return
        round_rect(canvas, 0, 0, width, 6, 3, fill="#2a3140", outline="")
        if self._update_frac > 0:
            end = max(6, width * min(1.0, self._update_frac))
            round_rect(canvas, 0, 0, end, 6, 3, fill=GREEN, outline="")

    def _set_update_progress(self, frac: float, text: str = "") -> None:
        self._update_frac = frac
        self._paint_update_bar()
        if text:
            self.update_status.configure(text=text)

    def _update_can_run(self) -> tuple[bool, str]:
        """The server must be down. stats.found catches one we did not start."""
        if self._maintenance:
            return False, 'wait for maintenance to finish'
        if self._update_busy:
            return False, "busy"
        if self.manager.running():
            return False, "stop the server first"
        if getattr(self.stats, "found", False):
            return False, "bedrock_server.exe is running - stop it first"
        return True, ""

    def refresh_update(self) -> None:
        """Repaint from what we already know; no network."""
        if not UPDATE_OK or not hasattr(self, "ver_labels"):
            return
        here = bedrock_update.installed_version(self.root_dir) or "unknown"
        value, note = self.ver_labels["installed"]
        value.configure(text=here)
        marker = self.root_dir / bedrock_update.VERSION_FILE
        note.configure(text="from bedrock_version.json" if marker.exists()
                       else "read from the folder name")

        ok, why = self._update_can_run()
        self.btn_install.set_enabled(ok and bool(self._pending_version()))
        self.install_note.configure(text=why if not ok else "")

        picked = self._selected_version()
        if hasattr(self, "btn_ignore"):
            self.btn_ignore.set_enabled(bool(picked))
            self.btn_ignore.set_text(
                "Stop ignoring" if picked in self.ignored_versions
                else "Ignore this version")
        if hasattr(self, "schedule_note"):
            self.schedule_note.configure(text=self._schedule_summary())
            self.btn_unschedule.set_enabled(bool(self.scheduled_update))
        for key, chip in getattr(self, "check_chips", {}).items():
            chip.set_on(key == getattr(self, "check_mode", "launch"))

    def _pending_version(self) -> str:
        """What the install button would install: whatever is selected."""
        version = self._selected_version()
        return version if bedrock_update.valid_version(version) else ""

    def check_updates(self, quiet: bool = False) -> None:
        """quiet=True is the unattended path: no buttons move, and a failure
        is a notification rather than a dialog in front of whatever you were
        doing."""
        if self._update_busy:
            return
        self._check_quiet = quiet
        self.ui["update_last_check"] = datetime.now().replace(
            microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
        save_ui_config(self.root_dir, self.ui)
        if not quiet:
            self.btn_check.set_enabled(False)
            self._set_update_progress(0, "asking Mojang what is current...")

        def work():
            try:
                self.q.put(("update_avail", bedrock_update.fetch_available()))
            except Exception as exc:
                self.q.put(("update_fail", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _render_available(self, avail: dict) -> None:
        here = bedrock_update.installed_version(self.root_dir)
        for key in ("stable", "preview"):
            version = avail.get(key, "")
            value, note = self.ver_labels[key]
            value.configure(text=version or "-")
            if not version:
                note.configure(text="not offered", fg=FG_FAINT)
                continue
            if key == "preview":
                note.configure(text="opt-in, expect breakage", fg=FG_FAINT)
                continue
            gap = bedrock_update.compare_versions(version, here) if here else 1
            if gap > 0:
                note.configure(text="update available", fg=GREEN)
            elif gap == 0:
                note.configure(text="you are up to date", fg=FG_DIM)
            else:
                note.configure(text="older than yours", fg=AMBER)

        self._available = dict(avail)
        here = bedrock_update.installed_version(self.root_dir)
        stable_now = avail.get("stable", "")
        if (stable_now and here
                and bedrock_update.compare_versions(stable_now, here) > 0
                and stable_now not in self.ignored_versions):
            self.notify("info", f"Bedrock {stable_now} is available",
                        f"You are running {here}. Open the Update page to "
                        "install it, schedule it, or ignore this build.",
                        "update")
        self.refresh_catalogue()
        stable = avail.get("stable", "")
        if stable and not self._selected_version():
            for item in self.version_tree.get_children():
                if self.version_tree.item(item, "values")[0] == stable:
                    self.version_tree.selection_set(item)
                    self.version_tree.see(item)
                    break
        self._set_update_progress(0, "")
        self.refresh_update()

    def verify_version(self) -> None:
        version = self.version_entry.get().strip()
        if not bedrock_update.valid_version(version):
            self.version_note.configure(text="that is not a version number",
                                        fg=AMBER)
            self.refresh_update()
            return
        self.version_note.configure(text="checking...", fg=FG_FAINT)

        def work():
            try:
                state, size = bedrock_update.check_version(version)
                if state == "available":
                    bedrock_update.remember_version(self.root_dir, version, size)
                self.q.put(("update_check", (version, state, size)))
            except Exception as exc:
                self.q.put(("update_fail", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def run_update(self) -> None:
        self._check_quiet = False
        version = self._pending_version()
        if not version:
            return
        ok, why = self._update_can_run()
        if not ok:
            messagebox.showinfo(APP_NAME, why.capitalize() + ".")
            return

        here = bedrock_update.installed_version(self.root_dir)
        older = here and bedrock_update.compare_versions(version, here) < 0
        warning = (
            f"Install Bedrock {version}?\n\n"
            f"Currently installed: {here or 'unknown'}\n\n"
            "The world is backed up first. Your settings, allowlist, "
            "operators, packs and worlds are left untouched."
        )
        if older:
            warning += (
                f"\n\nThis is OLDER than {here}. Bedrock upgrades a world the "
                "first time a newer build opens it, and older builds then "
                "refuse to load it. If this world has already run on "
                f"{here}, {version} will probably not open it."
            )
        if not messagebox.askyesno(APP_NAME, warning):
            return

        self._update_busy = True
        self.btn_install.set_enabled(False)
        self.btn_check.set_enabled(False)
        self.log_line(f"[launcher] installing Bedrock {version}", "mgr")
        self._task_cancel.clear()
        self._task_phase = 'Preparing'

        def report(done, total, label):
            self.report_stage(done, total, label)
            frac = (done / total) if total else 0.0
            self.q.put(("update_progress", (frac, f"{label}  {done * 100 // total}%"
                                            if total else label)))

        def work():
            try:
                archive = bedrock_update.download(version, self.root_dir, report)
                result = bedrock_update.apply_update(self.root_dir, archive,
                                                     version, report)
                self.q.put(("update_done", result))
            except Exception as exc:
                self.q.put(("update_fail", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _update_finished(self, result: dict) -> None:
        self._update_busy = False
        self.btn_check.set_enabled(True)
        self._set_update_progress(1.0, f"installed {result['to']}")
        self.log_line(
            f"[launcher] Bedrock {result['to']} installed - "
            f"{result['written']} files written, {result['skipped']} preserved",
            "ok")
        if result.get("backup"):
            self.log_line(f"[launcher] world backed up to "
                          f"{Path(result['backup']).name}", "ok")
        self.refresh_catalogue()
        self.refresh_update()
        self.server_version = result["to"]
        self.notify("ok", f"Bedrock {result['to']} installed",
                    f"{result['written']} files written, "
                    f"{result['skipped']} preserved."
                    + (f" World backed up to "
                       f"{Path(result['backup']).name}."
                       if result.get("backup") else ""), "update")
        if result.get("scheduled"):
            self.cancel_scheduled_update(quiet=True)
            if result.get("restart"):
                self._install_stage = "restarting"
                self._install_since = datetime.now()
            return
        messagebox.showinfo(
            APP_NAME,
            f"Bedrock {result['to']} is installed.\n\n"
            f"{result['written']} files written, {result['skipped']} kept.\n"
            + (f"World backed up to backups/{Path(result['backup']).name}\n"
               if result.get("backup") else "")
            + "\nStart the server to finish - the world upgrades on first load.")

    def _update_failed(self, message: str) -> None:
        was_quiet = getattr(self, "_check_quiet", False)
        self._update_busy = False
        self._install_stage = ""
        self.btn_check.set_enabled(True)
        self._set_update_progress(0, "")
        self.log_line(f"[launcher] update failed: {message}", "err")
        self.refresh_update()
        self.notify("error", "The update did not run", message, "update")
        if self.scheduled_update:
            self.cancel_scheduled_update(quiet=True)
        # an unattended failure must not throw a modal over whatever you are
        # doing - the bell already has it
        if not was_quiet:
            messagebox.showerror(APP_NAME,
                                 f"The update did not run:\n\n{message}")

    # -- console ----------------------------------------------------------

    # Bedrock's command set with the shape of each command's arguments. Tab
    # completes the name, the hint strip under the prompt shows the rest - so
    # you stop guessing whether it is "tp" or "teleport" and what order the
    # arguments go in.
    COMMANDS = {
        "allowlist": "on | off | list | reload | add <player> | remove <player>",
        "clear": "[player] [item] [data] [maxCount]",
        "clearspawnpoint": "[player]",
        "damage": "<target> <amount> [cause]",
        "daylock": "[true|false]",
        "deop": "<player>",
        "difficulty": "peaceful | easy | normal | hard",
        "effect": "<target> <effect> [seconds] [amplifier] [hideParticles]",
        "enchant": "<player> <enchantment> [level]",
        "fill": "<from x y z> <to x y z> <block> [data] [mode]",
        "function": "<name>",
        "gamemode": "survival | creative | adventure | spectator [player]",
        "gamerule": "<rule> [value]     (bare 'gamerule' lists them all)",
        "give": "<player> <item> [amount] [data]",
        "help": "[page | command]",
        "kick": "<player> [reason]",
        "kill": "[target]",
        "list": "-  who is online right now",
        "locate": "biome <name> | structure <name>",
        "me": "<message>",
        "mobevent": "<event> [true|false]",
        "msg": "<player> <message>",
        "music": "play | queue | stop | volume <track>",
        "op": "<player>",
        "ops": "-  list the operators",
        "particle": "<name> <x y z>",
        "permission": "list | reload",
        "playsound": "<sound> [player] [x y z] [volume] [pitch]",
        "reload": "-  reload behaviour pack scripts",
        "save": "hold | query | resume",
        "say": "<message>     (everyone sees it)",
        "scoreboard": "objectives ... | players ...",
        "scriptevent": "<namespace:id> <message>",
        "setblock": "<x y z> <block> [data] [mode]",
        "setmaxplayers": "<count>",
        "setworldspawn": "[x y z]",
        "spawnpoint": "[player] [x y z]",
        "spreadplayers": "<x z> <spread> <max> <targets>",
        "stop": "-  shut the server down (prefer the Stop button)",
        "stopsound": "<player> [sound]",
        "structure": "save | load | delete",
        "summon": "<entity> [x y z] [event] [name]",
        "tag": "<target> add | remove | list [name]",
        "teleport": "<target> <x y z | destination>",
        "tell": "<player> <message>",
        "tellraw": "<target> <json>",
        "tickingarea": "add | remove | list | preload",
        "time": "set <day|night|noon|midnight|value> | add <n> | query",
        "title": "<player> title | subtitle | actionbar | clear <text>",
        "toggledownfall": "-  flip the weather",
        "tp": "<target> <x y z | destination>",
        "transfer": "<player> <host> [port]",
        "w": "<player> <message>",
        "weather": "clear | rain | thunder [duration]",
        "wsserver": "<url> | out",
        "xp": "<amount>[L] [player]",
    }

    # these never reach bedrock_server.exe - server_manager.py eats them
    MANAGER_HINTS = {
        "!restart": "restart now, with the usual one minute warning",
        "!skip": "skip the next scheduled restart",
        "!next": "print the next restart time",
        "!sync": "push the schedule to the in-game menu",
        "!schedule": "!schedule 06:00,14:00,22:00",
        "!quit": "stop the server and the manager",
    }

    # level -> (gutter colour, message colour)
    LEVEL_FG = {
        "info":  ("#5a6379", "#c6cddb"),
        "ok":    (GREEN,     "#a8e0bf"),
        "warn":  (AMBER,     "#f2d6ab"),
        "err":   (RED,       "#f7bcbc"),
        "mgr":   (BLUE,      "#b6d6f2"),
        "event": (PURPLE,    "#d4c4f0"),
        "cmd":   (GREEN,     "#e9edf5"),
    }
    CHIP_COLOUR = {"info": "#6f7a92", "warn": AMBER, "err": RED,
                   "mgr": BLUE, "event": PURPLE}

    def _build_console_page(self) -> None:
        page = self._page("console")

        # view state, restored from launcher_ui.json
        self.hide_noise = tk.BooleanVar(
            value=bool(self.ui.get("console_hide_noise", True)))
        self.show_raw = tk.BooleanVar(value=bool(self.ui.get("console_raw", False)))
        self.wrap_on = tk.BooleanVar(value=bool(self.ui.get("console_wrap", True)))
        self.filter_mode = tk.StringVar(value="filter")
        self._follow = True          # stick to the newest line
        self._new_since = 0          # lines that arrived while scrolled away
        self._counts_pending = False
        self._see_pending = False

        bar = self._page_head(page, "Console",
                              "Everything the server says, and a prompt to say "
                              "something back.")
        for label, cmd, w in (("Clear", self.clear_console, 66),
                              ("Copy", self.copy_console, 62),
                              ("Save", self.save_console, 62),
                              ("Load past log", self.load_log_file, 112)):
            RoundButton(bar, label, cmd, kind="quiet", width=w,
                        bg=PANEL).pack(side="right", padx=(6, 0))

        # -- search and filter -----------------------------------------
        tools = tk.Frame(page, bg=PANEL)
        tools.pack(fill="x", padx=20, pady=(0, 7))
        holder = tk.Frame(tools, bg=INPUT)
        holder.pack(side="left", fill="x", expand=True)
        tk.Label(holder, text="  search", bg=INPUT, fg=FG_FAINT,
                 font=F_SMALL).pack(side="left")
        self.console_search = tk.Entry(holder, bg=INPUT, fg=FG, font=F_UI,
                                       borderwidth=0, highlightthickness=0,
                                       insertbackground=FG)
        self.console_search.pack(side="left", fill="x", expand=True, padx=6,
                                 ipady=6)
        self.console_search.bind("<Return>", lambda e: self._search_enter(1))
        self.console_search.bind("<Shift-Return>", lambda e: self._search_enter(-1))
        self.console_search.bind("<KeyRelease>", self._on_search_typed)
        self.console_search.bind("<Escape>", lambda e: self.clear_search())

        # Filter narrows the log to matching lines; Find walks between them
        # without hiding the context around them. Two jobs, two buttons.
        self.mode_chips = {}
        for key, label in (("filter", "Filter"), ("find", "Find")):
            chip = FilterChip(tools, label, colour=FOCUS,
                              command=lambda k=key: self.set_search_mode(k))
            chip.pack(side="left", padx=(6, 0))
            self.mode_chips[key] = chip
        self.find_prev = RoundButton(tools, "↑", lambda: self.console_find(-1),
                                     kind="quiet", width=32, height=26, bg=PANEL,
                                     font=F_SMALL)
        self.find_next = RoundButton(tools, "↓", lambda: self.console_find(1),
                                     kind="quiet", width=32, height=26, bg=PANEL,
                                     font=F_SMALL)
        self.find_prev.pack(side="left", padx=(8, 3))
        self.find_next.pack(side="left")
        self.find_note = tk.Label(tools, text="", bg=PANEL, fg=FG_FAINT,
                                  font=F_SMALL, width=14, anchor="w")
        self.find_note.pack(side="left", padx=(8, 0))

        # -- level chips -----------------------------------------------
        chips = FlowRow(page, bg=PANEL)
        chips.pack(fill="x", padx=20, pady=(0, 9))
        off = set(self.ui.get("console_levels_off", []))
        self.level_chips = {}
        for key, label, _levels in FILTER_GROUPS:
            chip = FilterChip(chips, label, colour=self.CHIP_COLOUR.get(key, BLUE),
                              command=self._on_filter_change)
            chip.set_on(key not in off)
            chips.add(chip)
            self.level_chips[key] = chip

        self.noise_note = tk.Label(chips, text="", bg=PANEL, fg=FG_FAINT,
                                   font=F_SMALL)
        chips.add(self.noise_note)
        for var, label, cb in ((self.hide_noise, " hide noise", self._on_filter_change),
                               (self.wrap_on, " wrap", self._apply_wrap),
                               (self.show_raw, " raw", self._on_filter_change)):
            toggle = tk.Checkbutton(chips, variable=var, text=label, bg=PANEL, fg=FG_DIM,
                           font=F_SMALL, selectcolor=INPUT, activebackground=PANEL,
                           activeforeground=FG, highlightthickness=0, bd=0,
                           command=cb)
            chips.add(toggle)

        # -- the prompt, packed from the bottom up so the log can never
        #    starve it of space (pack hands leftovers to expand=True first)
        self.hint = tk.Label(page, text="", bg=PANEL, fg=FG_FAINT, font=F_SMALL,
                             anchor="w")
        self.hint.pack(side="bottom", fill="x", padx=22, pady=(0, 12))

        row = tk.Frame(page, bg=PANEL)
        row.pack(side="bottom", fill="x", padx=20, pady=(10, 2))
        prompt = tk.Frame(row, bg=INPUT)
        prompt.pack(side="left", fill="x", expand=True)
        tk.Label(prompt, text=" >", bg=INPUT, fg=GREEN,
                 font=F_MONO).pack(side="left", padx=(8, 0))
        self.entry = tk.Entry(prompt, bg=INPUT, fg=FG, insertbackground=GREEN,
                              font=F_MONO, borderwidth=0, highlightthickness=0)
        self.entry.pack(side="left", fill="x", expand=True, ipady=8, padx=6)
        self.entry.bind("<Return>", lambda e: self.send_entry())
        self.entry.bind("<Up>", self._history_prev)
        self.entry.bind("<Down>", self._history_next)
        self.entry.bind("<Tab>", self._complete)
        self.entry.bind("<KeyRelease>", self._update_hint)
        RoundButton(row, "Send", self.send_entry, kind="accent", width=84,
                    bg=PANEL).pack(side="left", padx=(8, 0))

        quick = FlowRow(page, bg=PANEL)
        quick.pack(side="bottom", fill="x", padx=20, pady=(9, 0))
        quick.add(RoundButton(quick, 'Admin quick commands', self.open_admin_quick_commands,
                              kind='accent', width=206, height=32, bg=PANEL))
        quick.add(RoundButton(quick, 'List players', lambda: self.run_admin_quick_command('list', {}),
                              kind='quiet', width=110, height=32, bg=PANEL))
        quick.add(RoundButton(quick, 'Next restart', lambda: self.run_admin_quick_command('next_restart', {}),
                              kind='quiet', width=116, height=32, bg=PANEL))
        quick.add(RoundButton(quick, 'Command help', self.command_help_dialog,
                              kind='quiet', width=136, height=32, bg=PANEL))

        # -- the log itself --------------------------------------------
        border = tk.Frame(page, bg=LINE)
        border.pack(fill="both", expand=True, padx=20)
        area = tk.Frame(border, bg=INPUT)
        area.pack(fill="both", expand=True, padx=1, pady=1)
        area.grid_rowconfigure(0, weight=1)
        area.grid_columnconfigure(0, weight=1)

        self.console = tk.Text(area, bg=INPUT, fg=FG, insertbackground=FG,
                               font=F_MONO, wrap="word", borderwidth=0,
                               highlightthickness=0, relief="flat",
                               state="disabled", padx=14, pady=10,
                               spacing1=1, spacing3=2)
        self.console.grid(row=0, column=0, sticky="nsew")
        self.console_empty = tk.Label(self.console, text='Console is quiet.\nStart your server to see live output.',
                                       bg=INPUT, fg=FG_DIM, font=F_UI, justify='center')
        self.console_empty.place(relx=.5, rely=.5, anchor='center')
        vbar = ttk.Scrollbar(area, command=self._scroll_console)
        vbar.grid(row=0, column=1, sticky="ns")
        self.console_hbar = ttk.Scrollbar(area, orient="horizontal",
                                          command=self.console.xview)
        self.console.configure(yscrollcommand=vbar.set,
                               xscrollcommand=self.console_hbar.set)

        for level, (gutter, message) in self.LEVEL_FG.items():
            self.console.tag_configure("lv_" + level, foreground=gutter)
            self.console.tag_configure("msg_" + level, foreground=message)
        self.console.tag_configure("ts", foreground=FG_FAINT)
        self.console.tag_configure("rep", foreground="#8f7ac4")
        self.console.tag_configure("hit", background="#3a4a2c")
        self.console.tag_configure("current_hit", background="#5c7a35",
                                   foreground="#ffffff")

        # any user-driven scroll re-decides whether we are following the tail
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>", "<Prior>",
                    "<Next>", "<Home>", "<End>", "<Up>", "<Down>",
                    "<B1-Motion>"):
            self.console.bind(seq, self._user_scrolled, add="+")

        self._jump = RoundButton(area, "jump to latest", self.jump_to_latest,
                                 kind="accent", width=150, height=28, bg=INPUT,
                                 font=F_SMALL)

        self.cmd_history: list[str] = [str(c) for c in
                                       self.ui.get("console_history", [])][-60:]
        self.history_at = len(self.cmd_history)
        self._tab_matches: list[str] = []
        self._tab_head = ""
        self._tab_at = 0
        self._tab_last = None

        self._apply_wrap()
        self._sync_gutter()
        self.set_search_mode(self.filter_mode.get())
        self._update_hint()

    # -- console view state ------------------------------------------------

    def _sync_gutter(self) -> None:
        """Hang wrapped lines under the message column, not the timestamp."""
        try:
            pad = F_MONO.measure("00:00:00  WARN  ")
        except Exception:
            pad = 132
        for level in self.LEVEL_FG:
            self.console.tag_configure("msg_" + level, lmargin2=pad)

    def _apply_wrap(self) -> None:
        on = self.wrap_on.get()
        self.console.configure(wrap="word" if on else "none")
        if on:
            self.console_hbar.grid_forget()
        else:
            self.console_hbar.grid(row=1, column=0, sticky="ew")
        self._save_console_prefs()

    def set_search_mode(self, mode: str) -> None:
        self.filter_mode.set(mode)
        for key, chip in self.mode_chips.items():
            chip.set_on(key == mode)
        finding = mode == "find"
        self.find_prev.set_enabled(finding)
        self.find_next.set_enabled(finding)
        self.rerender_console()

    def clear_search(self) -> None:
        self.console_search.delete(0, "end")
        self._on_search_typed()

    def _on_search_typed(self, _evt=None) -> None:
        if self.filter_mode.get() == "filter":
            self.rerender_console()
        else:
            self.console_highlight()

    def _search_enter(self, direction: int) -> None:
        if self.filter_mode.get() == "find":
            self.console_find(direction)

    def _on_filter_change(self) -> None:
        self._save_console_prefs()
        self.rerender_console()

    def _save_console_prefs(self) -> None:
        try:
            self.ui["console_hide_noise"] = bool(self.hide_noise.get())
            self.ui["console_raw"] = bool(self.show_raw.get())
            self.ui["console_wrap"] = bool(self.wrap_on.get())
            self.ui["console_levels_off"] = [k for k, c in
                                             self.level_chips.items() if not c.on]
            save_ui_config(self.root_dir, self.ui)
        except Exception:
            pass

    # -- following the tail ------------------------------------------------

    def _scroll_console(self, *args):
        self.console.yview(*args)
        self._user_scrolled()

    def _user_scrolled(self, _evt=None):
        # after_idle, so the widget has actually moved before we read yview
        self.after_idle(self._recheck_follow)

    def _recheck_follow(self) -> None:
        try:
            self._follow = self.console.yview()[1] >= 0.999
        except tk.TclError:
            return
        if self._follow:
            self._new_since = 0
        self._update_jump()

    def _update_jump(self) -> None:
        if self._follow or not self._new_since:
            self._jump.place_forget()
            return
        n = self._new_since
        label = f"↓  {n} new line{'' if n == 1 else 's'}"
        self._jump.configure(width=max(126, 8 * len(label) + 46))
        self._jump.set_text(label)
        self._jump.place(relx=0.5, rely=1.0, anchor="s", y=-14)

    def _want_tail(self) -> None:
        """One scroll per idle turn. A burst of 200 lines through the queue
        used to mean 200 separate see() calls, each re-laying out the widget."""
        if not self._see_pending:
            self._see_pending = True
            self.after_idle(self._flush_tail)

    def _flush_tail(self) -> None:
        self._see_pending = False
        if self._follow:
            try:
                self.console.see("end")
            except tk.TclError:
                pass

    def jump_to_latest(self) -> None:
        self._follow = True
        self._new_since = 0
        self.console.see("end")
        self._update_jump()

    # -- rendering ---------------------------------------------------------

    def _segments(self, rec: dict) -> list:
        """One record -> alternating (text, tags) arguments for Text.insert."""
        level = rec["level"] if rec["level"] in self.LEVEL_FG else "info"
        repeats = rec.get("n", 1)
        tail = (f"   \u00d7{repeats}" if repeats > 1 else "") + "\n"
        if self.show_raw.get():
            return [rec["raw"].rstrip(), ("msg_" + level,), tail, ("rep",)]
        return [f"{rec['ts'] or '--:--:--'}  ", ("ts",),
                f"{LEVEL_LABEL.get(level, level):<5} ", ("lv_" + level,),
                rec["text"], ("msg_" + level,), tail, ("rep",)]

    def _visible(self, rec: dict) -> bool:
        if rec["level"] == "cmd":
            return True                      # never hide what you typed
        if self.hide_noise.get() and rec["noise"]:
            return False
        chip = self.level_chips.get(LEVEL_GROUP.get(rec["level"], "info"))
        if chip is not None and not chip.on:
            return False
        if self.filter_mode.get() == "filter":
            term = self.console_search.get().strip().lower()
            if len(term) >= 2 and term not in rec["text"].lower() \
                    and term not in rec["raw"].lower():
                return False
        return True

    def rerender_console(self) -> None:
        """Redraw from the buffer in a single insert, honouring every filter."""
        records = [r for r in self.console_buffer if self._visible(r)]
        records = records[-MAX_CONSOLE_LINES:]
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self._tail_start = None
        self._tail_rec = None
        if records:
            head: list = []
            for rec in records[:-1]:
                head.extend(self._segments(rec))
            if head:
                self.console.insert("end", *head)
            self._tail_start = self.console.index("end-1c")
            self._tail_rec = records[-1]
            self.console.insert("end", *self._segments(records[-1]))
        self.console.configure(state="disabled")
        self._follow = True
        self._new_since = 0
        self.console.see("end")
        self._update_jump()
        self.console_highlight()
        self._paint_counts()
        if records:
            self.console_empty.place_forget()
        else:
            self.console_empty.configure(text='No matching output.\nTry another search or reset the filters.'
                if self.console_buffer else 'Console is quiet.\nStart your server to see live output.')
            self.console_empty.place(relx=.5, rely=.5, anchor='center')
        if self.filter_mode.get() == "filter":
            term = self.console_search.get().strip()
            self.find_note.configure(
                text=f"{len(records)} of {len(self.console_buffer)}"
                if len(term) >= 2 else "")

    def _append_record(self, rec: dict) -> None:
        self.console_empty.place_forget()
        self.console.configure(state="normal")
        self._tail_start = self.console.index("end-1c")
        self._tail_rec = rec
        self.console.insert("end", *self._segments(rec))
        over = int(self.console.index("end-1c").split(".")[0]) - MAX_CONSOLE_LINES
        if over > 0:
            self.console.delete("1.0", f"{over + 1}.0")
            self._tail_start = None      # indices shifted, stop collapsing
        self.console.configure(state="disabled")
        if self._follow:
            self._want_tail()
        else:
            self._new_since += 1
            self._update_jump()

    def _redraw_tail(self, rec: dict) -> None:
        """Rewrite the last drawn line in place, for the xN counter."""
        if self._tail_start is None or self._tail_rec is not rec:
            return
        self.console.configure(state="normal")
        self.console.delete(self._tail_start, "end-1c")
        self.console.insert("end", *self._segments(rec))
        self.console.configure(state="disabled")
        if self._follow:
            self._want_tail()

    # -- counts ------------------------------------------------------------

    def _count_delta(self, rec: dict, delta: int) -> None:
        key = LEVEL_GROUP.get(rec["level"])
        if not key:
            return
        target = self._noise_counts if rec["noise"] else self._counts
        target[key] = target.get(key, 0) + delta

    def _recount(self) -> None:
        self._counts = {}
        self._noise_counts = {}
        for rec in self.console_buffer:
            self._count_delta(rec, 1)
        self._paint_counts()

    def _schedule_counts(self) -> None:
        """Repaint the chips a few times a second, not once per line."""
        if not self._counts_pending:
            self._counts_pending = True
            self.after(250, self._flush_counts)

    def _flush_counts(self) -> None:
        self._counts_pending = False
        try:
            self._paint_counts()
        except tk.TclError:
            pass

    def _paint_counts(self) -> None:
        show_noise = not self.hide_noise.get()
        hidden = 0
        for key, chip in self.level_chips.items():
            quiet = self._noise_counts.get(key, 0)
            hidden += quiet
            chip.set_count(self._counts.get(key, 0) + (quiet if show_noise else 0))
        self.noise_note.configure(
            text=f"{hidden} noise hidden" if hidden and not show_noise else "")

    # -- clear, copy, save -------------------------------------------------

    def clear_console(self) -> None:
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self.console.configure(state="disabled")
        self.console_buffer.clear()
        self._counts = {}
        self._noise_counts = {}
        self._tail_start = None
        self._tail_rec = None
        self._new_since = 0
        self._follow = True
        self._paint_counts()
        self._update_jump()
        self.find_note.configure(text="")
        self.console_empty.configure(text='Console is quiet.\nStart your server to see live output.')
        self.console_empty.place(relx=.5, rely=.5, anchor='center')

    def _visible_text(self) -> str:
        return "\n".join(
            (r["raw"].rstrip() if self.show_raw.get()
             else f"{r['ts'] or '--:--:--'}  "
                  f"{LEVEL_LABEL.get(r['level'], r['level']):<5} {r['text']}")
            + (f"   x{r['n']}" if r.get("n", 1) > 1 else "")
            for r in self.console_buffer if self._visible(r))

    def copy_console(self) -> None:
        """Copy exactly what is on screen - filters included."""
        text = self._visible_text()
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.find_note.configure(text=f"{text.count(chr(10)) + 1} lines copied")

    def save_console(self) -> None:
        dest = filedialog.asksaveasfilename(
            title="Save the console", initialdir=str(self.root_dir),
            initialfile=f"console-{datetime.now():%Y%m%d-%H%M%S}.txt",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not dest:
            return
        try:
            Path(dest).write_text(self._visible_text() + "\n", encoding="utf-8")
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not write it:\n{exc}")
            return
        self.log_line(f"[launcher] console saved to {Path(dest).name}", "ok")

    # -- find --------------------------------------------------------------

    def console_highlight(self) -> None:
        """Mark every match so they stay visible while scrolling."""
        self.console.tag_remove("hit", "1.0", "end")
        term = self.console_search.get()
        if len(term) < 2:
            if self.filter_mode.get() == "find":
                self.find_note.configure(text="")
            return
        count, idx = 0, "1.0"
        while count <= 500:
            idx = self.console.search(term, idx, nocase=True, stopindex="end")
            if not idx:
                break
            end = f"{idx}+{len(term)}c"
            self.console.tag_add("hit", idx, end)
            idx = end
            count += 1
        if self.filter_mode.get() == "find":
            self.find_note.configure(
                text=f"{count} match" + ("" if count == 1 else "es"))

    def console_find(self, direction: int = 1) -> None:
        term = self.console_search.get()
        if not term:
            return
        self.console_highlight()
        start = self.console.index("insert")
        if direction > 0:
            idx = self.console.search(term, f"{start}+1c", nocase=True,
                                      stopindex="end")
            if not idx:
                idx = self.console.search(term, "1.0", nocase=True, stopindex="end")
        else:
            idx = self.console.search(term, start, nocase=True, stopindex="1.0",
                                      backwards=True)
            if not idx:
                idx = self.console.search(term, "end", nocase=True,
                                          stopindex="1.0", backwards=True)
        if not idx:
            return
        end = f"{idx}+{len(term)}c"
        self.console.tag_remove("current_hit", "1.0", "end")
        self.console.tag_add("current_hit", idx, end)
        self.console.mark_set("insert", idx)
        self.console.see(idx)
        self._follow = False
        self._update_jump()

    # -- the prompt --------------------------------------------------------

    def _hint(self, text: str, colour: str = FG_FAINT) -> None:
        self.hint.configure(text=text, fg=colour)

    def _update_hint(self, evt=None) -> None:
        if evt is not None and getattr(evt, "keysym", "") == "Tab":
            return
        self._tab_last = None
        text = self.entry.get().strip()
        word = text.split(" ")[0].lower() if text else ""
        if not word:
            self._hint("Tab completes  ·  Up and Down walk your history  "
                       "·  !restart !skip !next !sync go to the manager")
        elif word in self.MANAGER_HINTS:
            self._hint(self.MANAGER_HINTS[word], BLUE)
        elif word in self.COMMANDS:
            self._hint(f"{word}   {self.COMMANDS[word]}", FG_DIM)
        else:
            near = [c for c in self.COMMANDS if c.startswith(word)][:8]
            self._hint("try:  " + "   ".join(near) if near
                       else "not a command I know - it will be sent as typed")

    def _complete(self, _evt=None):
        """Tab completes command names, then player names for the arguments."""
        current = self.entry.get()
        if self._tab_last == current and self._tab_matches:
            self._tab_at = (self._tab_at + 1) % len(self._tab_matches)
        else:
            head, sep, stub = current.rpartition(" ")
            pool = (sorted(self.players) if sep
                    else sorted(self.COMMANDS) + sorted(self.MANAGER_HINTS))
            low = stub.lower()
            self._tab_matches = [w for w in pool if w.lower().startswith(low)]
            self._tab_head = head + sep
            self._tab_at = 0
        if not self._tab_matches:
            self._hint("nothing to complete")
            return "break"
        pick = self._tab_matches[self._tab_at]
        self.entry.delete(0, "end")
        self.entry.insert(0, self._tab_head + pick)
        if len(self._tab_matches) > 1:
            self._hint(f"{self._tab_at + 1}/{len(self._tab_matches)}   "
                       + "   ".join(self._tab_matches[:10]), FG_DIM)
        else:
            self._update_hint()
        self._tab_last = self.entry.get()
        return "break"

    # -- past logs ---------------------------------------------------------

    def load_log_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Open a log file", initialdir=str(self.root_dir),
            filetypes=[("Log files", "*.log *.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            lines = Path(path).read_text(encoding="utf-8",
                                         errors="replace").splitlines()
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not read it:\n{exc}")
            return
        tail = lines[-MAX_CONSOLE_LINES:]
        self.clear_console()
        self.console_buffer.append(
            parse_line(f"[launcher] last {len(tail)} lines of "
                       f"{Path(path).name}", "mgr"))
        self.console_buffer.extend(parse_line(line) for line in tail)
        self._recount()
        self.rerender_console()

    # -- players ----------------------------------------------------------

    def _build_players_page(self) -> None:
        from launcher_players import PlayerDirectory
        page = self._page('players')
        self._page_head(page, 'Players', 'Everyone seen by this server — online and offline.')
        holder = tk.Frame(page, bg=PANEL)
        holder.pack(fill='both', expand=True, padx=12, pady=(0, 12))
        self.player_canvas = tk.Canvas(holder, bg=PANEL, highlightthickness=0)
        scroll = ttk.Scrollbar(holder, command=self.player_canvas.yview)
        scroll.pack(side='right', fill='y')
        self.player_canvas.pack(side='left', fill='both', expand=True)
        self.player_canvas.configure(yscrollcommand=scroll.set)
        self.player_directory = PlayerDirectory(self, self.player_canvas)
        window = self.player_canvas.create_window((0, 0), window=self.player_directory, anchor='nw')
        def size_directory(event):
            height = max(540, event.height)
            self.player_canvas.itemconfigure(window, width=event.width, height=height)
            self.player_canvas.configure(scrollregion=(0, 0, event.width, height))
        self.player_canvas.bind('<Configure>', size_directory)
        def scroll_directory(event):
            if (self.current_page == 'players' and str(event.widget).startswith(str(self.player_directory))
                    and event.widget.winfo_class() not in ('Treeview', 'Listbox', 'TCombobox', 'TScrollbar')):
                steps = int(-event.delta / 120) or (-1 if event.delta > 0 else 1)
                self.player_canvas.yview_scroll(steps, 'units')
        self.bind_all('<MouseWheel>', scroll_directory, add='+')


    # -- history ----------------------------------------------------------

    def _build_history_page(self) -> None:
        page = self._page("history")
        bar = self._page_head(page, "History",
                              "Everything the server has seen, per player. "
                              "Queue commands to run on their next join.")
        RoundButton(bar, "Refresh", self.refresh_history, kind="quiet",
                    width=82, bg=PANEL).pack(side="right", padx=(6, 0))
        RoundButton(bar, "Export CSV", self.export_history, kind="quiet",
                    width=104, bg=PANEL).pack(side="right", padx=6)

        if not self.history:
            reason = ("player_history.py is missing from the server folder."
                      if not HISTORY_OK else
                      f"The history database could not be opened:\n{self.history_error}")
            tk.Label(page, bg=PANEL, fg=AMBER, font=F_UI, justify="left",
                     text=f"{reason}\n\nHistory and the command queue are off. "
                          "Everything else works normally."
                     ).pack(anchor="w", padx=22, pady=20)
            return

        split = tk.Frame(page, bg=PANEL)
        split.pack(fill="both", expand=True, padx=20, pady=(0, 18))

        # ---- left: known players -------------------------------------
        lb = tk.Frame(split, bg=LINE, width=250)
        lb.pack(side="left", fill="y")
        lb.pack_propagate(False)
        inner = tk.Frame(lb, bg=INPUT)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(inner, text="  PLAYERS SEEN", bg=INPUT, fg=FG_DIM,
                 font=F_LABEL, anchor="w").pack(fill="x", pady=(8, 4))
        self.hist_tree = ttk.Treeview(inner, columns=("seen",), show="tree headings",
                                      selectmode="browse")
        self.hist_tree.heading("#0", text="NAME")
        self.hist_tree.heading("seen", text="PLAYTIME")
        self.hist_tree.column("#0", width=150)
        self.hist_tree.column("seen", width=80, anchor="e")
        self.hist_tree.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.hist_tree.tag_configure("on", foreground=GREEN)
        self.hist_tree.tag_configure("off", foreground=FG)
        self.hist_tree.bind("<<TreeviewSelect>>", self._on_pick_player)

        # ---- right: filters, timeline, queue -------------------------
        right = tk.Frame(split, bg=PANEL)
        right.pack(side="left", fill="both", expand=True, padx=(14, 0))

        # two rows: search + scope on top, filter chips underneath, so the
        # bar can never overflow however narrow the window gets
        top = tk.Frame(right, bg=PANEL)
        top.pack(fill="x", pady=(0, 6))
        holder = tk.Frame(top, bg=INPUT)
        holder.pack(side="left", fill="x", expand=True)
        tk.Label(holder, text="  search", bg=INPUT, fg=FG_FAINT,
                 font=F_SMALL).pack(side="left")
        self.hist_search = tk.Entry(holder, bg=INPUT, fg=FG, font=F_UI,
                                    borderwidth=0, highlightthickness=0,
                                    insertbackground=FG)
        self.hist_search.pack(side="left", fill="x", expand=True, padx=6, ipady=6)
        self.hist_search.bind("<Return>", lambda e: self.refresh_history())

        filt = tk.Frame(right, bg=PANEL)
        filt.pack(fill="x", pady=(0, 8))

        self.hist_scope = tk.StringVar(value="all")
        self.scope_btn = RoundButton(top, "All players", self._toggle_scope,
                                     kind="quiet", width=112, height=30,
                                     bg=PANEL, font=F_SMALL)
        self.scope_btn.pack(side="right", padx=(8, 0))

        self.hist_kind = tk.StringVar(value="all")
        self.kind_buttons: dict[str, RoundButton] = {}
        for label, value in (("All", "all"), ("Joins", "join,leave"),
                             ("Deaths", "death"), ("Chat", "chat"),
                             ("Console", "console"), ("Queue", "queue,command")):
            btn = RoundButton(filt, label, lambda v=value: self._set_kind(v),
                              kind="quiet", width=max(56, 8 * len(label) + 24),
                              height=28, bg=PANEL, font=F_SMALL)
            btn.pack(side="left", padx=(0, 6))
            self.kind_buttons[value] = btn

        # ---- queue ----------------------------------------------------
        qbox = tk.Frame(right, bg=CARD)
        qbox.pack(side="bottom", fill="x", pady=(12, 0))
        qin = tk.Frame(qbox, bg=CARD)
        qin.pack(fill="x", padx=14, pady=12)
        self.q_title = tk.Label(qin, text="QUEUE  -  pick a player", bg=CARD,
                                fg=FG_DIM, font=F_LABEL, anchor="w")
        self.q_title.pack(fill="x")
        tk.Label(qin, text="Runs automatically a few seconds after they next join. "
                           "@s becomes their name.",
                 bg=CARD, fg=FG_FAINT, font=F_SMALL, anchor="w").pack(fill="x",
                                                                      pady=(2, 8))
        row = tk.Frame(qin, bg=CARD)
        row.pack(fill="x")
        eh = tk.Frame(row, bg=INPUT)
        eh.pack(side="left", fill="x", expand=True)
        self.q_entry = tk.Entry(eh, bg=INPUT, fg=FG, font=F_MONO, borderwidth=0,
                                highlightthickness=0, insertbackground=GREEN)
        self.q_entry.pack(fill="x", padx=8, ipady=7)
        self.q_entry.bind("<Return>", lambda e: self.queue_add())
        RoundButton(row, "Queue it", self.queue_add, kind="accent", width=96,
                    bg=CARD).pack(side="left", padx=(8, 0))
        RoundButton(row, "Remove", self.queue_remove, kind="quiet", width=90,
                    bg=CARD).pack(side="left", padx=(6, 0))
        self.q_list = tk.Listbox(qin, bg=INPUT, fg=FG, borderwidth=0, height=4,
                                 highlightthickness=0, font=F_MONO,
                                 selectbackground="#2b3446", selectforeground=FG,
                                 activestyle="none")
        self.q_list.pack(fill="x", pady=(8, 0))

        tb = tk.Frame(right, bg=LINE)
        tb.pack(fill="both", expand=True)
        tw = tk.Frame(tb, bg=INPUT)
        tw.pack(fill="both", expand=True, padx=1, pady=1)
        cols = ("when", "who", "what", "detail")
        self.ev_tree = ttk.Treeview(tw, columns=cols, show="headings")
        for key, title, width, anchor in (
            ("when", "WHEN", 118, "w"), ("who", "PLAYER", 132, "w"),
            ("what", "EVENT", 88, "w"), ("detail", "DETAIL", 300, "w"),
        ):
            self.ev_tree.heading(key, text=title)
            self.ev_tree.column(key, width=width, anchor=anchor)
        sb = ttk.Scrollbar(tw, command=self.ev_tree.yview)
        self.ev_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.ev_tree.pack(fill="both", expand=True, padx=(4, 0), pady=4)
        for kind, colour in (("join", GREEN), ("leave", FG_DIM), ("death", RED),
                             ("chat", BLUE), ("queue", PURPLE),
                             ("console", FG)):
            self.ev_tree.tag_configure(kind, foreground=colour)

    def _set_kind(self, value: str) -> None:
        self.hist_kind.set(value)
        for key, btn in self.kind_buttons.items():
            btn.kind = "accent" if key == value else "quiet"
            btn._draw()
        self.refresh_history()

    def _toggle_scope(self) -> None:
        one = self.hist_scope.get() != "one"
        self.hist_scope.set("one" if one else "all")
        self.scope_btn.kind = "accent" if one else "quiet"
        self.scope_btn.set_text(
            (self.hist_player or "This player") if one else "All players")
        self.refresh_history()

    def _on_pick_player(self, _evt=None) -> None:
        sel = self.hist_tree.selection()
        if not sel:
            return
        self.hist_player = self.hist_tree.item(sel[0], "text").strip()
        if self.hist_scope.get() == "one":
            self.scope_btn.set_text(self.hist_player)
        self.refresh_queue()
        if self.hist_scope.get() == "one":
            self.refresh_history(keep_selection=True)

    def _refresh_history_legacy(self, keep_selection: bool = False) -> None:
        if not self.history:
            return
        try:
            people = self.history.players()
        except Exception as exc:
            log_once(self, f"history read failed: {exc}")
            return

        if not keep_selection:
            for item in self.hist_tree.get_children():
                self.hist_tree.delete(item)
            for p in people:
                self.hist_tree.insert(
                    "", "end", text=f" {p['name']}", values=(p["playtime"],),
                    tags=("on" if p["online"] else "off",))

        kinds = None
        raw = self.hist_kind.get()
        if raw != "all":
            kinds = raw.split(",")
        player = self.hist_player if self.hist_scope.get() == "one" else None

        try:
            rows = self.history.timeline(
                player=player, kinds=kinds,
                search=self.hist_search.get().strip(), limit=400)
        except Exception as exc:
            log_once(self, f"timeline failed: {exc}")
            return

        for item in self.ev_tree.get_children():
            self.ev_tree.delete(item)
        for e in rows:
            self.ev_tree.insert("", "end", tags=(e["kind"],), values=(
                short_time(e["ts"]), e["player"],
                KIND_LABEL.get(e["kind"], e["kind"]),
                (e["detail"] or "")[:200]))

    def refresh_queue(self) -> None:
        if not self.history:
            return
        self.q_list.delete(0, "end")
        name = self.hist_player
        if not name:
            self.q_title.configure(text="QUEUE  -  pick a player")
            return
        try:
            items = self.history.queue_for(name)
        except Exception:
            return
        self.q_title.configure(
            text=f"QUEUE FOR {name.upper()}  -  {len(items)} pending")
        self._queue_ids = []
        for q in items:
            self.q_list.insert("end", f"  {q['command']}")
            self._queue_ids.append(q["id"])

    def queue_add(self) -> None:
        if not self.history:
            return
        name = self.hist_player
        cmd = self.q_entry.get().strip()
        if not name:
            messagebox.showinfo(APP_NAME, "Pick a player on the left first.")
            return
        if not cmd:
            return
        if self.player_directory.enqueue(name, cmd):
            self.q_entry.delete(0, "end")
            self.refresh_queue()
            self.log_line(f"[launcher] queued for {name}: {cmd}", "ok")
        else:
            self.task_status.set(self.player_directory.feedback.get())

    def queue_remove(self) -> None:
        sel = self.q_list.curselection()
        if not sel or not getattr(self, "_queue_ids", None):
            return
        try:
            self.history.queue_delete(self._queue_ids[sel[0]])
        except Exception:
            return
        self.refresh_queue()

    def export_history(self) -> None:
        if not self.history:
            return
        dest = filedialog.asksaveasfilename(
            title="Export history", defaultextension=".csv",
            initialfile=f"player-history-{datetime.now():%Y%m%d}.csv",
            filetypes=[("CSV", "*.csv")])
        if not dest:
            return
        import csv
        try:
            rows = self.history.timeline(limit=100000)
            with open(dest, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["timestamp", "player", "event", "detail"])
                for e in rows:
                    w.writerow([e["ts"], e["player"], e["kind"], e["detail"]])
            self.log_line(f"[launcher] exported {len(rows)} events to {dest}", "ok")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Export failed:\n{exc}")

    # -- backups ----------------------------------------------------------

    @property
    def backup_dir(self) -> Path:
        return self.root_dir / "backups"

    def _build_backups_page(self) -> None:
        page = self._page("backups")
        bar = self._page_head(page, "Backups",
                              "Zipped copies of your world. Made with the server "
                              "stopped, so they are never half-written.")
        RoundButton(bar, "Refresh", self.refresh_backups, kind="quiet", width=82,
                    bg=PANEL).pack(side="right", padx=(6, 0))
        RoundButton(bar, "Open folder", lambda: self.open_folder("backups"),
                    kind="quiet", width=108, bg=PANEL).pack(side="right", padx=6)
        RoundButton(bar, "Back up now", self.backup_now, kind="primary",
                    width=120, bg=PANEL).pack(side="right", padx=6)

        body = tk.Frame(page, bg=PANEL)
        body.pack(fill="both", expand=True, padx=20, pady=(0, 16))

        opts = tk.Frame(body, bg=CARD)
        opts.pack(side="bottom", fill="x", pady=(12, 0))
        inner = tk.Frame(opts, bg=CARD)
        inner.pack(fill="x", padx=16, pady=12)
        try:
            mcfg = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            if not isinstance(mcfg, dict):
                mcfg = {}
            initial = bool(mcfg.get("backup_before_restart", False))
        except (OSError, json.JSONDecodeError):
            initial = bool(self.ui.get("backup_before_restart", False))
            mcfg = {}
        if not isinstance(mcfg, dict):
            mcfg = {}
        self.auto_backup = tk.BooleanVar(value=initial)
        cb = tk.Checkbutton(
            inner, variable=self.auto_backup, onvalue=True, offvalue=False,
            text="  Back up automatically before every scheduled restart",
            bg=CARD, fg=FG, font=F_UI, selectcolor=INPUT, activebackground=CARD,
            activeforeground=FG, highlightthickness=0, bd=0, anchor="w",
            command=self._toggle_auto_backup)
        cb.pack(fill="x")
        retention = tk.Frame(inner, bg=CARD)
        retention.pack(fill='x', pady=(8, 0))
        tk.Label(retention, text='Keep automatic backups:', bg=CARD, fg=FG_DIM,
                 font=F_SMALL).pack(side='left', padx=(5, 8))
        self.backup_keep = tk.StringVar(value=str(mcfg.get('backup_keep', 10)))
        tk.Spinbox(retention, from_=1, to=1000, textvariable=self.backup_keep,
                   width=5, bg=INPUT, fg=FG, buttonbackground=CARD, insertbackground=FG,
                   relief='flat', font=F_UI).pack(side='left')
        RoundButton(retention, 'Save', self._toggle_auto_backup, kind='quiet',
                    width=65, height=28, bg=CARD).pack(side='left', padx=8)
        tk.Label(inner, bg=CARD, fg=FG_FAINT, font=F_SMALL, anchor="w",
                 justify="left",
                 text="   Manual, update, and recovery backups are always kept. Retention applies after the next automatic backup.",
                 wraplength=680
                 ).pack(fill="x")

        arow = tk.Frame(body, bg=PANEL)
        arow.pack(side="bottom", fill="x", pady=(8, 0))
        RoundButton(arow, "Restore selected", self.backup_restore, kind="danger",
                    width=142, bg=PANEL).pack(side="left", padx=(0, 6))
        RoundButton(arow, "Delete selected", self.backup_delete, kind="quiet",
                    width=132, bg=PANEL).pack(side="left", padx=6)
        RoundButton(arow, 'Verify selected', self.backup_verify, kind='accent',
                    width=134, bg=PANEL).pack(side='left', padx=6)
        tk.Label(arow, bg=PANEL, fg=FG_FAINT, font=F_SMALL, anchor="w",
                 text="   Restore keeps a recovery backup.", wraplength=170
                 ).pack(side="left", fill="x", expand=True)

        border = tk.Frame(body, bg=LINE)
        border.pack(fill="both", expand=True)
        wrap = tk.Frame(border, bg=INPUT)
        wrap.pack(fill="both", expand=True, padx=1, pady=1)
        self.backup_tree = ttk.Treeview(wrap, columns=("when", "size"),
                                        show="tree headings", height=8,
                                        selectmode="browse")
        self.backup_tree.heading("#0", text="BACKUP")
        self.backup_tree.heading("when", text="WHEN")
        self.backup_tree.heading("size", text="SIZE")
        self.backup_tree.column("#0", width=420)
        self.backup_tree.column("when", width=190)
        self.backup_tree.column("size", width=110, anchor="e")
        sb = ttk.Scrollbar(wrap, command=self.backup_tree.yview)
        self.backup_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.backup_tree.pack(fill="both", expand=True, padx=(4, 0), pady=4)

        self.backup_note = ttk.Label(page, text="", style="Dim.TLabel")
        self.backup_note.pack(anchor="w", padx=22, pady=(0, 14))

    def _toggle_auto_backup(self) -> None:
        try:
            keep = int(self.backup_keep.get())
            if not 1 <= keep <= 1000:
                raise ValueError()
        except ValueError:
            self.backup_note.configure(text='Keep between 1 and 1000 automatic backups.')
            return
        want = bool(self.auto_backup.get())
        self.ui["backup_before_restart"] = want
        save_ui_config(self.root_dir, self.ui)
        # the manager performs the backup, so the setting lives in its config
        try:
            cfg = json.loads(self.config_path.read_text(encoding="utf-8-sig")) \
                if self.config_path.exists() else {}
            if not isinstance(cfg, dict):
                cfg = {}
            cfg["backup_before_restart"] = want
            cfg['backup_keep'] = keep
            atomic_json(self.config_path, cfg)
        except (OSError, json.JSONDecodeError) as exc:
            self.backup_note.configure(text=f"Could not save the setting: {exc}")
            return
        state = "on" if want else "off"
        self.backup_note.configure(
            text=f"Automatic pre-restart backup is {state}; keep {keep}. "
                 "Takes effect at the next restart.")

    def _refresh_backups_legacy(self) -> None:
        for item in self.backup_tree.get_children():
            self.backup_tree.delete(item)
        self._backups = {}
        if not self.backup_dir.is_dir():
            self.backup_note.configure(
                text="No backups yet. 'Back up now' creates the folder.")
            return
        files = sorted(self.backup_dir.glob("*.zip"),
                       key=lambda f: f.stat().st_mtime, reverse=True)
        total = 0
        for f in files:
            st = f.stat()
            total += st.st_size
            when = datetime.fromtimestamp(st.st_mtime)
            age = human_delta(datetime.now() - when)
            item = self.backup_tree.insert(
                "", "end", text=f" {f.name}",
                values=(f"{when:%Y-%m-%d %H:%M}  ({age} ago)",
                        f"{st.st_size/1048576:.0f} MB"))
            self._backups[item] = f
        self.backup_note.configure(
            text=f"{len(files)} backup(s), {total/1048576:.0f} MB total in "
                 f"{self.backup_dir.name}/")

    def _selected_backup(self) -> Path | None:
        sel = self.backup_tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "Pick a backup from the list first.")
            return None
        return self._backups.get(sel[0])

    def _maintenance_task(self, label, work, readonly=False) -> bool:
        if self._maintenance or getattr(self, '_update_busy', False) or self._install_stage:
            messagebox.showinfo(APP_NAME, 'Wait for the current operation to finish.')
            return False
        if not readonly and (self.manager.running() or self.stats.found):
            messagebox.showwarning(APP_NAME, 'Stop the server before changing or backing up world files.')
            return False
        self._maintenance = label
        if hasattr(self, '_task_cancel'):
            self._task_cancel.clear()
        self._task_phase = 'Preparing'
        self.task_status.set(label + '…')
        self.task_progress.pack(side='right', padx=8)
        self.task_progress.configure(mode='indeterminate', value=0)
        self.task_progress.start(12)
        self._set_state(self.status_text, BLUE)

        def worker():
            try:
                from contextlib import nullcontext
                with nullcontext() if readonly else operation_lock(self.root_dir):
                    # Recheck in the worker; a process can start since the last stats tick.
                    if not readonly and UPDATE_OK and bedrock_update.server_running():
                        raise RuntimeError('bedrock_server.exe is running. Stop it first.')
                    detail = work()
                self.q.put(('maintenance_done', (label, detail, None)))
            except Exception as exc:
                self.q.put(('maintenance_done', (label, '', str(exc))))
        threading.Thread(target=worker, daemon=True).start()
        return True

    def _backup_verify_legacy(self):
        path = self._selected_backup()
        if not path:
            return
        def check():
            result = verify_backup(path)
            return f"{path.name}: verified {result['files']} files, {result['bytes'] / 1048576:.1f} MB unpacked."
        self._maintenance_task('Verify backup', check)

    def backup_now(self, silent: bool = False) -> bool:
        def work():
            path = create_backup(self.root_dir, progress=self.report_stage)
            return f'Backup saved and verified: {path.name} ({path.stat().st_size/1048576:.1f} MB)'
        return self._maintenance_task('Back up world', work)

    def backup_delete(self) -> None:
        if self._maintenance:
            return
        f = self._selected_backup()
        if not f:
            return
        if not messagebox.askyesno(APP_NAME, f"Delete {f.name}?\n\n"
                                             "This cannot be undone."):
            return
        try:
            f.unlink()
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not delete it:\n{exc}")
            return
        self.refresh_backups()

    def _backup_restore_legacy(self) -> None:
        f = self._selected_backup()
        if not f:
            return
        if self.manager.running() or self.stats.found:
            messagebox.showwarning(APP_NAME,
                                   "Stop the server before restoring a world.")
            return
        if not messagebox.askyesno(
                APP_NAME,
                f"Restore {f.name}?\n\n"
                "Your current world is replaced by this backup. Everything built "
                "since it was made is lost.\n\n"
                "The world being replaced is backed up first, so this is "
                "reversible."):
            return

        self.log_line(f"[launcher] restoring {f.name}", "mgr")

        def work():
            safety = restore_backup(self.root_dir, f, self.report_stage)
            return 'World restored. ' + (f'Previous world saved as {safety.name}.' if safety else 'Ready to start.')
        self._maintenance_task('Restore world', work)

    # -- schedule ---------------------------------------------------------

    def _build_schedule_page(self) -> None:
        page = self._page("schedule")
        self._page_head(page, "Restart schedule",
                        "Players are warned, the world saves, and the server "
                        "comes back on its own.")

        box = tk.Frame(page, bg=PANEL)
        box.pack(fill="both", expand=True, padx=20, pady=(0, 18))

        card = tk.Frame(box, bg=CARD)
        card.pack(fill="x")
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=18, pady=16)
        tk.Label(inner, text="DAILY RESTART TIMES", bg=CARD, fg=FG_DIM,
                 font=F_LABEL).pack(anchor="w")
        tk.Label(inner, text="24-hour clock, comma separated.", bg=CARD,
                 fg=FG_FAINT, font=F_SMALL).pack(anchor="w", pady=(2, 10))

        row = tk.Frame(inner, bg=CARD)
        row.pack(fill="x")
        holder = tk.Frame(row, bg=INPUT)
        holder.pack(side="left")
        self.sched_entry = tk.Entry(holder, bg=INPUT, fg=FG, font=F_MONO,
                                    width=32, borderwidth=0,
                                    highlightthickness=0, insertbackground=FG)
        self.sched_entry.pack(padx=10, ipady=8)
        RoundButton(row, "Save schedule", self.save_schedule, kind="primary",
                    width=124, bg=CARD).pack(side="left", padx=8)
        RoundButton(row, "Reload", self.load_schedule, kind="quiet",
                    width=82, bg=CARD).pack(side="left")

        tk.Label(inner, text="PRESETS", bg=CARD, fg=FG_DIM,
                 font=F_LABEL).pack(anchor="w", pady=(18, 6))
        presets = tk.Frame(inner, bg=CARD)
        presets.pack(fill="x")
        for label, value in (
            ("3x daily", "06:00,14:00,22:00"),
            ("Twice daily", "05:00,17:00"),
            ("Nightly", "04:00"),
            ("Every 6h", "00:00,06:00,12:00,18:00"),
        ):
            RoundButton(presets, label, lambda v=value: self._set_sched(v),
                        kind="quiet", width=100, height=28, bg=CARD,
                        font=F_SMALL).pack(side="left", padx=(0, 6))

        tk.Label(inner, text="WARNING TIMES  (minutes before restart)",
                 bg=CARD, fg=FG_DIM, font=F_LABEL).pack(anchor="w",
                                                        pady=(20, 6))
        wh = tk.Frame(inner, bg=INPUT)
        wh.pack(anchor="w")
        self.warn_entry = tk.Entry(wh, bg=INPUT, fg=FG, font=F_MONO, width=32,
                                   borderwidth=0, highlightthickness=0,
                                   insertbackground=FG)
        self.warn_entry.pack(padx=10, ipady=8)

        actions = tk.Frame(box, bg=PANEL)
        actions.pack(fill="x", pady=(16, 0))
        RoundButton(actions, "Restart now", self.do_restart, kind="ghost",
                    width=118, bg=PANEL).pack(side="left", padx=(0, 6))
        RoundButton(actions, "Skip next", lambda: self.send_manager("!skip"),
                    kind="quiet", width=104, bg=PANEL).pack(side="left", padx=6)
        RoundButton(actions, "Push to in-game menu",
                    lambda: self.send_manager("!sync"), kind="quiet",
                    width=168, bg=PANEL).pack(side="left", padx=6)

        self.sched_note = ttk.Label(box, text="", style="Dim.TLabel")
        self.sched_note.pack(anchor="w", pady=(14, 0))

    def _set_sched(self, value: str) -> None:
        self.sched_entry.delete(0, "end")
        self.sched_entry.insert(0, value)

    @property
    def config_path(self) -> Path:
        return self.root_dir / "manager_config.json"

    def load_schedule(self) -> None:
        times, warns = ["06:00", "14:00", "22:00"], [15, 10, 5, 2, 1]
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
                times = [str(t) for t in data.get("restart_times", times)]
                warns = [int(w) for w in data.get("warn_minutes", warns)]
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass
        self._set_sched(",".join(times))
        self.warn_entry.delete(0, "end")
        self.warn_entry.insert(0, ",".join(str(w) for w in warns))
        self.sched_note.configure(text=f"Loaded from {self.config_path.name}")

    def save_schedule(self) -> None:
        times = parse_times(self.sched_entry.get())
        if not times:
            messagebox.showerror(APP_NAME,
                                 "Times must look like 06:00,14:00,22:00 (24-hour).")
            return
        try:
            warns = sorted({int(w) for w in re.split(r"[,\s]+", self.warn_entry.get().strip())
                            if w}, reverse=True)
        except ValueError:
            messagebox.showerror(APP_NAME, "Warning times must be whole numbers of minutes.")
            return
        if not warns:
            warns = [15, 10, 5, 2, 1]

        if any(w <= 0 or w > 1440 for w in warns):
            messagebox.showerror(APP_NAME, 'Warning minutes must be between 1 and 1440.')
            return
        try:
            payload = json.loads(self.config_path.read_text(encoding='utf-8-sig')) if self.config_path.exists() else {}
            if not isinstance(payload, dict):
                raise ValueError('manager_config.json must contain an object.')
            payload.update(restart_times=times, warn_minutes=warns)
            atomic_json(self.config_path, payload)
        except (OSError, ValueError) as exc:
            messagebox.showerror(APP_NAME, f"Could not write the config file:\n{exc}")
            return

        if self.manager.running():
            self.send_manager("!schedule " + ",".join(times))
            note = "Saved and applied to the running server."
        else:
            note = "Saved. It takes effect the next time you start the server."
        self.sched_note.configure(text=note)
        self.log_line(f"[launcher] schedule set to {', '.join(times)}", "ok")

    # -- mods ------------------------------------------------------------

    def _build_mods_page(self) -> None:
        page = self._page("mods")
        bar = self._page_head(page, "Mods",
                              "Green = active in the world. Disabling is instant "
                              "and reversible; uninstalling deletes the files.")
        RoundButton(bar, "Refresh", self.refresh_mods, kind="quiet", width=82,
                    bg=PANEL).pack(side="right", padx=(6, 0))
        RoundButton(bar, "Rebuild menu", self.run_menu_builder,
                    kind="quiet", width=116, bg=PANEL).pack(side="right", padx=6)
        RoundButton(bar, "Open folder", lambda: self.open_folder("mods"),
                    kind="quiet", width=108, bg=PANEL).pack(side="right", padx=6)

        self.mod_note = ttk.Label(page, text="", style="Dim.TLabel")
        self.mod_note.pack(side="bottom", anchor="w", padx=22, pady=(6, 12))

        tools_card = tk.Frame(page, bg=CARD, padx=12, pady=8)
        tools_card.pack(fill='x', padx=20, pady=(0, 10))
        actions = tk.Frame(tools_card, bg=CARD)
        actions.pack(side='right')
        ttk.Button(actions, text='Install / update in-game tools', command=self.install_ingame_tools).pack(side='left', padx=6)
        ttk.Button(actions, text='Command help', command=self.command_help_dialog).pack(side='left')
        self.admin_tools_status = tk.StringVar(value='In-game tools · /admin:help')
        status = tk.Label(tools_card, textvariable=self.admin_tools_status, bg=CARD, fg=FG_DIM,
                          justify='left', anchor='w', wraplength=390, font=F_SMALL)
        status.pack(side='left', fill='x', expand=True)
        status.bind('<Configure>', lambda event: status.configure(wraplength=max(160, event.width - 8)))

        body = tk.Frame(page, bg=PANEL)
        body.pack(fill="both", expand=True, padx=20, pady=(0, 4))

        # Two explicit sections. The lower one is packed first so it reserves
        # its height; the upper one then expands into whatever is left. Mixing
        # side="top" and side="bottom" in one frame starves the last widget.
        lower = tk.Frame(body, bg=PANEL)
        lower.pack(side="bottom", fill="x", pady=(14, 0))
        upper = tk.Frame(body, bg=PANEL)
        upper.pack(side="top", fill="both", expand=True)

        # ---------- upper: packs installed on the server
        tk.Label(upper, text="INSTALLED PACKS", bg=PANEL, fg=FG_DIM,
                 font=F_LABEL, anchor="w").pack(fill="x", pady=(0, 4))

        pb = tk.Frame(upper, bg=LINE)
        pb.pack(fill="both", expand=True)
        pw = tk.Frame(pb, bg=INPUT)
        pw.pack(fill="both", expand=True, padx=1, pady=1)
        cols = ("kind", "version", "state", "folder")
        self.mod_tree = ttk.Treeview(pw, columns=cols, show="tree headings",
                                     height=6, selectmode="browse")
        self.mod_tree.heading("#0", text="PACK")
        self.mod_tree.heading("kind", text="TYPE")
        self.mod_tree.heading("version", text="VERSION")
        self.mod_tree.heading("state", text="IN WORLD")
        self.mod_tree.heading("folder", text="FOLDER")
        self.mod_tree.column("#0", width=250)
        self.mod_tree.column("kind", width=80, anchor="center")
        self.mod_tree.column("version", width=80, anchor="center")
        self.mod_tree.column("state", width=90, anchor="center")
        self.mod_tree.column("folder", width=240)
        sb = ttk.Scrollbar(pw, command=self.mod_tree.yview)
        self.mod_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.mod_tree.pack(fill="both", expand=True, padx=(4, 0), pady=4)
        self.mod_tree.tag_configure("on", foreground=GREEN)
        self.mod_tree.tag_configure("off", foreground=FG_FAINT)

        prow = tk.Frame(upper, bg=PANEL)
        prow.pack(fill="x", pady=(8, 0))
        RoundButton(prow, "Enable in world", lambda: self.pack_world_set(True),
                    kind="primary", width=132, bg=PANEL).pack(side="left",
                                                              padx=(0, 6))
        RoundButton(prow, "Disable in world", lambda: self.pack_world_set(False),
                    kind="ghost", width=134, bg=PANEL).pack(side="left", padx=6)
        RoundButton(prow, "Uninstall", self.pack_uninstall, kind="danger",
                    width=100, bg=PANEL).pack(side="left", padx=6)
        tk.Label(prow, bg=PANEL, fg=FG_FAINT, font=F_SMALL, anchor="w",
                 text="   Disabling keeps the files - it only stops the world "
                      "loading them.").pack(side="left", fill="x", expand=True)

        # ---------- lower: archives sitting in mods/
        tk.Label(lower, text="ARCHIVES IN mods/", bg=PANEL, fg=FG_DIM,
                 font=F_LABEL, anchor="w").pack(fill="x", pady=(0, 4))

        ab = tk.Frame(lower, bg=LINE)
        ab.pack(fill="x")
        aw = tk.Frame(ab, bg=INPUT)
        aw.pack(fill="x", padx=1, pady=1)
        self.arch_tree = ttk.Treeview(aw, columns=("size", "status"),
                                      show="tree headings", height=3,
                                      selectmode="browse")
        self.arch_tree.heading("#0", text="FILE")
        self.arch_tree.heading("size", text="SIZE")
        self.arch_tree.heading("status", text="STATUS")
        self.arch_tree.column("#0", width=360)
        self.arch_tree.column("size", width=90, anchor="e")
        self.arch_tree.column("status", width=150)
        self.arch_tree.pack(fill="x", padx=4, pady=4)
        self.arch_tree.tag_configure("installed", foreground=GREEN)
        self.arch_tree.tag_configure("pending", foreground=AMBER)
        self.arch_tree.tag_configure("disabled", foreground=FG_FAINT)

        arow = tk.Frame(lower, bg=PANEL)
        arow.pack(fill="x", pady=(8, 0))
        RoundButton(arow, "Install new", self.run_installer, kind="accent",
                    width=110, bg=PANEL).pack(side="left", padx=(0, 6))
        RoundButton(arow, "Restore", self.archive_restore, kind="quiet",
                    width=92, bg=PANEL).pack(side="left", padx=6)
        RoundButton(arow, "Delete archive", self.archive_delete, kind="danger",
                    width=122, bg=PANEL).pack(side="left", padx=6)
        tk.Label(arow, bg=PANEL, fg=FG_FAINT, font=F_SMALL, anchor="w",
                 text="   Restore un-does an uninstall so 'Install new' picks it "
                      "up again.").pack(side="left", fill="x", expand=True)

        self._packs: dict[str, dict] = {}
        self._archives: dict[str, dict] = {}

    # -- pack actions ------------------------------------------------------

    def _selected_pack(self) -> dict | None:
        sel = self.mod_tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "Pick a pack from the list first.")
            return None
        return self._packs.get(sel[0])

    def _world_file(self, kind: str) -> Path:
        name = ("world_behavior_packs.json" if kind == "behavior"
                else "world_resource_packs.json")
        return self.root_dir / "worlds" / self.level_name() / name

    def pack_world_set(self, enable: bool) -> None:
        if self._maintenance or self._update_busy:
            return
        """Add or remove a pack from the world's pack list, keeping the files."""
        pack = self._selected_pack()
        if not pack:
            return
        path = self._world_file(pack["kind"])
        try:
            entries = json.loads(path.read_text(encoding="utf-8-sig")) \
                if path.exists() else []
        except (OSError, json.JSONDecodeError) as exc:
            messagebox.showerror(APP_NAME, f"Could not read {path.name}:\n{exc}")
            return
        if not isinstance(entries, list):
            entries = []

        present = any(isinstance(e, dict) and e.get("pack_id") == pack["uuid"]
                      for e in entries)
        if enable and present:
            self.mod_note.configure(text=f"{pack['name']} is already enabled.")
            return
        if not enable and not present:
            self.mod_note.configure(text=f"{pack['name']} is already disabled.")
            return

        if enable:
            entries.append({"pack_id": pack["uuid"], "version": pack["version"]})
        else:
            entries = [e for e in entries
                       if not (isinstance(e, dict) and e.get("pack_id") == pack["uuid"])]

        try:
            backup = path.with_name(f"{path.name}.{datetime.now():%Y%m%d-%H%M%S}.bak")
            if path.exists():
                backup.write_text(path.read_text(encoding="utf-8-sig"),
                                  encoding="utf-8")
            with operation_lock(self.root_dir):
                if UPDATE_OK and bedrock_update.server_running():
                    raise OSError('Stop the server before changing packs.')
                atomic_json(path, entries)
        except (OSError, RuntimeError) as exc:
            messagebox.showerror(APP_NAME, f"Could not write {path.name}:\n{exc}")
            return

        self._remember_world_choice(pack["uuid"], enable)
        word = "enabled" if enable else "disabled"
        self.log_line(f"[launcher] {pack['name']} {word} in the world", "ok")
        self.mod_note.configure(
            text=f"{pack['name']} {word}. Restart the server for it to take effect.")
        self.refresh_mods()
        self.run_menu_builder()

    def _remember_world_choice(self, uuid: str, enabled: bool) -> None:
        """Record deliberate disables so 'verify' does not flag them."""
        path = self.root_dir / "mods" / "world_disabled.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig")) \
                if path.exists() else []
            if not isinstance(data, list):
                data = []
        except (OSError, json.JSONDecodeError):
            data = []
        if enabled:
            data = [u for u in data if u != uuid]
        elif uuid not in data:
            data.append(uuid)
        try:
            path.parent.mkdir(exist_ok=True)
            atomic_json(path, data)
        except OSError:
            pass

    def pack_uninstall(self) -> None:
        pack = self._selected_pack()
        if not pack:
            return
        if self.manager.running() or self.stats.found:
            messagebox.showwarning(
                APP_NAME, "Stop the server before uninstalling packs.")
            return
        if not pack.get("archive"):
            messagebox.showinfo(
                APP_NAME,
                f"{pack['name']} was not installed from mods/, so the installer "
                "cannot remove it.\n\nUse 'Disable in world' instead, which "
                "stops the world loading it without touching the files.")
            return
        if not messagebox.askyesno(
                APP_NAME,
                f"Uninstall {pack['name']}?\n\n"
                "This removes it from the world AND deletes its pack folder.\n"
                f"The archive stays in mods/ ({pack['archive']}) marked disabled, "
                "so you can Restore it later.\n\n"
                "Anything built with this pack's blocks may break."):
            return
        self._run_helper("bedrock_addons.py",
                         ["uninstall", Path(pack["archive"]).stem, "--yes"],
                         "Uninstaller")

    # -- archive actions ---------------------------------------------------

    def _selected_archive(self) -> dict | None:
        sel = self.arch_tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "Pick an archive from the lower list.")
            return None
        return self._archives.get(sel[0])

    def archive_restore(self) -> None:
        arc = self._selected_archive()
        if not arc:
            return
        if arc["status"] != "uninstalled":
            self.mod_note.configure(
                text=f"{arc['name']} is not uninstalled, so there is nothing to restore.")
            return
        self._run_helper("bedrock_addons.py", ["enable", Path(arc["name"]).stem],
                         "Restore")

    def archive_delete(self) -> None:
        if self._maintenance or self._update_busy:
            return
        arc = self._selected_archive()
        if not arc:
            return
        if not messagebox.askyesno(
                APP_NAME,
                f"Delete {arc['name']} from mods/?\n\n"
                "This only removes the archive file. Packs already installed "
                "from it stay on the server.\n\nThis cannot be undone."):
            return
        try:
            (self.root_dir / "mods" / arc["name"]).unlink()
            self.log_line(f"[launcher] deleted mods/{arc['name']}", "ok")
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not delete it:\n{exc}")
            return
        self.refresh_mods()

    def _active_pack_ids(self, kind: str) -> set[str]:
        world = self.level_name()
        name = "world_behavior_packs.json" if kind == "behavior" else "world_resource_packs.json"
        path = self.root_dir / "worlds" / world / name
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return set()
        if not isinstance(data, list):
            return set()
        return {e.get("pack_id") for e in data if isinstance(e, dict)}

    def _refresh_mods_legacy(self) -> None:
        for item in self.mod_tree.get_children():
            self.mod_tree.delete(item)
        for item in self.arch_tree.get_children():
            self.arch_tree.delete(item)
        self._packs, self._archives = {}, {}

        # which archive did each installed pack come from?
        origin: dict[str, str] = {}
        state: dict = {}
        try:
            state = json.loads(
                (self.root_dir / "mods" / "_addon_state.json")
                .read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            state = {}
        for archive, rec in (state.items() if isinstance(state, dict) else []):
            for pk in rec.get("packs", []):
                origin[pk.get("uuid", "")] = archive

        total = on = 0
        for kind, folder in (("behavior", "behavior_packs"),
                             ("resource", "resource_packs")):
            active = self._active_pack_ids(kind)
            base = self.root_dir / folder
            if not base.is_dir():
                continue
            for d in sorted(base.iterdir()):
                if not d.is_dir():
                    continue
                try:
                    m = json.loads((d / "manifest.json")
                                   .read_text(encoding="utf-8-sig"))
                    header = m.get("header", {})
                except (OSError, json.JSONDecodeError, AttributeError):
                    continue
                uuid = header.get("uuid", "")
                if not uuid:
                    continue
                version = header.get("version")
                vtext = ".".join(str(v) for v in version) \
                    if isinstance(version, list) else str(version or "?")
                # vanilla packs ship with the server - hide them unless enabled
                stock = d.name.startswith(("vanilla", "chemistry", "editor",
                                           "experimental", "server_"))
                live = uuid in active
                if stock and not live:
                    continue
                name = str(header.get("name", d.name))
                if name.startswith("pack."):
                    name = d.name
                item = self.mod_tree.insert(
                    "", "end", text=f" {name}",
                    values=(kind, vtext, "yes" if live else "no", d.name),
                    tags=("on" if live else "off",))
                self._packs[item] = {
                    "name": name, "uuid": uuid, "kind": kind, "folder": d.name,
                    "version": version if isinstance(version, list) else [1, 0, 0],
                    "archive": origin.get(uuid, ""),
                }
                total += 1
                on += 1 if live else 0

        # archives sitting in mods/
        mods = self.root_dir / "mods"
        if mods.is_dir():
            for f in sorted(mods.iterdir()):
                if not f.is_file() or f.suffix.lower() not in \
                        (".mcaddon", ".mcpack", ".zip"):
                    continue
                rec = state.get(f.name, {}) if isinstance(state, dict) else {}
                if rec.get("disabled"):
                    status, tag = "uninstalled", "disabled"
                elif rec.get("packs"):
                    status, tag = "installed", "installed"
                else:
                    status, tag = "not installed yet", "pending"
                size = f.stat().st_size
                stext = (f"{size/1048576:.0f} MB" if size >= 1048576
                         else f"{size/1024:.0f} KB")
                item = self.arch_tree.insert("", "end", text=f" {f.name}",
                                             values=(stext, status), tags=(tag,))
                self._archives[item] = {"name": f.name, "status": status}

        self.mod_note.configure(
            text=f"{total} pack(s) on disk, {on} active in the world. "
                 "Changes take effect when the server next starts.")

    def run_installer(self) -> None:
        self._run_helper("bedrock_addons.py", ["install", "--yes"], "Addon installer")

    def run_menu_builder(self) -> None:
        from build_admin_addon import refresh_generated
        self._maintenance_task('Rebuild in-game help/menu',
            lambda: refresh_generated(self.root_dir, lock_held=True, progress=self.report_stage))

    def _run_helper(self, script: str, args: list[str], label: str) -> None:
        if self._maintenance or self._update_busy or self._install_stage:
            messagebox.showinfo(APP_NAME, 'Wait for the current operation to finish.')
            return
        if script == "bedrock_addons.py":
            # self.manager.running() only knows about a server WE started.
            # stats.found is the real test: is bedrock_server.exe alive at all,
            # including from another window or a previous session?
            if self.manager.running() or self.stats.found:
                messagebox.showwarning(
                    APP_NAME,
                    "bedrock_server is still running.\n\n"
                    "Stop it before installing or removing packs - editing them "
                    "under a live server can corrupt the world.")
                return
        self.log_line(f"[launcher] running {script} {' '.join(args)}", "mgr")
        self._maintenance = label
        self.task_status.set(label + '…')
        self._set_state(self.status_text, BLUE)

        def work():
            try:
                res = subprocess.run(
                    worker_command(script, *args, '--server', self.root_dir),
                    cwd=str(self.root_dir), capture_output=True, text=True, timeout=300,
                    encoding='utf-8', errors='replace', env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0)
                for line in (res.stdout or "").splitlines():
                    self.q.put(("line", f"  {line}"))
                for line in (res.stderr or "").splitlines():
                    self.q.put(("line", f"  {line}"))
                if res.returncode:
                    raise RuntimeError(f'{label} exited with code {res.returncode}. See Console for details.')
                if script == "bedrock_addons.py" and res.returncode == 0:
                    from build_admin_addon import refresh_generated
                    try:
                        self.q.put(('line', '[launcher] ' + refresh_generated(self.root_dir, progress=self.report_stage)))
                    except Exception as exc:
                        raise RuntimeError(f'Pack operation completed, but help/menu refresh failed: {exc}. Use Rebuild menu after resolving this.') from exc
            except Exception as exc:
                self.q.put(("error", f"{label} failed: {exc}"))
            self.q.put(("mods_changed", ""))
            self.q.put(('helper_done', label))

        threading.Thread(target=work, daemon=True).start()

    # -- server.properties ------------------------------------------------

    COMMON_PROPS = [
        ("server-name", "Server name shown in the list"),
        ("gamemode", "survival / creative / adventure"),
        ("difficulty", "peaceful / easy / normal / hard"),
        ("max-players", "Maximum players"),
        ("allow-list", "true = only allowlisted players may join"),
        ("view-distance", "Chunks streamed to players (RAM + bandwidth)"),
        ("tick-distance", "Chunks simulated around players (CPU)"),
        ("player-idle-timeout", "Minutes before AFK kick, 0 = never"),
        ("default-player-permission-level", "visitor / member / operator"),
        ("allow-cheats", "true enables commands"),
        ("content-log-console-output-enabled", "Required for the in-game menu"),
    ]

    def _build_settings_page(self) -> None:
        page = self._page("settings")
        bar = self._page_head(page, "Settings",
                              "Every setting in server.properties, with the "
                              "file's own notes. Changes apply on next start.")
        RoundButton(bar, "Save", self.save_props, kind="primary", width=86,
                    bg=PANEL).pack(side="right", padx=(6, 0))
        RoundButton(bar, "Reload", self.load_props, kind="quiet", width=86,
                    bg=PANEL).pack(side="right", padx=6)

        tools = tk.Frame(page, bg=PANEL)
        tools.pack(fill="x", padx=20, pady=(0, 8))
        holder = tk.Frame(tools, bg=INPUT)
        holder.pack(side="left", fill="x", expand=True)
        tk.Label(holder, text="  search", bg=INPUT, fg=FG_FAINT,
                 font=F_SMALL).pack(side="left")
        self.prop_search = tk.Entry(holder, bg=INPUT, fg=FG, font=F_UI,
                                    borderwidth=0, highlightthickness=0,
                                    insertbackground=FG)
        self.prop_search.pack(side="left", fill="x", expand=True, padx=6, ipady=6)
        self.prop_search.bind("<KeyRelease>", lambda e: self._filter_props())

        self.only_common = tk.BooleanVar(value=True)
        tk.Checkbutton(tools, variable=self.only_common, text="  common only",
                       bg=PANEL, fg=FG_DIM, font=F_SMALL, selectcolor=INPUT,
                       activebackground=PANEL, activeforeground=FG,
                       highlightthickness=0, bd=0,
                       command=self._filter_props).pack(side="left", padx=(10, 0))

        tk.Label(tools, text="text size", bg=PANEL, fg=FG_FAINT,
                 font=F_SMALL).pack(side="left", padx=(20, 4))
        RoundButton(tools, "-", lambda: self.bump_scale(-0.1), kind="quiet",
                    width=30, height=28, bg=PANEL).pack(side="left", padx=2)
        self.scale_label = tk.Label(
            tools, text=f"{int(float(self.ui.get('scale',1.0))*100)}%",
            bg=PANEL, fg=FG_DIM, font=F_SMALL, width=5)
        self.scale_label.pack(side="left")
        RoundButton(tools, "+", lambda: self.bump_scale(+0.1), kind="quiet",
                    width=30, height=28, bg=PANEL).pack(side="left", padx=2)

        self.props_note = ttk.Label(page, text="", style="Dim.TLabel")
        self.props_note.pack(side="bottom", anchor="w", padx=22, pady=(6, 12))

        border = tk.Frame(page, bg=LINE)
        border.pack(fill="both", expand=True, padx=20)
        holder2 = tk.Frame(border, bg=INPUT)
        holder2.pack(fill="both", expand=True, padx=1, pady=1)
        canvas = tk.Canvas(holder2, bg=INPUT, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(holder2, command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.prop_host = tk.Frame(canvas, bg=INPUT)
        self._prop_window = canvas.create_window((0, 0), window=self.prop_host,
                                                 anchor="nw")
        self.prop_canvas = canvas
        self.prop_host.bind("<Configure>",
                            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(self._prop_window, width=e.width))
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            canvas.bind_all(seq, self._scroll_props, add="+")

        self.prop_vars: dict[str, tk.StringVar] = {}
        self.prop_rows: dict[str, tk.Frame] = {}
        self.prop_help: dict[str, str] = {}
        self.load_props()

    def _scroll_props(self, event):
        if getattr(self, "current_page", "") != "settings":
            return
        delta = 0
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        elif getattr(event, "delta", 0):
            delta = -1 if event.delta > 0 else 1
        if delta:
            self.prop_canvas.yview_scroll(delta, "units")

    def _parse_props(self) -> tuple[dict, dict, list]:
        """Returns values, help text per key, and the key order in the file."""
        values, helps, order = {}, {}, []
        try:
            lines = self.props_path.read_text(encoding="utf-8-sig",
                                              errors="replace").splitlines()
        except OSError:
            return values, helps, order
        # Bedrock writes the explanation AFTER the setting it describes, so
        # comments attach to the most recent key, not the next one.
        current: str | None = None
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                text = line.lstrip("#").strip()
                # a commented-out property is an example, not prose
                if not text or re.match(r"^[a-z0-9-]+=", text):
                    continue
                if current:
                    prior = helps.get(current, "")
                    if len(prior) < 240:
                        helps[current] = (prior + " " + text).strip()
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            values[key] = val.strip()
            order.append(key)
            current = key
        for k in helps:
            helps[k] = helps[k][:240]
        return values, helps, order

    def load_props(self) -> None:
        self._props_baseline = self.props_path.read_text(encoding='utf-8-sig')
        values, helps, order = self._parse_props()
        if not values:
            self.props_note.configure(text="Could not read server.properties.")
            return
        self.prop_help = helps
        for child in self.prop_host.winfo_children():
            child.destroy()
        self.prop_vars, self.prop_rows = {}, {}

        common = {k for k, _ in self.COMMON_PROPS}
        for key in order:
            row = tk.Frame(self.prop_host, bg=INPUT)
            var = tk.StringVar(value=values[key])
            self.prop_vars[key] = var
            self.prop_rows[key] = row

            top = tk.Frame(row, bg=INPUT)
            top.pack(fill="x", padx=14, pady=(9, 0))
            tk.Label(top, text=key, bg=INPUT,
                     fg=FG if key in common else FG_DIM,
                     font=F_UI, anchor="w", width=38).pack(side="left")
            eh = tk.Frame(top, bg=BG)
            eh.pack(side="left")
            tk.Entry(eh, textvariable=var, width=26, bg=BG, fg=FG, font=F_MONO,
                     borderwidth=0, highlightthickness=0,
                     insertbackground=FG).pack(padx=8, ipady=5)
            hint = helps.get(key, "")
            if hint:
                tk.Label(row, text=hint, bg=INPUT, fg=FG_FAINT, font=F_SMALL,
                         anchor="w", justify="left", wraplength=560).pack(
                             fill="x", padx=(14, 14), pady=(2, 0))
            tk.Frame(row, bg="#1a1e27", height=1).pack(fill="x", pady=(8, 0))

        self._filter_props()
        self.props_note.configure(
            text=f"{len(values)} settings loaded from server.properties.")

    def _filter_props(self) -> None:
        term = self.prop_search.get().strip().lower()
        common = {k for k, _ in self.COMMON_PROPS}
        shown = 0
        for key, row in self.prop_rows.items():
            match = (not term) or term in key.lower() \
                or term in self.prop_help.get(key, "").lower()
            if self.only_common.get() and not term and key not in common:
                match = False
            row.pack_forget()
            if match:
                row.pack(fill="x")
                shown += 1
        self.props_note.configure(
            text=f"showing {shown} of {len(self.prop_rows)} settings")

    def _save_props_legacy(self) -> None:
        proposed = {key: var.get().strip() for key, var in self.prop_vars.items()}
        errors = health.validate_properties(proposed) if HEALTH_OK else []
        try:
            world_path(self.root_dir, proposed.get('level-name', self.level_name()))
        except ValueError as exc:
            errors.append(str(exc))
        if errors:
            messagebox.showerror(APP_NAME, '\n'.join(errors))
            return
        try:
            text = self.props_path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not read server.properties:\n{exc}")
            return
        backup = self.props_path.with_name(
            f"server.properties.{datetime.now():%Y%m%d-%H%M%S}.bak")
        try:
            backup.write_text(text, encoding="utf-8")
        except OSError:
            pass

        lines = text.splitlines()
        changed = []
        for i, line in enumerate(lines):
            st = line.strip()
            if st.startswith("#") or "=" not in st:
                continue
            key = st.partition("=")[0].strip()
            var = self.prop_vars.get(key)
            if var is None:
                continue
            new = var.get().strip()
            old = st.partition("=")[2].strip()
            if new != old:
                lines[i] = f"{key}={new}"
                changed.append(f"{key}: {old or '(empty)'} -> {new or '(empty)'}")
        if not changed:
            self.props_note.configure(text="Nothing changed.")
            return
        try:
            atomic_text(self.props_path, "\n".join(lines) + "\n")
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not write server.properties:\n{exc}")
            return
        for c in changed:
            self.log_line(f"[launcher] {c}", "ok")
        self.props_note.configure(
            text=f"Saved {len(changed)} change(s). Backup: {backup.name}. "
                 "Restart to apply.")
        self.max_players = self._read_max_players()

    @property
    def props_path(self) -> Path:
        return self.root_dir / "server.properties"

    # -- actions ---------------------------------------------------------

    def level_name(self) -> str:
        try:
            for line in self.props_path.read_text(encoding="utf-8-sig",
                                                  errors="replace").splitlines():
                s = line.strip()
                if s.startswith("#") or "=" not in s:
                    continue
                k, _, v = s.partition("=")
                if k.strip() == "level-name":
                    return v.strip()
        except OSError:
            pass
        return "Bedrock level"

    def do_start(self) -> None:
        if self.manager.running() or self._maintenance or getattr(self, '_update_busy', False) or self._install_stage:
            return
        try:
            if UPDATE_OK and bedrock_update.server_running():
                messagebox.showinfo(APP_NAME, 'A Bedrock server is already running outside this launcher.')
                return
        except Exception as exc:
            messagebox.showerror(APP_NAME, f'Could not check server state: {exc}')
            return
        self._stopping_on_purpose = False
        self.log_line("[launcher] starting the server manager", "mgr")
        self.players.clear()
        self.packs.clear()
        try:
            self.manager.start()
        except (OSError, RuntimeError) as exc:
            self.log_line(f'[launcher] start failed: {exc}', 'err')
            return
        self._set_state("Starting", WARN)

    def do_stop(self) -> None:
        if not self.manager.running():
            return
        if self.players and not messagebox.askyesno(
                APP_NAME,
                f"{len(self.players)} player(s) are online.\n\nStop the server anyway?"):
            return
        self.log_line("[launcher] stopping - the world will save first", "mgr")
        self._stopping_on_purpose = True
        self._set_state("Stopping", WARN)
        threading.Thread(target=self.manager.shutdown, daemon=True).start()

    def do_restart(self) -> None:
        if not self.manager.running():
            messagebox.showinfo(APP_NAME, "The server isn't running.")
            return
        if messagebox.askyesno(APP_NAME,
                               "Restart the server?\n\nPlayers get a one minute warning."):
            self.send_manager("!restart")

    def send_manager(self, text: str) -> None:
        if not self.manager.send(text):
            self.log_line("[launcher] the server manager isn't running", "err")

    def send_command(self, cmd: str) -> None:
        if not self.manager.running():
            self.log_line("[launcher] start the server first", "err")
            return
        self.log_line(f"> {cmd}", "dim")
        self.manager.send(cmd)

    def send_entry(self) -> None:
        text = self.entry.get().strip()
        if not text:
            return
        if not self.cmd_history or self.cmd_history[-1] != text:
            self.cmd_history.append(text)
        del self.cmd_history[:-60]
        self.history_at = len(self.cmd_history)
        # keep the history across launches - retyping a long teleport is grim
        self.ui["console_history"] = list(self.cmd_history)
        save_ui_config(self.root_dir, self.ui)
        self.entry.delete(0, "end")
        self._update_hint()
        self.send_command(text)

    def _history_prev(self, _evt):
        if self.cmd_history and self.history_at > 0:
            self.history_at -= 1
            self.entry.delete(0, "end")
            self.entry.insert(0, self.cmd_history[self.history_at])
        return "break"

    def _history_next(self, _evt):
        if self.history_at < len(self.cmd_history) - 1:
            self.history_at += 1
            self.entry.delete(0, "end")
            self.entry.insert(0, self.cmd_history[self.history_at])
        else:
            self.history_at = len(self.cmd_history)
            self.entry.delete(0, "end")
        return "break"

    def _selected_player(self) -> str | None:
        name = self.player_directory.selected()
        if not name:
            messagebox.showinfo(APP_NAME, "Pick a player from the list first.")
            return None
        return name

    def _player_cmd(self, template: str) -> None:
        name = self._selected_player()
        if not name:
            return
        # quote only when needed; a bare gamertag is the safest form
        target = f'"{name}"' if any(c.isspace() for c in name) else name
        self.send_command(template.format(target))

    def do_say(self) -> None:
        text = self.say_entry.get().strip()
        if not text:
            return
        self.send_command(f"say {text}")
        self.say_entry.delete(0, "end")

    def open_folder(self, sub: str = "") -> None:
        target = self.root_dir / sub if sub else self.root_dir
        target.mkdir(parents=True, exist_ok=True)
        try:
            if IS_WIN:
                os.startfile(str(target))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                webbrowser.open(target.as_uri())
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open the folder:\n{exc}")

    def backup_world(self) -> None:
        world = self.root_dir / "worlds" / self.level_name()
        if not world.is_dir():
            messagebox.showerror(APP_NAME, f"World folder not found:\n{world}")
            return
        if self.manager.running() or self.stats.found or self._maintenance:
            messagebox.showwarning(
                APP_NAME,
                "Stop the server before backing up.\n\nCopying a world while it's "
                "open can produce a corrupt backup.")
            return
        dest = filedialog.asksaveasfilename(
            title="Save world backup",
            defaultextension=".zip",
            initialfile=f"{self.level_name()}-{datetime.now():%Y%m%d-%H%M}.zip",
            filetypes=[("Zip archive", "*.zip")])
        if not dest:
            return
        self.log_line("[launcher] backing up the world...", "mgr")

        def work():
            path = create_backup(self.root_dir, dest=Path(dest))
            return f'Backup saved and verified: {path}'
        self._maintenance_task('Export world backup', work)

    # -- console output ---------------------------------------------------

    def log_line(self, text: str, tag: str = "") -> None:
        """Parse one line once, buffer the record, draw it if it passes the
        filters. Everything downstream reads the record, never the text."""
        rec = parse_line(text, tag)

        # A run of identical lines becomes one line with a counter. Bedrock
        # emits the same structure-registration error dozens of times per load;
        # collapsing keeps it honest (it really is an error) without letting it
        # bury everything else.
        #
        # The content log puts two blank lines between consecutive repeats, so
        # the run has to be traced past them or it never collapses at all.
        last = None
        if rec["text"]:
            for back in range(1, min(5, len(self.console_buffer) + 1)):
                candidate = self.console_buffer[-back]
                if candidate["text"]:
                    last = candidate
                    break
        elif self.console_buffer:
            last = self.console_buffer[-1]

        if (last is not None and last["level"] == rec["level"]
                and last["noise"] == rec["noise"]
                and last["text"] == rec["text"]):
            last["n"] = last.get("n", 1) + 1
            last["ts"] = rec["ts"] or last["ts"]
            if getattr(self, "console", None) is not None and self._visible(last):
                self._redraw_tail(last)
            return

        self.console_buffer.append(rec)
        self._count_delta(rec, 1)
        over = len(self.console_buffer) - MAX_CONSOLE_LINES
        if over > 0:
            for old in self.console_buffer[:over]:
                self._count_delta(old, -1)
            del self.console_buffer[:over]
        # lines can arrive before the page exists - buffer them and move on
        if getattr(self, "console", None) is None:
            return
        self._schedule_counts()
        if self._visible(rec):
            self._append_record(rec)

    def _absorb(self, line: str) -> None:
        plain = clean(line)

        if self.history:
            try:
                made = self.history.ingest_line(line)
                if made and self.current_page == "history":
                    self._repeat('history_refresh', 100, self.refresh_history)
            except Exception as exc:
                log_once(self, f"history error: {exc}")

        m = RE_CONNECT.search(plain)
        if m:
            name = m.group(1).strip()
            self.players.add(name)
            self._refresh_players()
            self._fire_queue(name)
        m = RE_DISCONNECT.search(plain)
        if m:
            timer = self._timers.pop('queue_player_' + m.group(1).strip(), None)
            if timer:
                self.after_cancel(timer)
            self.players.discard(m.group(1).strip())
            self._refresh_players()
        m = RE_SPAWN.search(plain)
        if m:
            self.players.add(m.group(1).strip())
            self._refresh_players()

        m = RE_VERSION.search(plain)
        if m:
            self.server_version = m.group(1)

        m = RE_PACK.search(plain)
        if m:
            self.packs.append(m.group(1))

        m = RE_NEXT_RESTART.search(plain)
        if m:
            try:
                self.next_restart = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
            except ValueError:
                pass

        if RE_STARTED.search(plain):
            self.server_up = True
            self.started_at = datetime.now()
            self._set_state("Running", OK)
        elif RE_STOPPED.search(plain):
            self.server_up = False
            if self.history:
                self.history.end_sessions()
            self.players.clear()
            self._refresh_players()

    def _fire_queue(self, name: str) -> None:
        """Run anything queued for a player once they have joined."""
        if not self.history:
            return
        try:
            pending = self.history.queue_for(name)
        except Exception:
            return
        if not pending:
            return
        watermark = self.history.queue_watermark()

        def run():
            if name not in self.players or not self.manager.running() or self._stopping_on_purpose:
                return
            try:
                sent = self.history.queue_deliver(name, self.manager.send, before_id=watermark)
                for item in sent:
                    self.log_line(f"[queue] sent for {name}: {item['command']}", 'ok')
                if self.current_page == 'history':
                    self.refresh_queue()
            except Exception as exc:
                log_once(self, f'Command delivery failed: {exc}')
        self._repeat('queue_player_' + name, 6000, run)
        self.log_line(f"[launcher] {len(pending)} queued command(s) for {name} "
                      "will run in a few seconds", "mgr")

    def _refresh_players(self) -> None:
        if hasattr(self, 'player_directory'):
            self.player_directory.refresh(force=False)

    def _set_state(self, text: str, colour: str) -> None:
        self.status_text = text
        self._paint_pill(text, colour)
        running = self.manager.running()
        busy = bool(self._maintenance or getattr(self, '_update_busy', False) or self._install_stage)
        self.btn_start.set_enabled(not running and not busy and not self.stats.found)
        self.btn_stop.set_enabled(running)
        self.btn_restart.set_enabled(running)

    # -- periodic ---------------------------------------------------------

    def _drain_queue(self) -> None:
        if getattr(self, "_closing", False):
            return
        drained = 0
        deadline = time.monotonic() + 0.018
        while drained < 200 and time.monotonic() < deadline:
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                break
            drained += 1
            if kind == "line":
                self._absorb(payload)
                self.log_line(payload)
            elif kind == 'runtime_line':
                self.log_line(payload)
            elif kind == 'player_role_done':
                self.player_directory.role_done(payload)
            elif kind == 'runtime_state':
                self.players = set(payload.get('players', []))
                self.server_up = payload.get('server_up', False)
                self.server_version = payload.get('version') or self.server_version
                try:
                    self.started_at = datetime.fromisoformat(payload['started_at']) if payload.get('started_at') else None
                    self.next_restart = datetime.fromisoformat(payload['next_restart']) if payload.get('next_restart') else None
                except ValueError:
                    pass
                self._refresh_players()
                state = 'Stopping' if payload.get('stopping') else 'Running' if self.server_up else 'Starting'
                self._set_state(state, GREEN if self.server_up else AMBER)
                if payload.get('maintenance'):
                    self.task_status.set(payload['maintenance'])
                if self.current_page == 'history':
                    self._repeat('history_refresh', 100, self.refresh_history)
            elif kind == 'feature_progress':
                done, total, label = payload
                self._task_phase = label
                self.task_status.set(f'{label} • {done}/{total}' if total else label)
                if total:
                    self.task_progress.stop()
                    self.task_progress.configure(mode='determinate', maximum=total, value=done)
            elif kind == 'feature_report':
                self.show_exportable_report(*payload)
            elif kind == 'command_reference':
                self.command_help_loaded(*payload)
            elif kind == 'quick_command_done':
                self.admin_quick_command_done(*payload)
            elif kind == "error":
                self.log_line(payload, "err")
                self.notify("error", "Something failed", payload, "launcher")
            elif kind == "backups_changed":
                self.refresh_backups()
            elif kind == 'maintenance_done':
                label, detail, error = payload
                self._maintenance = ''
                self.task_progress.stop()
                self.task_progress.pack_forget()
                self.task_status.set(f'{label}: failed' if error else detail)
                self.log_line(f'[launcher] {error or detail}', 'err' if error else 'ok')
                self.notify('error' if error else 'ok', label + (' failed' if error else ' complete'), error or detail, 'backups')
                self.refresh_backups()
                if not error and label in ('Restore complete point', 'Apply mod profile', 'Recover interrupted changes', 'Install / update in-game tools', 'Rebuild in-game help/menu'):
                    self.load_props()
                    self.max_players = self._read_max_players()
                    self.refresh_mods()
                    self.refresh_update()
                    self.refresh_health()
                self._set_state('Running' if self.server_up else 'Stopped', GREEN if self.server_up else RED)
            elif kind == 'shutdown_done':
                if self.manager.running():
                    self.task_status.set('Still stopping. Wait for the server to finish, then close again.')
                    self._close_pending = False
                else:
                    self.on_close()
            elif kind == "health":
                self._render_health(payload)
            elif kind == "update_avail":
                self._render_available(payload)
                self.btn_check.set_enabled(True)
            elif kind == "update_check":
                version, state, size = payload
                if state == "available":
                    self.version_note.configure(
                        text=f"{version} added, {size / 1048576:.0f} MB", fg=GREEN)
                    self.refresh_catalogue()
                    for item in self.version_tree.get_children():
                        if self.version_tree.item(item, "values")[0] == version:
                            self.version_tree.selection_set(item)
                            self.version_tree.see(item)
                            break
                elif state == "blocked":
                    self.version_note.configure(
                        text="the download server is rate-limiting - "
                             "wait a few minutes", fg=AMBER)
                else:
                    self.version_note.configure(
                        text=f"no build {version} on the download server",
                        fg=AMBER)
                self.refresh_update()
            elif kind == "update_progress":
                frac, text = payload
                self._set_update_progress(frac, text)
            elif kind == "update_done":
                self._update_finished(payload)
            elif kind == "update_fail":
                self._update_failed(payload)
            elif kind == "queued":
                self.log_line(f"[queue] {payload}", "ok")
                self.manager.send(payload)
            elif kind == "mods_changed":
                self.refresh_mods()
            elif kind == 'helper_done':
                self._maintenance = ''
                self.task_status.set(f'{payload} finished. See Console for the result.')
                self._set_state(self.status_text, GREEN if self.server_up else RED)
            elif kind == "exit":
                self.server_up = False
                self.started_at = None
                self.next_restart = None
                self.players.clear()
                self._refresh_players()
                self.log_line("[launcher] the server manager has stopped", "mgr")
                self._set_state("Stopped", BAD)
                if not getattr(self, "_stopping_on_purpose", False):
                    self.notify("warn", "The server stopped",
                                "The manager exited. If you did not stop it, "
                                "the Console page has the last output.",
                                "server")
        self._repeat("queue", 15 if not self.q.empty() else POLL_QUEUE_MS, self._drain_queue)

    def _tick_stats(self) -> None:
        if getattr(self, "_closing", False):
            return
        threading.Thread(target=self.stats.poll, daemon=True).start()
        st = self.stats
        if st.error:
            log_once(self, f'Performance sampling failed: {st.error}')

        self.cards["players"].set(len(self.players),
                                  len(self.players) / max(1, self.max_players))
        if st.found:
            self.cards["cpu"].set(f"{st.cpu_percent:.0f}%", st.cpu_percent / 100)
            self.cards["mem"].set(f"{st.mem_gb:.2f} GB",
                                  st.mem_gb / max(1.0, st.total_gb or 1))
        else:
            self.cards["cpu"].set("-", None)
            self.cards["mem"].set("-", None)
        if st.total_gb:
            self.cards["free"].set(f"{st.free_gb:.1f} GB",
                                   st.free_gb / st.total_gb)
        else:
            self.cards["free"].set("-", None)

        if self.current_page == "update" and UPDATE_OK:
            self.refresh_update()
        self.lbl_version.configure(text=f"version {self.server_version}")
        self.lbl_world.configure(text=f"world: {self.level_name()}")
        self.overview_label.configure(text=f'Bedrock {self.server_version}  ·  {self.status_text}')
        self._set_state('External server' if st.found and not self.manager.running() and not self._maintenance else self.status_text,
                        GREEN if self.server_up else AMBER if st.found else RED)
        self._repeat("stats", POLL_STATS_MS, self._tick_stats)

    def _tick_clock(self) -> None:
        if getattr(self, "_closing", False):
            return
        running = self.manager.running()
        if self.started_at and running:
            self.cards["uptime"].set(human_delta(datetime.now() - self.started_at))
        else:
            self.cards["uptime"].set("-")

        if self.next_restart and running:
            left = self.next_restart - datetime.now()
            secs = left.total_seconds()
            if secs <= 0:
                self.cards["next"].set("due")
            else:
                # meter fills over the last hour before a restart
                self.cards["next"].set(human_delta(left),
                                       max(0.0, 1 - min(secs, 3600) / 3600))
        else:
            self.cards["next"].set("-")
        self._repeat("clock", 1000, self._tick_clock)

    # -- shutdown ---------------------------------------------------------

    def _repeat(self, name: str, delay: int, func) -> None:
        """Schedule a repeating tick, remembering the id.

        A Python-side "are we closing" flag is not enough: destroy() drops
        the Tcl command registration while the timer is still armed, so Tcl
        errors before the callback ever reaches Python. The ids have to be
        cancelled, not ignored.
        """
        if self._closing:
            return
        previous = self._timers.get(name)
        if previous:
            try:
                self.after_cancel(previous)
            except tk.TclError:
                pass
        self._timers[name] = self.after(delay, func)

    def _cancel_timers(self) -> None:
        for timer in self._timers.values():
            try:
                self.after_cancel(timer)
            except (tk.TclError, ValueError):
                pass
        self._timers.clear()

    def on_close(self) -> None:
        if self._maintenance or getattr(self, '_update_busy', False):
            self.task_status.set('Finish the current operation before closing the launcher.')
            return
        if self.manager.running() and not getattr(self, '_detach_requested', False):
            if getattr(self, '_close_pending', False):
                return
            choice = messagebox.askyesnocancel(
                    APP_NAME,
                    "Stop the server and close?\n\nYes: save and stop the server.\nNo: keep the server running and close only the launcher.\nCancel: stay here.")
            if choice is None:
                return
            if choice is False:
                self._detach_requested = True
                self.on_close()
                return
            self.log_line("[launcher] stopping the server before exit...", "mgr")
            self._close_pending = True
            self._stopping_on_purpose = True
            self.task_status.set('Saving the world and stopping the server…')
            def stop_then_close():
                self.manager.shutdown()
                self.q.put(('shutdown_done', ''))
            threading.Thread(target=stop_then_close, daemon=True).start()
            return
        try:
            self.ui["geometry"] = self.winfo_geometry()
        except tk.TclError:
            pass
        save_ui_config(self.root_dir, self.ui)
        if hasattr(self, 'app_updates'):
            self.app_updates.close()
        self.manager.disconnect()
        if self.history:
            self.history.close()
        # pending after() callbacks would otherwise fire into a dead
        # interpreter and spray "invalid command name" into stderr
        self._closing = True
        self._cancel_timers()
        for timer in self.tk.splitlist(self.tk.call('after', 'info')):
            # Cancel Tcl scheduling without deleting a child widget's Python
            # callback command through this root (the child still owns it).
            self.tk.call('after', 'cancel', timer)
        self.destroy()


# ---------------------------------------------------------------------------


def main() -> int:
    root_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 \
        else Path(__file__).resolve().parent

    if not (root_dir / "server.properties").exists():
        temp = tk.Tk()
        temp.withdraw()
        messagebox.showerror(
            APP_NAME,
            f"No server.properties found in:\n{root_dir}\n\n"
            "Put this launcher in your bedrock-server folder, next to "
            "bedrock_server.exe.")
        temp.destroy()
        return 2

    app = Launcher(root_dir)
    app.mainloop()
    return 0


if __name__ == "__main__":
    from release_entry import main as release_main
    sys.exit(release_main(Launcher))
