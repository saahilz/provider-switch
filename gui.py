"""Provider Switch — desktop GUI.

A themed tkinter front-end over scripts/provider_switch.py (stdlib only).
It stores nothing itself: profiles live in the plugin's profiles.json, keys
in the OS credential store, and every action shells out to the audited CLI,
so settings editing, backups and rollback work unchanged.

    python gui.py

The pill dropdown picks the active profile; each profile names its own
credential reference, so switching provider also switches which API key the
apiKeyHelper serves. Keys are typed into a hidden field and piped to the
CLI on stdin — never argv, never a file, never this window's log.
"""
from __future__ import annotations

import json
import math
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox as mb
from tkinter import ttk

ROOT = Path(__file__).resolve().parent
CLI = ROOT / "scripts" / "provider_switch.py"
DATA_DIR = Path.home() / ".claude" / "plugins" / "data" / "provider-switch"
PROFILES_FILE = DATA_DIR / "profiles.json"
CRED_DIR = DATA_DIR / "credentials"

NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# ——— palette ————————————————————————————————————————————————
BG        = "#101014"   # window
RAISED    = "#16161C"   # cards
FIELD     = "#1C1C24"   # inputs, header, footer
HOVER     = "#22222C"
LINE      = "#2A2A36"
TEXT      = "#E8E8F0"
MUTED     = "#9494A6"
ACCENT    = "#7C6CF0"   # violet — the single loud colour
ACCENT_DN = "#5B4EC0"
ACCENT_BG = "#232041"
GREEN     = "#4CC38A"
AMBER     = "#E0AF68"
RED       = "#F2555A"
ON_ACCENT = "#0D0B1A"
MONO_FAMILIES = ("Cascadia Code", "Cascadia Mono", "Consolas", "Menlo", "Courier New")
UI_FAMILIES = ("Segoe UI Variable Text", "Segoe UI", "SF Pro Text", "Helvetica")

MONO = "Consolas"   # replaced at start-up by _pick_font() with what exists
UI = "Segoe UI"
PAD_X = 22


def _pick_font(families: tuple[str, ...], fallback: str) -> str:
    """tkinter needs one family name; choose the first of ours that exists."""
    try:
        from tkinter import font as tkfont
        available = {f.lower() for f in tkfont.families()}
    except Exception:
        return fallback
    for name in families:
        if name.lower() in available:
            return name
    return fallback


def _slug(ref: str) -> str:
    # Mirrors credential_store._slug. The CLI is the only writer; this just
    # powers the "key stored" status column.
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in ref)


def load_state() -> tuple[dict, dict]:
    """(profiles doc, provider map). Survives first-run: no profiles.json yet."""
    doc = {"profiles": {}, "active": None, "default": None}
    if PROFILES_FILE.exists():
        try:
            doc = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass  # the CLI's `validate` explains it; keep the window openable
    providers = {p["id"]: p for p in
                 json.loads((ROOT / "scripts" / "providers.json").read_text(encoding="utf-8"))["providers"]}
    extra = DATA_DIR / "providers.json"
    if extra.exists():  # user-added providers, same override rule as config.py
        try:
            for p in json.loads(extra.read_text(encoding="utf-8")).get("providers", []):
                providers[p["id"]] = p
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    doc.setdefault("profiles", {})
    return doc, providers


# ——— theme —————————————————————————————————————————————————————

def apply_theme(root: tk.Tk) -> None:
    root.configure(bg=BG)
    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure(".", font=(UI, 10), background=BG, foreground=TEXT,
                    bordercolor=LINE, lightcolor=BG, darkcolor=BG, focuscolor=ACCENT)
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=RAISED)
    style.configure("TLabel", background=BG)
    style.configure("CardMuted.TLabel", background=RAISED, foreground=MUTED, font=(UI, 9))
    style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=(UI, 9))
    style.configure("FieldMuted.TLabel", background=FIELD, foreground=MUTED, font=(UI, 9))

    # A select, not an accent blob: the hero's one primary action is Activate,
    # so the dropdown reads as a quiet field with a chevron.
    style.configure("Select.TCombobox", fieldbackground=FIELD, background=FIELD,
                    foreground=TEXT, arrowcolor=MUTED, borderwidth=0,
                    relief="flat", padding=(12, 8), font=(UI, 10),
                    lightcolor=FIELD, darkcolor=FIELD, troughcolor=FIELD)
    style.map("Select.TCombobox",
              fieldbackground=[("readonly", FIELD), ("active", HOVER)],
              foreground=[("readonly", TEXT)],
              arrowcolor=[("active", TEXT)],
              bordercolor=[("focus", ACCENT)])
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", ON_ACCENT)
    root.option_add("*TCombobox*Listbox.font", (UI, 10))

    style.configure("Treeview", background=RAISED, fieldbackground=RAISED,
                    foreground=TEXT, rowheight=40, borderwidth=0, relief="flat",
                    font=(UI, 10))
    style.configure("Treeview.Heading", background=RAISED, foreground=MUTED,
                    font=(UI, 8, "bold"), relief="flat", padding=(10, 9))
    style.map("Treeview", background=[("selected", HOVER)],
              foreground=[("selected", TEXT)])
    style.map("Treeview.Heading", background=[("active", RAISED)])

    style.configure("Vertical.TScrollbar", background=FIELD, troughcolor=RAISED,
                    borderwidth=0, arrowcolor=MUTED)
    style.map("Vertical.TScrollbar", background=[("active", HOVER)])

    style.configure("Card.TCheckbutton", background=RAISED, foreground=TEXT,
                    focuscolor=RAISED, font=(UI, 10))
    style.map("Card.TCheckbutton", background=[("active", RAISED)],
              foreground=[("selected", TEXT)], indicatorcolor=[("selected", ACCENT)])


# ——— small custom widgets ————————————————————————————————————————

class IconButton(tk.Canvas):
    """Flat window control (— and ✕) drawn, not emoji, with hover feedback."""

    def __init__(self, parent, glyph: str, command, hover_bg: str = HOVER) -> None:
        super().__init__(parent, width=46, height=34, bg=FIELD, highlightthickness=0, bd=0)
        self.glyph, self.command, self.hover_bg = glyph, command, hover_bg
        self._draw(FIELD, MUTED)
        self.bind("<Enter>", lambda _e: self._draw(hover_bg, "#FFFFFF" if hover_bg == RED else TEXT))
        self.bind("<Leave>", lambda _e: self._draw(FIELD, MUTED))
        self.bind("<Button-1>", lambda _e: self.command())

    def _draw(self, bg: str, fg: str) -> None:
        self.delete("all")
        self.configure(bg=FIELD)
        self.create_rectangle(0, 0, 46, 34, fill=bg, outline=bg)
        self.create_text(23, 17, text=self.glyph, fill=fg, font=(UI, 11))


def rounded_card(parent, bg: str = RAISED, radius: int = 14, height: int | None = None):
    """(canvas, inner_frame): a rounded-rect card you pack children into.

    `height` must be set for fixed-size cards: a bare Canvas requests ~265px
    by default, and three of those overflow the window (which silently pushes
    the toolbar off-screen). Pass None only for a card that expands to fill.
    """
    cv = tk.Canvas(parent, bg=BG, highlightthickness=0, bd=0)
    if height is not None:
        cv.configure(height=height)
    inner = tk.Frame(cv, bg=bg)
    win = cv.create_window(0, 0, window=inner, anchor="nw")

    def draw_bg(event):
        w, h = event.width, event.height
        if w < 3 or h < 3:
            return
        cv.delete("cardbg")
        # One polygon for the face: the rect+4-ovals trick leaves visibly
        # lumpy corners at small radii.
        cv.create_polygon(_rounded_pts(0, 0, w, h, radius, steps=8),
                          fill=bg, outline=LINE, width=1, tags="cardbg")
        cv.tag_lower("cardbg")  # keep the frame above its own background
        cv.itemconfigure(win, width=w, height=h)

    cv.bind("<Configure>", draw_bg)
    return cv, inner


def flat_entry(parent, width: int = 34) -> tk.Entry:
    e = tk.Entry(parent, bg=FIELD, fg=TEXT, insertbackground=ACCENT, relief="flat",
                 highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT,
                 font=(UI, 10), width=width)
    e.bind("<FocusIn>", lambda _e, x=e: x.configure(highlightbackground=ACCENT))
    e.bind("<FocusOut>", lambda _e, x=e: x.configure(highlightbackground=LINE))
    return e


def _rounded_pts(x0: float, y0: float, x1: float, y1: float, r: float, steps: int = 6):
    """Polygon points tracing a rounded rectangle clockwise.

    Drawn as one filled polygon rather than arcs + rects: the arc seams show
    as hairline gaps at this size, and a single path gives a crisp edge for
    both fill and 1px border.
    """
    pts: list[float] = []
    for cx, cy, a0, a1 in (
        (x0 + r, y0 + r, 180, 270),
        (x1 - r, y0 + r, 270, 360),
        (x1 - r, y1 - r, 0, 90),
        (x0 + r, y1 - r, 90, 180),
    ):
        for i in range(steps + 1):
            a = math.radians(a0 + (a1 - a0) * i / steps)
            pts.extend((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


class PillButton(tk.Canvas):
    """A drawn button: rounded, hover/press states, four variants.

    Stock tk.Button can't do rounded corners, real padding, or a 1px accent
    border on a dark surface — this draws the shape and re-fills it on hover
    instead of rebuilding, so there is no flicker.
    """

    VARIANTS = {
        #            rest      hover     text       border
        "primary": (ACCENT, ACCENT_DN, ON_ACCENT, ACCENT),
        "secondary": (RAISED, HOVER, TEXT, LINE),
        "ghost": (BG, HOVER, MUTED, BG),
        "danger": ("#2A1A1C", "#3A2226", RED, "#5C2A2F"),
    }

    def __init__(self, parent: tk.Misc, text: str, command, variant: str = "secondary",
                 pad: int = 15, height: int = 34, radius: int = 9, font_size: int = 10,
                 bold: bool = False, parent_bg: str | None = None, icon: str = "") -> None:
        face, hover, fg, border = self.VARIANTS[variant]
        f = tkfont.Font(family=UI, size=font_size, weight="bold" if bold or variant in ("primary", "danger") else "normal")
        label = f"{icon}  {text}" if icon else text
        w = f.measure(label) + pad * 2
        bg = parent_bg if parent_bg is not None else _parent_bg(parent)
        super().__init__(parent, width=w, height=height, bg=bg, highlightthickness=0, bd=0)
        self._face, self._hover, self._fg, self._border = face, hover, fg, border
        self._command = command
        self._enabled = True
        self._bw, self._bh, self._r = w, height, radius
        self._font, self._pad, self._label_text = f, pad, label
        self._draw()
        self.bind("<Enter>", lambda _e: self._restyle(self._hover))
        self.bind("<Leave>", lambda _e: self._restyle(self._face))
        self.bind("<Button-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.configure(cursor="hand2")

    def _draw(self) -> None:
        self.delete("all")
        self._shape = self.create_polygon(
            _rounded_pts(0.5, 0.5, self._bw - 0.5, self._bh - 0.5, self._r),
            fill=self._face, outline=self._border, width=1, smooth=False)
        self._label = self.create_text(self._bw / 2, self._bh / 2, text=self._label_text,
                                       fill=self._fg, font=self._font)

    def set_text(self, text: str) -> None:
        """Relabel and re-measure — a canvas has no `text` option."""
        self._label_text = text
        self._bw = self._font.measure(text) + self._pad * 2
        self.configure(width=self._bw)
        self._draw()

    def _restyle(self, fill: str) -> None:
        if self._enabled:
            self.itemconfigure(self._shape, fill=fill)

    def _press(self, _e) -> None:
        if not self._enabled:
            return
        self.itemconfigure(self._shape, fill=self._border)
        self.move(self._label, 0, 1)  # a 1px nudge reads as a physical press

    def _release(self, _e) -> None:
        if not self._enabled:
            return
        self.move(self._label, 0, -1)
        self.itemconfigure(self._shape, fill=self._hover)
        self._command()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.itemconfigure(self._shape, fill=self._face if enabled else FIELD,
                           outline=self._border if enabled else LINE)
        self.itemconfigure(self._label, fill=self._fg if enabled else MUTED)
        self.configure(cursor="hand2" if enabled else "arrow")


class PillSelect(tk.Canvas):
    """Drawn dropdown: a canvas field that opens a dark Listbox.

    ttk.Combobox refuses the dark field styling on Windows — it paints a white
    entry box regardless of fieldbackground — so the select is drawn like the
    rest of the chrome instead.
    """

    def __init__(self, parent, width: int = 250, height: int = 34,
                 parent_bg: str | None = None, placeholder: str = "Select a profile") -> None:
        bg = parent_bg if parent_bg is not None else _parent_bg(parent)
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._cw, self._ch = width, height
        self._values: list[str] = []
        self._index: int | None = None
        self._placeholder = placeholder
        self._open: tk.Toplevel | None = None
        self._draw()
        self.bind("<Button-1>", self._toggle)
        self.bind("<Enter>", lambda _e: self.itemconfigure(self._face, fill=HOVER))
        self.bind("<Leave>", lambda _e: self.itemconfigure(self._face, fill=FIELD))

    # ---- drawing ----

    def _draw(self) -> None:
        self.delete("all")
        self._face = self.create_polygon(
            _rounded_pts(0.5, 0.5, self._cw - 0.5, self._ch - 0.5, 9),
            fill=FIELD, outline=LINE, width=1)
        chosen = self._index is not None
        label = self._values[self._index] if chosen else self._placeholder
        self.create_text(13, self._ch / 2, text=label, anchor="w",
                         fill=TEXT if chosen else MUTED, font=(UI, 10))
        self.create_text(self._cw - 14, self._ch / 2, text="▾", anchor="e",
                         fill=MUTED if not chosen else TEXT, font=(UI, 9))

    # ---- api used by App ----

    def set_values(self, values: list[str], index: int | None = None) -> None:
        self._values = list(values)
        self._index = index if index is not None and 0 <= index < len(values) else None
        self._draw()

    def set_index(self, index: int | None) -> None:
        self._index = index if index is not None and 0 <= index < len(self._values) else None
        self._draw()

    def get_index(self) -> int | None:
        return self._index

    def get_value(self) -> str:
        return self._values[self._index] if self._index is not None else ""

    # ---- popup ----

    def _toggle(self, _e=None) -> None:
        if self._open is not None:
            self._close()
            return
        if not self._values:
            return
        top = tk.Toplevel(self)
        top.overrideredirect(True)
        top.configure(bg=LINE)
        rows = min(len(self._values), 8)
        top.geometry(f"{self._cw}x{rows * 26 + 4}+{self.winfo_rootx()}+{self.winfo_rooty() + self._ch + 2}")
        lb = tk.Listbox(top, bg=RAISED, fg=TEXT, selectbackground=ACCENT,
                        selectforeground=ON_ACCENT, relief="flat", bd=0,
                        highlightthickness=0, activestyle="none", font=(UI, 10),
                        exportselection=False)
        for v in self._values:
            lb.insert("end", v)
        if self._index is not None:
            lb.selection_set(self._index)
            lb.see(self._index)
        lb.pack(fill="both", expand=True, padx=1, pady=1)

        def choose(_ev=None):
            sel = lb.curselection()
            if sel:
                self._index = sel[0]
                self._draw()
            self._close()

        lb.bind("<ButtonRelease-1>", choose)
        lb.bind("<Return>", choose)
        top.bind("<Escape>", lambda _e: self._close())
        lb.focus_set()
        self._open = top
        try:
            top.grab_set()
        except tk.TclError:
            pass

    def _close(self) -> None:
        if self._open is not None:
            try:
                self._open.grab_release()
            except tk.TclError:
                pass
            self._open.destroy()
            self._open = None


def _parent_bg(widget: tk.Misc) -> str:
    try:
        return widget.cget("bg")
    except tk.TclError:
        return BG


def text_button(parent, text: str, command, primary: bool = False, danger: bool = False,
                variant: str | None = None, **kw) -> PillButton:
    """Back-compat factory: the whole app routes through here."""
    if variant is None:
        variant = "primary" if primary else ("danger" if danger else "secondary")
    return PillButton(parent, text, command, variant=variant, **kw)


# ——— dialogs ————————————————————————————————————————————————————

class Modal(tk.Toplevel):
    """Frameless rounded dialog base with a draggable title row and ✕."""

    def __init__(self, parent: tk.Misc, title: str, width: int, height: int) -> None:
        super().__init__(parent)
        self.overrideredirect(True)
        self.configure(bg=BG)
        # `height` is only the pre-measurement guess used for centring; the
        # window is resized to its content by _fit_height below, or the last
        # row of buttons gets clipped on a frameless dialog.
        px = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        py = parent.winfo_rooty() + max(20, (parent.winfo_height() - height) // 3)
        self.geometry(f"{width}x{height}+{max(px, 40)}+{max(py, 40)}")

        head = tk.Frame(self, bg=FIELD)
        head.pack(fill="x")
        self.head = head
        drag: dict = {}
        head.bind("<Button-1>", lambda e: drag.update(x=e.x_root, y=e.y_root, wx=self.winfo_x(), wy=self.winfo_y()))
        head.bind("<B1-Motion>", lambda e: self.geometry(
            f"+{drag['wx'] + e.x_root - drag['x']}+{drag['wy'] + e.y_root - drag['y']}"))
        tk.Label(head, text=title, bg=FIELD, fg=TEXT, font=(UI, 11, "bold")).pack(side="left", padx=16)
        IconButton(head, "✕", self.done).pack(side="right")

        self.body = tk.Frame(self, bg=BG)
        self.body.pack(fill="both", expand=True)
        self.grab_set()
        self.bind("<Escape>", lambda _e: self.done())
        # A fixed height clips content when a label wraps or fonts differ;
        # grow to fit whatever the dialog actually built.
        self.after(0, self._fit_height)

    def _fit_height(self) -> None:
        if not self.winfo_exists():
            return
        self.update_idletasks()
        need = self.head.winfo_reqheight() + self.body.winfo_reqheight()
        self.geometry(f"{self.winfo_width()}x{need}")

    def done(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


def ask_key(parent: tk.Misc, ref: str) -> str | None:
    """Hidden-entry key capture; returns the key or None if cancelled."""
    dlg = Modal(parent, f"API key · {ref}", 480, 220)
    out: dict = {"key": None}
    shown = {"v": False}
    tk.Label(dlg.body, text="Encrypted in the OS credential store — never in "
                            "profiles.json, settings, or the log.",
             bg=BG, fg=MUTED, font=(UI, 9), justify="left").pack(anchor="w", padx=22, pady=(18, 8))
    entry = flat_entry(dlg.body, width=52)
    entry.configure(insertwidth=0, show="●")
    entry.pack(fill="x", padx=22, ipady=10)
    entry.focus_set()

    def toggle():
        shown["v"] = not shown["v"]
        entry.configure(show="" if shown["v"] else "●")
        eye.set_text("hide" if shown["v"] else "show")

    def ok(_=None):
        value = entry.get().strip()
        if value:
            out["key"] = value
            dlg.done()

    row = tk.Frame(dlg.body, bg=BG)
    row.pack(fill="x", padx=22, pady=18)
    eye = text_button(row, "show", toggle, variant="ghost", parent_bg=BG)
    eye.pack(side="left")
    text_button(row, "Save key", ok, primary=True, parent_bg=BG).pack(side="right")
    dlg.bind("<Return>", ok)
    dlg.wait_window()
    return out["key"]


def ask_confirm(parent: tk.Misc, title: str, msg: str, ok_label: str, danger: bool = False) -> bool:
    dlg = Modal(parent, title, 460, 210)
    out = {"v": False}
    tk.Label(dlg.body, text=msg, bg=BG, fg=TEXT, font=(UI, 10), justify="left",
             wraplength=410).pack(anchor="w", padx=24, pady=(20, 12))
    row = tk.Frame(dlg.body, bg=BG)
    row.pack(fill="x", padx=24, pady=(4, 18))
    text_button(row, "Cancel", dlg.done).pack(side="left")

    def yes():
        out["v"] = True
        dlg.done()

    text_button(row, ok_label, yes, primary=not danger, danger=danger).pack(side="right")
    dlg.wait_window()
    return out["v"]


class ProfileDialog(Modal):
    def __init__(self, app: "App", title: str, existing: tuple[str, dict] | None) -> None:
        super().__init__(app, title, 560, 430)
        self.app = app
        _, providers = load_state()
        rows = [("name", "Name (lowercase, e.g. work)"), ("provider", "Provider"),
                ("base_url", "Base URL (blank = provider default)"), ("model", "Model"),
                ("credential", "Key reference (blank = name)"), ("auth", "Auth (blank = provider default)")]
        self.w: dict[str, object] = {}
        for key, label in rows:
            box = tk.Frame(self.body, bg=BG)
            box.pack(fill="x", padx=24, pady=5)
            tk.Label(box, text=label, bg=BG, fg=MUTED, font=(UI, 9), width=30, anchor="w").pack(side="left")
            if key == "provider":
                w = ttk.Combobox(box, state="readonly", width=34,
                                 values=[f"{pid}  ·  {p.get('label', pid)}" for pid, p in sorted(providers.items())])
                w.set("anthropic  ·  " + providers["anthropic"]["label"])
            elif key == "auth":
                w = ttk.Combobox(box, width=34, values=["", "api_key", "bearer_token", "none"])
            else:
                w = flat_entry(box, width=34)
            w.pack(side="left")
            self.w[key] = w
        if existing:
            name, prof = existing
            pid = prof.get("provider", "")
            self.w["name"].insert(0, name)
            self.w["name"].configure(state="disabled")
            self.w["provider"].set(f"{pid}  ·  {providers.get(pid, {}).get('label', pid)}")
            self.w["base_url"].insert(0, prof.get("baseUrl") or "")
            self.w["model"].insert(0, prof.get("model", ""))
            self.w["credential"].insert(0, prof.get("credential", ""))
            self.w["auth"].set(prof.get("auth", ""))
        self.want_key = tk.BooleanVar(value=not (existing and existing[1].get("auth") == "none"))
        chk = tk.Checkbutton(self.body, text="Paste API key now", variable=self.want_key,
                             bg=BG, fg=TEXT, activebackground=BG, activeforeground=TEXT,
                             selectcolor=ACCENT_BG, highlightthickness=0, bd=0,
                             font=(UI, 10), anchor="w")
        chk.pack(anchor="w", padx=24, pady=(12, 4))
        err = tk.Label(self.body, text="", bg=BG, fg=RED, font=(UI, 9))
        err.pack(anchor="w", padx=24)
        self._err = err
        row = tk.Frame(self.body, bg=BG)
        row.pack(fill="x", padx=24, pady=(6, 20))
        text_button(row, "Cancel", self.done).pack(side="left")
        text_button(row, "Save", self._ok, primary=True).pack(side="right")

    def _ok(self) -> None:
        name = self.w["name"].get().strip()
        fields = {}
        for key, w in self.w.items():
            v = w.get().strip()
            fields[key] = v.split("  ·  ")[0] if key == "provider" else v
        if not name or not fields["provider"] or not fields["model"]:
            self._err.configure(text="name, provider and model are required")
            return
        key = None
        if self.want_key.get():
            key = ask_key(self, fields["credential"] or name)
            if key is None:
                return  # cancelled the key prompt: don't half-save
        self.app.save_profile(name, fields, key)
        self.done()


# ——— main window ————————————————————————————————————————————————

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        global UI, MONO
        UI = _pick_font(UI_FAMILIES, UI)
        MONO = _pick_font(MONO_FAMILIES, MONO)
        self.title("Provider Switch")
        self.geometry("880x640")
        self.minsize(840, 700)  # below this the toolbar drops off the card stack
        apply_theme(self)
        self._q: queue.Queue = queue.Queue()
        self.doc, self.providers = {"profiles": {}}, {}
        self._build()
        self.refresh()
        self._run_cli(["validate"])  # a console line, not a modal error
        self.after(60, self._drain)

    # ---- structure ----

    def _build(self) -> None:
        # custom title bar (frameless window, draggable)
        self.overrideredirect(True)
        self.geometry("900x800+120+50")
        drag: dict = {}
        head = tk.Frame(self, bg=FIELD)
        head.pack(fill="x")
        head.bind("<Button-1>", lambda e: drag.update(x=e.x_root, y=e.y_root, wx=self.winfo_x(), wy=self.winfo_y()))
        head.bind("<B1-Motion>", lambda e: self.geometry(
            f"+{drag['wx'] + e.x_root - drag['x']}+{drag['wy'] + e.y_root - drag['y']}"))
        brand = tk.Frame(head, bg=FIELD)
        brand.pack(side="left")
        tk.Label(brand, text="◍", bg=FIELD, fg=ACCENT, font=(UI, 16)).pack(side="left", padx=(18, 0))
        tk.Label(brand, text="  Provider Switch", bg=FIELD, fg=TEXT, font=(UI, 11, "bold")).pack(side="left", pady=12)
        tk.Label(brand, text="profiles · credential store · settings.json", bg=FIELD,
                 fg=MUTED, font=(UI, 9)).pack(side="left", padx=(14, 0))
        IconButton(head, "—", self.withdraw).pack(side="right", padx=(0, 46))
        IconButton(head, "✕", self.destroy).pack(side="right")
        tk.Frame(self, bg=LINE, height=1).pack(fill="x")  # hairline under the bar

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=PAD_X, pady=(16, 0))
        self.body = body

        # — hero: what is routing right now, and the one primary action —
        card, inner = rounded_card(body, height=112)
        card.pack(fill="x", pady=(0, 16))
        left = tk.Frame(inner, bg=RAISED)
        left.pack(side="left", padx=20, pady=16)
        tk.Label(left, text="ROUTING NOW", bg=RAISED, fg=ACCENT, font=(UI, 8, "bold")).pack(anchor="w")
        self.hero_name = tk.Label(left, text="—", bg=RAISED, fg=TEXT, font=(UI, 17, "bold"))
        self.hero_name.pack(anchor="w", pady=(2, 2))
        self.hero_meta = tk.Label(left, text="", bg=RAISED, fg=MUTED, font=(UI, 9))
        self.hero_meta.pack(anchor="w")

        right = tk.Frame(inner, bg=RAISED)
        right.pack(side="right", padx=20, pady=16)
        self.pick = PillSelect(right, width=248, parent_bg=RAISED)
        self.pick.pack(side="left", padx=(0, 10))
        self.switch_btn = text_button(right, "Activate", self.switch_selected, primary=True)
        self.switch_btn.pack(side="left")
        status = tk.Frame(right, bg=RAISED)
        status.pack(side="left", padx=(18, 0))
        self.status_dot = tk.Canvas(status, width=16, height=16, bg=RAISED, highlightthickness=0, bd=0)
        self.status_dot.pack(side="left", padx=(0, 6))
        self.active_lbl = tk.Label(status, text="", bg=RAISED, fg=MUTED, font=(UI, 9))
        self.active_lbl.pack(side="left")

        # — profiles —
        self._section(body, "Profiles", right_action=("+ Add profile", self.add))
        tcard, tinner = rounded_card(body, height=268)
        tcard.pack(fill="x", expand=False, pady=(0, 10))
        cols = ("provider", "model", "url", "state")
        self.table = ttk.Treeview(tinner, columns=cols, show="tree headings", height=6)
        self.table.heading("#0", text="Profile")
        self.table.column("#0", width=150, minwidth=110)
        for key, head, w in (("provider", "Provider", 170), ("model", "Model", 165),
                             ("url", "Base URL", 235), ("state", "Key", 90)):
            self.table.heading(key, text=head)
            self.table.column(key, width=w, minwidth=90,
                              anchor="center" if key == "state" else "w")
        self.table.tag_configure("ok", foreground=GREEN)
        self.table.tag_configure("bad", foreground=RED)
        self.table.tag_configure("none", foreground=MUTED)
        self.table.pack(side="left", fill="both", expand=True, padx=(2, 0), pady=(0, 2))
        sb = ttk.Scrollbar(tinner, orient="vertical", command=self.table.yview)
        sb.pack(side="right", fill="y", padx=(0, 2), pady=(0, 2))
        self.table.configure(yscrollcommand=sb.set)
        self._sel_name: str | None = None
        self.table.bind("<<TreeviewSelect>>", lambda _e: self._remember_selection())

        # — row actions: routine on the left, destructive pushed right —
        bar = tk.Frame(body, bg=BG)
        bar.pack(fill="x", pady=(0, 16))
        for label, cmd in (("Edit", self.edit), ("Set key", self.set_key), ("Test", self.test_selected)):
            text_button(bar, label, cmd, parent_bg=BG).pack(side="left", padx=(0, 6))
        text_button(bar, "Refresh", self.refresh, parent_bg=BG, variant="ghost").pack(side="left")
        text_button(bar, "Rollback", self.rollback, parent_bg=BG, variant="ghost").pack(side="right")
        text_button(bar, "Delete", self.delete, parent_bg=BG, danger=True).pack(side="right", padx=(0, 6))

        # — activity —
        self._section(body, "Activity")
        ccard, cinner = rounded_card(body, height=120)
        ccard.pack(fill="both", expand=True, pady=(0, 6))
        self.log = tk.Text(cinner, height=5, state="disabled", bg=RAISED, fg=MUTED,
                           relief="flat", bd=0, font=(MONO, 9), padx=16, pady=12,
                           wrap="word", spacing1=2, spacing3=2)
        self.log.pack(fill="both", expand=True)
        for tag, colour in (("ok", GREEN), ("bad", RED), ("cmd", ACCENT), ("note", TEXT), ("muted", MUTED)):
            self.log.tag_configure(tag, foreground=colour)

        foot = tk.Frame(self, bg=FIELD)
        foot.pack(fill="x")
        tk.Frame(self, bg=LINE, height=1).pack(fill="x")
        tk.Label(foot, text=f"profiles: {PROFILES_FILE}", bg=FIELD, fg=MUTED,
                 font=(UI, 9)).pack(side="left", padx=PAD_X, pady=8)
        tk.Label(foot, text="API keys live only in the OS credential store", bg=FIELD,
                 fg=MUTED, font=(UI, 9)).pack(side="right", padx=PAD_X)

    def _section(self, parent, title: str, right_action: tuple[str, object] | None = None) -> tk.Frame:
        """Small caps section heading with an optional trailing action."""
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(0, 7))
        tk.Label(row, text=title.upper(), bg=BG, fg=MUTED, font=(UI, 8, "bold")).pack(side="left")
        if right_action:
            label, cmd = right_action
            text_button(row, label, cmd, parent_bg=BG, variant="ghost").pack(side="right")
        return row

    # ---- cli plumbing ----

    def _run_cli(self, args: list[str], stdin: str | None = None) -> None:
        self._append(f"$ provider_switch {' '.join(args)}", "cmd")
        threading.Thread(target=self._spawn, args=(args, stdin), daemon=True).start()

    def _spawn(self, args: list[str], stdin: str | None) -> None:
        try:
            r = subprocess.run([sys.executable, str(CLI), *args], input=stdin, text=True,
                               capture_output=True, timeout=120, creationflags=NO_WINDOW)
            out = ((r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")).strip()
            self._q.put(("out", out or "ok", r.returncode == 0))
        except Exception as exc:
            self._q.put(("out", f"error: {exc}", False))

    def _drain(self) -> None:
        try:
            while True:
                kind, payload, flag = self._q.get_nowait()
                if kind == "out":
                    self._append(payload, "ok" if flag else "bad")
                    self.refresh()
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _append(self, text: str, tag: str = "muted") -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", (tag,))
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---- view ----

    def refresh(self) -> None:
        self.doc, self.providers = load_state()
        names = sorted(self.doc["profiles"])
        self.table.delete(*self.table.get_children())
        for name in names:
            p = self.doc["profiles"][name]
            needs = p.get("auth", "api_key") != "none"
            ref = p.get("credential", name)
            kind, label = ("none", "cloud") if not needs else (
                ("ok", "● stored") if (CRED_DIR / f"{_slug(ref)}.dpapi").exists() else ("bad", "● missing"))
            self.table.insert("", "end", iid=name, text=name, tags=(kind,), values=(
                self.providers.get(p.get("provider"), {}).get("label", p.get("provider", "?")),
                p.get("model", ""), p.get("baseUrl") or "—", label))
        self._pick_names = names
        labels = [f"{n}  ·  {self.providers.get(self.doc['profiles'][n].get('provider'), {}).get('label', '')}"
                  for n in names]
        active = self.doc.get("active")
        self.pick.set_values(labels, names.index(active) if active in names else None)
        self._draw_status(active)
        self._update_hero(active, names)

    def _update_hero(self, active: str | None, names: list[str]) -> None:
        """The hero answers two questions at a glance: who is routing, and is its key good."""
        shown = active if active in self.doc["profiles"] else self.doc.get("default")
        if shown and shown in self.doc["profiles"]:
            p = self.doc["profiles"][shown]
            provider = self.providers.get(p.get("provider"), {}).get("label", p.get("provider", "?"))
            host = (p.get("baseUrl") or "").replace("https://", "") or "provider default"
            self.hero_name.configure(text=shown)
            self.hero_meta.configure(text=f"{provider}  ·  {p.get('model', '')}  ·  {host}")
            state = "active" if shown == active else "default (not switched)"
            self.active_lbl.configure(text=state)
        else:
            self.hero_name.configure(text="No profile yet")
            self.hero_meta.configure(text="Add one to get started")
            self.active_lbl.configure(text="")

    def _draw_status(self, active: str | None) -> None:
        c = self.status_dot
        c.delete("all")
        kind = "warn"
        if active and active in self.doc["profiles"]:
            p = self.doc["profiles"][active]
            if p.get("auth", "api_key") == "none":
                kind = "ok"
            elif (CRED_DIR / f"{_slug(p.get('credential', active))}.dpapi").exists():
                kind = "ok"
            else:
                kind = "bad"
        colour = {"ok": GREEN, "bad": RED, "warn": AMBER}[kind]
        c.create_oval(1, 1, 15, 15, outline=LINE, fill=RAISED)
        c.create_oval(4, 4, 12, 12, fill=colour, outline=colour)

    def _remember_selection(self) -> None:
        sel = self.table.selection()
        if sel:
            self._sel_name = sel[0]

    def _name_from_pick(self) -> str | None:
        i = self.pick.get_index()
        names = getattr(self, "_pick_names", [])
        return names[i] if i is not None and i < len(names) else None

    def _selected(self) -> str | None:
        return self._sel_name or self._name_from_pick()

    # ---- actions ----

    def switch_selected(self) -> None:
        name = self._name_from_pick()
        if not name:
            self._append("choose a profile in the pill, then Activate.", "note")
            return
        self._run_cli(["switch", name, "--yes"])

    def test_selected(self) -> None:
        name = self._selected()
        if name:
            self._run_cli(["test", name])

    def set_key(self) -> None:
        name = self._selected()
        if not name:
            self._append("select a profile first.", "note")
            return
        ref = self.doc["profiles"][name].get("credential", name)
        key = ask_key(self, ref)
        if key:
            self._run_cli(["credential", "set", ref], key + "\n")

    def delete(self) -> None:
        name = self._selected()
        if not name:
            self._append("select a profile first.", "note")
            return
        if ask_confirm(self, "Delete profile",
                       f"Remove {name!r} from profiles.json?\nIts stored key is kept.",
                       "Delete", danger=True):
            self._run_cli(["remove", name, "--yes"])

    def rollback(self) -> None:
        self._run_cli(["rollback"])

    def add(self) -> None:
        ProfileDialog(self, "Add profile", None)

    def edit(self) -> None:
        name = self._selected()
        if name:
            ProfileDialog(self, f"Edit {name}", (name, self.doc["profiles"][name]))
        else:
            self._append("select a profile to edit.", "note")

    def save_profile(self, name: str, fields: dict[str, str], key: str | None) -> None:
        verb = "add" if name not in self.doc["profiles"] else "edit"
        args = [verb, name]
        for flag in ("provider", "model", "credential", "auth"):
            if fields.get(flag):
                args += [f"--{flag}", fields[flag]]
        if fields.get("base_url"):
            args += ["--base-url", fields["base_url"]]
        self._run_cli(args)
        if key:
            self._run_cli(["credential", "set", fields.get("credential") or name], key + "\n")


def _install_crash_capture(app: tk.Tk) -> None:
    """Under pythonw.exe there is no stderr, so a traceback disappears.

    Tk sends callback errors to a hook instead of raising, which is exactly
    the case that would fail silently in a windowless launch. Write both to a
    log file and surface a dialog, so a broken button is never invisible.
    """
    log_path = DATA_DIR / "gui-error.log"

    def record(exc_type, exc, tb, context: str) -> None:
        import traceback
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} · {context}\n{text}")
        except Exception:
            pass
        try:
            mb.showerror("Provider Switch — error",
                         f"{exc_type.__name__}: {exc}\n\nDetails written to:\n{log_path}")
        except Exception:
            pass

    def tk_hook(exc_type, exc, tb):
        record(exc_type, exc, tb, "Tk callback")

    def sys_hook(exc_type, exc, tb):
        record(exc_type, exc, tb, "main thread")

    app.report_callback_exception = tk_hook
    sys.excepthook = sys_hook


if __name__ == "__main__":
    app = App()
    if "--smoke" in sys.argv:
        app.withdraw()
        app.update()
        app.destroy()
        print("smoke ok")
        sys.exit(0)
    _install_crash_capture(app)
    app.mainloop()
