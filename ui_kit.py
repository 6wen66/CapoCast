"""CapoCast's look: the colours, the bundled font, and the drawn widgets (buttons, sliders, switches, chips).

Everything with a rounded edge is drawn as an anti-aliased picture (see `rounded`), because Tk's own canvas shapes have
hard, jagged edges. Kept apart from roku_listen_gui.py so the window code stays about behaviour.
"""
import ctypes
import os
import sys
import tkinter as tk
import tkinter.font as tkfont

from PIL import Image, ImageDraw, ImageTk

# Red on black. MUTED is a touch lighter than it was, so the small grey text is easier to read.
BG, CARD, EDGE = "#000000", "#0e0e10", "#222226"
FG, MUTED = "#f2f2f4", "#8d8d97"
ACCENT, ACCENT_HI = "#ff4e45", "#ff6a62"
GREEN, AMBER, RED, GREY = "#34d399", "#fbbf24", "#f87171", "#5b5b66"
RAISED, PRESSED, TRACK = "#26262b", "#34343a", "#2a2a2e"   # a hovered / pressed surface, and the groove of a slider


def asset(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", name)


def _load_fonts():
    """Roboto (YouTube's UI font) is bundled and loaded privately, so nothing gets installed system-wide."""
    try:
        return all(ctypes.windll.gdi32.AddFontResourceExW(asset(f), 0x10, 0) > 0   # FR_PRIVATE
                   for f in ("Roboto-Regular.ttf", "Roboto-Medium.ttf", "Roboto-Bold.ttf"))
    except Exception:
        return False


_ROBOTO = _load_fonts()
FONT = "Roboto" if _ROBOTO else "Segoe UI"
FONT_MED = "Roboto Medium" if _ROBOTO else "Segoe UI Semibold"


def dark_titlebar(win):
    """Give a window the dark title bar (Windows 10/11), so pop-up windows match the main one. Call it before the window
    is first shown; a window already on screen needs a withdraw / deiconify afterwards to repaint."""
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
        val = ctypes.c_int(1)
        for attr in (20, 19):          # DWMWA_USE_IMMERSIVE_DARK_MODE (the Win10 build variants)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), 4)
    except Exception:
        pass


def fit_text(font, text, px):
    """`text` shortened with an ellipsis so it is at most `px` pixels wide in `font` (empty if not even that fits)."""
    f = tkfont.Font(font=font)
    if f.measure(text) <= px:
        return text
    while text and f.measure(text + "…") > px:
        text = text[:-1]
    return text.rstrip() + "…" if text else ""


def _shade(color, factor):
    """`color` ("#rrggbb") scaled toward black: the pressed look of a button."""
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % (round(r * factor), round(g * factor), round(b * factor))


def text_y(font, cy):
    """y for create_text so the text's capitals sit visually centered on cy (Tk centers the whole
    ascent+descent box, which makes text look a pixel or two high next to icons)."""
    f = tkfont.Font(font=font)
    asc, desc = f.metrics("ascent"), f.metrics("descent")
    return round(cy + (asc * 0.75) / 2 - asc / 2 + desc / 2)


SUPERSAMPLE = 4   # shapes are drawn this much larger and shrunk, which is what smooths their edges


def _picture(canvas, w, h, r, fill, outline):
    """An anti-aliased rounded rectangle as a PhotoImage, made once per size and colour. Kept on the window, so it
    lives exactly as long as the window does (a PhotoImage belongs to the Tk it was made for)."""
    root = canvas.winfo_toplevel()
    cache = root.__dict__.setdefault("_ui_pictures", {})
    key = (w, h, r, fill, outline)
    pic = cache.get(key)
    if pic is None:
        k = SUPERSAMPLE
        im = Image.new("RGBA", (w * k, h * k), (0, 0, 0, 0))
        ImageDraw.Draw(im).rounded_rectangle([0, 0, w * k - 1, h * k - 1], radius=r * k, fill=fill or None,
                                             outline=outline or None, width=k)
        pic = cache[key] = ImageTk.PhotoImage(im.resize((w, h), Image.LANCZOS))
    return pic


def rounded(canvas, x1, y1, x2, y2, r, fill="", outline="", **_ignored):
    """Draw a smooth rounded rectangle on a canvas; returns the canvas item."""
    w, h = max(2, round(x2 - x1)), max(2, round(y2 - y1))
    pic = _picture(canvas, w, h, max(0, min(round(r), w // 2, h // 2)), fill, outline)
    return canvas.create_image(round(x1), round(y1), image=pic, anchor="nw")


def oval(canvas, cx, cy, radius, fill, outline=""):
    """A smooth filled circle centred on (cx, cy)."""
    return rounded(canvas, cx - radius, cy - radius, cx + radius, cy + radius, radius, fill=fill, outline=outline)


def dot_picture(canvas, diameter, color):
    return _picture(canvas, diameter, diameter, diameter // 2, color, "")


class RoundButton(tk.Canvas):
    def __init__(self, parent, text, command, width=328, height=46):
        super().__init__(parent, width=width, height=height, bg=BG, highlightthickness=0, cursor="hand2")
        self.w, self.h, self.command = width, height, command
        self.text, self.fill, self.fg, self.hover, self.enabled, self.hot = text, ACCENT, "#ffffff", ACCENT_HI, True, False
        self.bind("<Enter>", lambda e: self._hot(True))
        self.bind("<Leave>", lambda e: self._hot(False))
        self.down = False
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", lambda e: self._release())
        self.draw()

    def _hot(self, on):
        self.hot = on
        if not on:
            self.down = False
        self.draw()

    def _press(self, e):
        if self.enabled:
            self.down = True
            self.draw()
            self.command()

    def _release(self):
        if self.down:
            self.down = False
            self.draw()

    def draw(self):
        self.delete("all")
        fill = self.hover if (self.hot and self.enabled) else self.fill
        if self.down and self.enabled:
            fill = _shade(self.fill, 0.85)
        if not self.enabled:
            fill = TRACK
        rounded(self, 1, 1, self.w - 1, self.h - 1, 14, fill=fill, outline="")
        self.create_text(self.w / 2, self.h / 2, text=self.text, fill=self.fg if self.enabled else MUTED,
                         font=(FONT, 11, "bold"))

    def style(self, text, fill, hover, enabled=True):
        self.text, self.fill, self.hover, self.enabled = text, fill, hover, enabled
        self.draw()


class IconButton(tk.Canvas):
    """Small rounded button with a drawn icon: back, back10, rev, play, pause, fwd, fwd10, up, down, left,
    right, ok. With repeat=True, holding it down repeats the command like a held remote button."""

    REPEAT_DELAY, REPEAT_EVERY = 400, 140   # ms, roughly like holding a real Roku remote's arrow

    def __init__(self, parent, kind, command, width=58, height=44, repeat=False):
        super().__init__(parent, width=width, height=height, bg=BG, highlightthickness=0, cursor="hand2")
        self.w, self.h, self.kind, self.command, self.hot = width, height, kind, command, False
        self.repeat, self.down, self._job = repeat, False, None
        self.bind("<Enter>", lambda e: self._hot(True))
        self.bind("<Leave>", lambda e: (self._up(), self._hot(False)))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", lambda e: self._up())
        self.draw()

    def _hot(self, on):
        self.hot = on
        self.draw()

    def _press(self, e):
        self.down = True
        self.draw()
        self.command()
        if self.repeat:
            self._job = self.after(self.REPEAT_DELAY, self._again)

    def _again(self):
        if self.down:
            self.command()
            self._job = self.after(self.REPEAT_EVERY, self._again)

    def _up(self):
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        if self.down:
            self.down = False
            self.draw()

    def set_kind(self, kind):
        if kind != self.kind:
            self.kind = kind
            self.draw()

    def _tri(self, cx, cy, s, right):
        d = s if right else -s
        self.create_polygon(cx - d * 0.6, cy - s, cx - d * 0.6, cy + s, cx + d * 0.8, cy, fill=FG, outline="")

    def draw(self):
        self.delete("all")
        fill = PRESSED if self.down else RAISED if self.hot else CARD
        rounded(self, 1, 1, self.w - 1, self.h - 1, 12, fill=fill, outline=EDGE)
        cx, cy, k = self.w / 2, self.h / 2, self.kind
        if k in ("up", "down"):   # same solid triangles as play / rev / fwd, turned upright
            d = -1 if k == "up" else 1
            self.create_polygon(cx - 8, cy - d * 4.5, cx + 8, cy - d * 4.5, cx, cy + d * 5.5, fill=FG, outline="")
        elif k in ("left", "right"):
            d = 1 if k == "right" else -1
            self.create_polygon(cx - d * 4.5, cy - 8, cx - d * 4.5, cy + 8, cx + d * 5.5, cy, fill=FG, outline="")
        elif k == "ok":
            self.create_text(cx, cy, text="OK", fill=FG, font=(FONT, 11, "bold"))
        elif k == "back":   # the Roku remote's U-turn arrow: points left on top, hooks down on the right
            self.create_line(cx - 9, cy - 4, cx + 3, cy - 4, cx + 9, cy - 4, cx + 9, cy + 6, cx + 3, cy + 6,
                             cx - 3, cy + 6, smooth=True, width=2.5, fill=FG, capstyle="round",
                             arrow="first", arrowshape=(7, 8, 4))
        elif k in ("back10", "fwd10"):
            self.create_text(cx, cy, text=("−10" if k == "back10" else "+10"), fill=FG, font=(FONT, 11, "bold"))
        elif k in ("rev", "fwd"):
            right = k == "fwd"
            self._tri(cx - 6, cy, 7, right)
            self._tri(cx + 6, cy, 7, right)
        elif k == "play":
            self._tri(cx, cy, 9, True)
        else:  # pause
            for dx in (-6, 2):
                self.create_rectangle(cx + dx, cy - 8, cx + dx + 5, cy + 8, fill=FG, outline="")


class DPad(tk.Frame):
    """The remote's arrows and OK as a cross of square buttons, styled like the transport row."""

    # 3 x 54 + 2 x 8 = 178 px: the height of the status card + Connect + transport row it sits beside
    def __init__(self, parent, send, size=54, gap=8):
        super().__init__(parent, bg=BG)
        self.buttons = {}
        for kind, key, row, col in (("up", "Up", 0, 1), ("left", "Left", 1, 0), ("ok", "Select", 1, 1),
                                    ("right", "Right", 1, 2), ("down", "Down", 2, 1)):
            btn = IconButton(self, kind, lambda k=key: send(k), width=size, height=size,
                             repeat=kind != "ok")   # OK doesn't auto-repeat on a Roku remote either
            btn.grid(row=row, column=col, padx=(0 if col == 0 else gap, 0), pady=(0 if row == 0 else gap, 0))
            self.buttons[key] = btn


class Slider(tk.Canvas):
    def __init__(self, parent, lo, hi, value, command, width=328, height=30):
        super().__init__(parent, width=width, height=height, bg=BG, highlightthickness=0, cursor="hand2")
        self.lo, self.hi, self.value, self.command, self.w, self.h = lo, hi, value, command, width, height
        self.pad = 10
        self.mark = 100          # where to draw the 100% tick (None for none)
        self.dragging = False    # true while the mouse is held on it, so live updates don't fight your hand
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "dragging", False))
        self.draw()

    def set_range(self, lo, hi, value, mark=None):
        """Change what the slider measures (PC volume 0-200% vs the TV's 0-100) without calling `command`."""
        self.lo, self.hi, self.mark = lo, hi, mark
        self.value = max(lo, min(hi, value))
        self.draw()

    def get(self):
        return self.value

    def set(self, v):
        self.value = max(self.lo, min(self.hi, v))
        self.draw()

    def _drag(self, e):
        self.dragging = True
        span = self.w - 2 * self.pad
        frac = (e.x - self.pad) / span
        self.set(round(self.lo + frac * (self.hi - self.lo)))
        self.command(self.value)

    def draw(self):
        self.delete("all")
        span = self.w - 2 * self.pad
        x = self.pad + span * (self.value - self.lo) / (self.hi - self.lo)
        y = self.h / 2
        rounded(self, self.pad, y - 2, self.w - self.pad, y + 2, 2, fill=TRACK)
        # mark 100% so the boost range is obvious
        if self.mark is not None:
            x100 = self.pad + span * (self.mark - self.lo) / (self.hi - self.lo)
            self.create_line(x100, y - 7, x100, y + 7, fill="#4a4a54", width=1)
        if x - self.pad >= 4:
            rounded(self, self.pad, y - 2, x, y + 2, 2, fill=ACCENT)
        oval(self, x, y, 9, BG)                       # a ring of background around the knob, so it stands off the bar
        oval(self, x, y, 7, "#ffffff")


class TabSwitch(tk.Canvas):
    """Segmented control: a dark pill with the active segment raised."""

    def __init__(self, parent, names, command, seg_w=104, height=34):
        super().__init__(parent, width=seg_w * len(names) + 8, height=height, bg=BG, highlightthickness=0,
                         cursor="hand2")
        self.names, self.command, self.seg_w, self.h = names, command, seg_w, height
        self.active, self.hot = names[0], None
        self.bind("<Button-1>", lambda e: self._pick(e.x))
        self.bind("<Motion>", lambda e: self._hover(self._at(e.x)))
        self.bind("<Leave>", lambda e: self._hover(None))
        self.draw()

    def _at(self, x):
        i = int((x - 4) // self.seg_w)
        return self.names[i] if 0 <= i < len(self.names) else None

    def _hover(self, name):
        if name != self.hot:
            self.hot = name
            self.draw()

    def _pick(self, x):
        name = self._at(x)
        if name:
            self.command(name)

    def set(self, name):
        self.active = name
        self.draw()

    def draw(self):
        self.delete("all")
        rounded(self, 0, 0, int(self["width"]), self.h, 12, fill="#121215", outline="")
        for i, name in enumerate(self.names):
            x1 = 4 + i * self.seg_w
            if name == self.active:
                rounded(self, x1, 4, x1 + self.seg_w, self.h - 4, 9, fill=RAISED, outline="")
            color = FG if name == self.active else ("#b4b4bc" if name == self.hot else MUTED)
            font = (FONT_MED, 10)
            self.create_text(x1 + self.seg_w / 2, text_y(font, self.h / 2), text=name, fill=color, font=font)


class Toggle(tk.Canvas):
    """iOS-style switch."""

    def __init__(self, parent, on, command, width=46, height=26):
        super().__init__(parent, width=width, height=height, bg=BG, highlightthickness=0, cursor="hand2")
        self.on, self.command, self.w, self.h = on, command, width, height
        self.bind("<Button-1>", lambda e: self._flip())
        self.draw()

    def _flip(self):
        self.on = not self.on
        self.draw()
        self.command(self.on)

    def draw(self):
        self.delete("all")
        r = self.h / 2
        rounded(self, 1, 1, self.w - 1, self.h - 1, r - 1, fill=ACCENT if self.on else TRACK, outline="")
        cx = self.w - r if self.on else r
        oval(self, cx, self.h / 2, r - 4, "#ffffff")


class NavButton(tk.Canvas):
    """Small round browser button: back, fwd, reload."""

    def __init__(self, parent, kind, command, size=34):
        super().__init__(parent, width=size, height=size, bg=BG, highlightthickness=0, cursor="hand2")
        self.kind, self.command, self.s, self.hot, self.down = kind, command, size, False, False
        self.badge = None       # a small status dot on the corner (a colour), or None
        self.active = False     # drawn in the accent colour: the thing this button shows is on
        self.bind("<Enter>", lambda e: self._set(True))
        self.bind("<Leave>", lambda e: self._set(False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", lambda e: self._up())
        self.draw()

    def _set(self, hot):
        self.hot = hot
        self.down = self.down and hot
        self.draw()

    def _press(self, e):
        self.down = True
        self.draw()
        self.command()

    def _up(self):
        if self.down:
            self.down = False
            self.draw()

    def draw(self):
        self.delete("all")
        s, c = self.s, self.s / 2
        if self.hot:
            oval(self, c, c, c - 1, PRESSED if self.down else RAISED)
        col = ACCENT_HI if self.active else FG if self.hot else "#a8a8b0"
        if self.kind in ("back", "fwd"):
            d = -1 if self.kind == "back" else 1
            self.create_line(c - 3 * d, c - 6, c + 3 * d, c, c - 3 * d, c + 6, fill=col, width=2,
                             capstyle="round", joinstyle="round")
        elif self.kind == "phone":
            rounded(self, c - 6, c - 9, c + 6, c + 9, 3, fill="", outline=col)
            self.create_line(c - 2, c + 5, c + 2, c + 5, fill=col, width=2, capstyle="round")
        elif self.kind == "keyboard":   # a keyboard: the Type on TV box
            rounded(self, c - 10, c - 6, c + 10, c + 6, 3, fill="", outline=col)
            for dx in (-6, -2, 2, 6):
                self.create_line(c + dx, c - 2, c + dx, c - 1, fill=col, width=2)
            self.create_line(c - 5, c + 2, c + 5, c + 2, fill=col, width=2, capstyle="round")
        elif self.kind == "pulse":   # a heartbeat line: the connection health panel
            self.create_line(c - 9, c, c - 4, c, c - 2, c - 7, c + 2, c + 7, c + 4, c, c + 9, c, fill=col, width=2,
                             capstyle="round", joinstyle="round")
        else:  # reload: open arc + arrow head
            self.create_arc(c - 7, c - 7, c + 7, c + 7, start=60, extent=290, style="arc", outline=col, width=2)
            self.create_polygon(c + 3, c - 10, c + 8, c - 5, c + 2, c - 3, fill=col, outline="")
        if self.badge:
            oval(self, s - 8, 8, 6, BG)
            oval(self, s - 8, 8, 4, self.badge)


class AppTile(tk.Canvas):
    """One quick-launch tile: the app's picture (or its name until the picture arrives). Click opens it on the TV,
    right-click offers to unpin it."""

    W, H = 85, 56

    def __init__(self, parent, label, command, image=None, on_menu=None, accent=False):
        super().__init__(parent, width=self.W, height=self.H, bg=BG, highlightthickness=0, cursor="hand2")
        self.label, self.command, self.image, self.on_menu, self.accent, self.hot = label, command, image, on_menu, accent, False
        self.bind("<Enter>", lambda e: self._hot(True))
        self.bind("<Leave>", lambda e: self._hot(False))
        self.bind("<Button-1>", lambda e: self.command())
        if on_menu:
            self.bind("<Button-3>", lambda e: self.on_menu(e))
        self.draw()

    def _hot(self, on):
        self.hot = on
        self.draw()

    def draw(self):
        self.delete("all")
        rounded(self, 1, 1, self.W - 1, self.H - 1, 12, fill=RAISED if self.hot else CARD,
                outline=ACCENT if self.hot else EDGE)
        if self.image is not None:
            self.create_image(self.W / 2, self.H / 2, image=self.image)
        else:
            self.create_text(self.W / 2, self.H / 2, text=self.label, fill=ACCENT_HI if self.accent else FG,
                             font=(FONT_MED, 9), width=self.W - 14, justify="center")


class Chip(tk.Canvas):
    """A small pill that is a button or a switch: "Discord: On", "Upscale: Off", "Stop casting".

    Behaves like the text label it replaces: `config(text=..., fg=ACCENT)` repaints it, and a chip whose fg is the accent
    colour is shown as on. `solid=True` makes it a filled accent pill (for the one action that matters on a strip)."""

    H, PAD = 26, 12

    def __init__(self, parent, text, command, solid=False, bg=BG):
        super().__init__(parent, height=self.H, bg=bg, highlightthickness=0, cursor="hand2")
        self.text, self.command, self.solid, self.on, self.hot, self.down, self._bg = text, command, solid, False, False, False, bg
        self.bind("<Enter>", lambda e: self._set(hot=True))
        self.bind("<Leave>", lambda e: self._set(hot=False, down=False))
        self.bind("<ButtonPress-1>", lambda e: (self._set(down=True), self.command()))
        self.bind("<ButtonRelease-1>", lambda e: self._set(down=False))
        self.draw()

    def _set(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.draw()

    def configure(self, cnf=None, **kw):
        if "text" in kw or "fg" in kw:
            self.text = kw.pop("text", self.text)
            if "fg" in kw:
                self.on = kw.pop("fg") == ACCENT
            self.draw()
        kw.pop("font", None)
        if kw or cnf:
            return super().configure(cnf, **kw)
        return None

    config = configure

    def draw(self):
        self.delete("all")
        font = (FONT_MED, 9)
        w = tkfont.Font(font=font).measure(self.text) + 2 * self.PAD
        self.tk.call(self._w, "configure", "-width", w)
        if self.solid:
            fill, fg, edge = (_shade(ACCENT, 0.85) if self.down else ACCENT_HI if self.hot else ACCENT), "#ffffff", ""
        else:
            fill = PRESSED if self.down else RAISED if self.hot else (_mix(ACCENT, CARD, 0.22) if self.on else CARD)
            fg = ACCENT_HI if self.on else FG if self.hot else MUTED
            edge = ACCENT if self.on else EDGE
        rounded(self, 0, 0, w, self.H, self.H // 2, fill=fill, outline=edge)
        self.create_text(w / 2, text_y(font, self.H / 2), text=self.text, fill=fg, font=font)


def _mix(a, b, t):
    """`a` blended toward `b` by `t` (0 = a, 1 = b), both "#rrggbb"."""
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(pa, pb))


class Tooltip:
    """A short explanation that appears under a widget after the mouse rests on it. Never takes focus."""

    DELAY = 600

    def __init__(self, widget, text):
        self.widget, self.text, self.tip, self._job = widget, text, None, None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")
        widget.bind("<ButtonPress>", self._leave, add="+")

    def _enter(self, _e):
        self._leave()
        self._job = self.widget.after(self.DELAY, self._show)

    def _leave(self, _e=None):
        if self._job:
            self.widget.after_cancel(self._job)
            self._job = None
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None

    def _show(self):
        self._job = None
        try:
            x = self.widget.winfo_rootx()
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            tip = self.tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            tip.attributes("-topmost", True)
            tk.Label(tip, text=self.text, bg=RAISED, fg=FG, font=(FONT, 9), justify="left", wraplength=280, padx=10,
                     pady=6, highlightthickness=1, highlightbackground=EDGE).pack()
            tip.wm_geometry(f"+{x}+{y}")
        except tk.TclError:
            self.tip = None


class Dot(tk.Canvas):
    """A small smooth status dot whose colour can change."""

    def __init__(self, parent, size=10, bg=BG, color=GREY):
        super().__init__(parent, width=size, height=size, bg=bg, highlightthickness=0)
        self.size, self.color = size, color
        self.draw()

    def set(self, color):
        if color != self.color:
            self.color = color
            self.draw()

    def draw(self):
        self.delete("all")
        self.create_image(0, 0, image=dot_picture(self, self.size, self.color), anchor="nw")


class ProgressBar(tk.Canvas):
    """A thin read-only bar for how far into the video the TV is. Clicking it jumps there when `seekable`."""

    def __init__(self, parent, on_seek, width=328, height=14):
        super().__init__(parent, width=width, height=height, bg=BG, highlightthickness=0)
        self.w, self.h, self.on_seek = width, height, on_seek
        self.frac, self.seekable, self.hot = 0.0, False, False
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", lambda e: self._set(hot=True))
        self.bind("<Leave>", lambda e: self._set(hot=False))
        self.draw()

    def _set(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.draw()

    def set(self, frac, seekable):
        frac = max(0.0, min(1.0, frac))
        if (frac, seekable) != (self.frac, self.seekable):
            self.frac, self.seekable = frac, seekable
            self.draw()

    def _click(self, e):
        if self.seekable:
            self.on_seek(max(0.0, min(1.0, e.x / self.w)))

    def draw(self):
        self.delete("all")
        y, thick = self.h / 2, 6 if (self.hot and self.seekable) else 4
        rounded(self, 0, y - thick / 2, self.w, y + thick / 2, thick / 2, fill=TRACK)
        if self.frac > 0:
            rounded(self, 0, y - thick / 2, max(thick, self.w * self.frac), y + thick / 2, thick / 2,
                    fill=ACCENT if self.seekable else MUTED)
        self.configure(cursor="hand2" if self.seekable else "")
