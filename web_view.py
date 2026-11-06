"""Draws the headless browser's page inside the app and sends your mouse and keyboard to it.

Brave runs with no window of its own (see brave_profile.launch_args). It streams the page as JPEG frames over
DevTools; this canvas shows the newest one and forwards clicks, scrolling and typing back. Nothing is embedded or
re-parented, so there is nothing to appear on the desktop or to render blank.
"""
import io
import time
import tkinter as tk

from PIL import Image, ImageTk

SHIFT, CTRL, ALT = 0x1, 0x4, 0x20000                    # Tk event.state bits on Windows
BTN = {1: ("left", 1), 2: ("middle", 4), 3: ("right", 2)}   # Tk button -> (CDP name, CDP buttons bit)

# Tk keysym -> (DOM key, Windows virtual-key code, text typed). Non-text keys have no text.
SPECIAL = {
    "Return": ("Enter", 13, "\r"), "KP_Enter": ("Enter", 13, "\r"), "BackSpace": ("Backspace", 8, None),
    "Tab": ("Tab", 9, None), "Escape": ("Escape", 27, None), "Delete": ("Delete", 46, None),
    "Left": ("ArrowLeft", 37, None), "Up": ("ArrowUp", 38, None), "Right": ("ArrowRight", 39, None),
    "Down": ("ArrowDown", 40, None), "Home": ("Home", 36, None), "End": ("End", 35, None),
    "Prior": ("PageUp", 33, None), "Next": ("PageDown", 34, None), "Insert": ("Insert", 45, None),
}
EDIT_COMMANDS = {"a": "selectAll", "c": "copy", "x": "cut", "z": "undo", "y": "redo"}


def fit(frame_w, frame_h, canvas_w, canvas_h):
    """Where to draw a frame so all of it shows, centred: (x offset, y offset, scale). Normally the frame is exactly the
    canvas's size (scale 1, no offset); it differs while a phone is browsing, when the page is rendered at the phone's
    size, and for a moment after the window is resized. Pure, so it's testable."""
    if frame_w <= 0 or frame_h <= 0 or (frame_w, frame_h) == (canvas_w, canvas_h):
        return 0, 0, 1.0
    scale = min(canvas_w / frame_w, canvas_h / frame_h)
    return round((canvas_w - frame_w * scale) / 2), round((canvas_h - frame_h * scale) / 2), scale


def to_page(x, y, placement):
    """A point on the canvas -> the point in the page it is over (inverse of `fit`)."""
    ox, oy, scale = placement
    return (x - ox) / scale, (y - oy) / scale


def modifiers(state):
    """Tk modifier bits -> the DevTools bitmask (Alt 1, Ctrl 2, Meta 4, Shift 8)."""
    return (1 if state & ALT else 0) | (2 if state & CTRL else 0) | (8 if state & SHIFT else 0)


def key_events(keysym, char, state):
    """One Tk key press -> a list of ('key', cdp_event) / ('paste', None) for the page. Pure, so it's testable."""
    mods = modifiers(state)
    ctrl = bool(state & CTRL) and not (state & ALT)       # AltGr arrives as Ctrl+Alt: that's a character
    if keysym in SPECIAL:
        key, vk, text = SPECIAL[keysym]
        down = {"type": "keyDown" if text else "rawKeyDown", "key": key, "code": key, "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk, "modifiers": mods}
        if text:
            down["text"] = text
        up = {"type": "keyUp", "key": key, "code": key, "windowsVirtualKeyCode": vk, "modifiers": mods}
        return [("key", down), ("key", up)]
    if ctrl and len(keysym) == 1 and keysym.lower() in "acxzyv":
        k = keysym.lower()
        if k == "v":
            return [("paste", None)]                       # the page can't read our clipboard; we type it in
        vk = ord(k.upper())
        return [("key", {"type": "rawKeyDown", "key": k, "windowsVirtualKeyCode": vk, "modifiers": mods,
                         "commands": [EDIT_COMMANDS[k]]}),
                ("key", {"type": "keyUp", "key": k, "windowsVirtualKeyCode": vk, "modifiers": mods})]
    if char and char.isprintable() and not ctrl:
        vk = ord(char.upper()) if char.isascii() and char.isalnum() else 0
        return [("key", {"type": "keyDown", "key": char, "text": char, "unmodifiedText": char,
                         "windowsVirtualKeyCode": vk, "modifiers": mods}),
                ("key", {"type": "keyUp", "key": char, "windowsVirtualKeyCode": vk, "modifiers": mods})]
    return []


class PageView(tk.Canvas):
    def __init__(self, parent, get_guard, bg, on_resize):
        super().__init__(parent, bg=bg, highlightthickness=0, takefocus=1, cursor="arrow")
        self.get_guard, self.on_resize = get_guard, on_resize
        self.seq, self.has_frame, self._photo, self._job = 0, False, None, None
        self.placement, self._frame_size = (0, 0, 1.0), (0, 0)          # see fit()
        self._last_click, self._clicks, self._last_move = (0.0, 0, 0), 0, 0.0
        self._image = self.create_image(0, 0, anchor="nw")
        self.bind("<Configure>", self._on_size)
        for n in (1, 2, 3):
            self.bind(f"<ButtonPress-{n}>", lambda e, n=n: self._press(e, n))
            self.bind(f"<ButtonRelease-{n}>", lambda e, n=n: self._release(e, n))
        self.bind("<Motion>", self._move)
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<KeyPress>", self._key)
        self.after(33, self._paint)

    # ---- drawing -----------------------------------------------------------------
    def _paint(self):
        try:
            g = self.get_guard()
            if g.running:
                seq, data = g.frame()
                if data and seq != self.seq:
                    self.seq = seq
                    img = Image.open(io.BytesIO(data))
                    self._frame_size = img.size
                    self._place(img)
                    self.has_frame = True
        except Exception:
            pass                                          # a bad frame is skipped; the next one repaints
        self.after(33, self._paint)

    def _place(self, img=None):
        """Draw the newest frame, scaled to fit and centred if it isn't the canvas's own size."""
        w, h = self.winfo_width(), self.winfo_height()
        fw, fh = self._frame_size
        self.placement = fit(fw, fh, w, h)
        ox, oy, scale = self.placement
        if img is not None:
            if scale != 1.0:
                img = img.resize((max(1, round(fw * scale)), max(1, round(fh * scale))), Image.BILINEAR)
            self._photo = ImageTk.PhotoImage(img)
            self.itemconfigure(self._image, image=self._photo)
        self.coords(self._image, ox, oy)

    def _pt(self, e):
        return to_page(e.x, e.y, self.placement)

    def reset(self):
        """Forget the old picture (the browser was restarted)."""
        self.seq, self.has_frame, self.placement, self._frame_size = 0, False, (0, 0, 1.0), (0, 0)
        self.itemconfigure(self._image, image="")
        self._photo = None

    def _on_size(self, e):          # (not "_configure": tkinter's Canvas already uses that name internally)
        self._place()
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(150, lambda: self.on_resize(e.width, e.height))   # not on every pixel of a drag

    # ---- mouse -------------------------------------------------------------------
    def _mods(self, e):
        return modifiers(e.state)

    def _held(self, e):
        return (1 if e.state & 0x100 else 0) | (4 if e.state & 0x200 else 0) | (2 if e.state & 0x400 else 0)

    def _press(self, e, n):
        self.focus_set()
        now = time.monotonic()
        t, c, x0 = self._last_click
        near = abs(e.x - x0) < 5 and now - t < 0.4
        self._clicks = min(3, self._clicks + 1) if near else 1
        self._last_click = (now, self._clicks, e.x)
        name, bit = BTN[n]
        x, y = self._pt(e)
        self.get_guard().mouse("mousePressed", x, y, name, self._held(e) | bit, self._clicks, mods=self._mods(e))

    def _release(self, e, n):
        name, bit = BTN[n]
        x, y = self._pt(e)
        self.get_guard().mouse("mouseReleased", x, y, name, self._held(e) & ~bit, self._clicks, mods=self._mods(e))

    def _move(self, e):
        now = time.monotonic()
        if now - self._last_move < 0.025:                 # ~40 moves a second is plenty
            return
        self._last_move = now
        x, y = self._pt(e)
        self.get_guard().mouse("mouseMoved", x, y, buttons=self._held(e), mods=self._mods(e))

    def _wheel(self, e):
        horizontal = bool(e.state & SHIFT)
        step = -e.delta                                   # Tk: up is positive; the page wants down positive
        x, y = self._pt(e)
        self.get_guard().mouse("mouseWheel", x, y, dx=step if horizontal else 0, dy=0 if horizontal else step,
                               mods=self._mods(e))

    # ---- keyboard ----------------------------------------------------------------
    def _key(self, e):
        g = self.get_guard()
        for kind, ev in key_events(e.keysym, e.char, e.state):
            if kind == "paste":
                try:
                    g.text(self.clipboard_get())
                except tk.TclError:
                    pass
            else:
                g.key(ev)
        return "break"
