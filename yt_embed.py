"""Run real Chrome (the user's own profile: login + extensions) inside a tkinter frame.

Chrome is launched as a chrome-less --app window, then re-parented into the frame as a child window. It's still
real Chrome, so the Playlet Cast extension's auto-cast works in it. Only the window we launched is ever touched:
it's identified by diffing the window list before and after the launch.
"""
import ctypes
import os
import subprocess
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
user32.GetWindowLongW.restype = ctypes.c_long

GWL_STYLE = -16
WS_CHILD, WS_POPUP, WS_CAPTION, WS_THICKFRAME = 0x40000000, 0x80000000, 0x00C00000, 0x00040000
WS_MINIMIZEBOX, WS_MAXIMIZEBOX, WS_SYSMENU, WS_BORDER = 0x00020000, 0x00010000, 0x00080000, 0x00800000
SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x0004, 0x0010, 0x0020
RDW_INVALIDATE, RDW_UPDATENOW, RDW_ALLCHILDREN = 0x0001, 0x0100, 0x0080
SW_HIDE, SW_SHOWNA = 0, 8
WM_CLOSE = 0x0010

# Chrome paints its own title strip and keeps an invisible resize border even after the frame styles are
# removed; the child is offset by these (at 96 dpi) so the parent clips them away.
CAPTION, BORDER = 30, 7

CHROME_PATHS = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
]


def find_chrome():
    for p in CHROME_PATHS:
        p = os.path.expandvars(p)
        if os.path.exists(p):
            return p
    return None


def chrome_windows():
    """{hwnd: title} for every top-level Chrome window."""
    found = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _):
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(h, cls, 64)
        if cls.value == "Chrome_WidgetWin_1":
            n = user32.GetWindowTextLengthW(h)
            t = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, t, n + 1)
            found[h] = t.value
        return True

    user32.EnumWindows(cb, 0)
    return found


class ChromeEmbed:
    def __init__(self, log=lambda m: None):
        self.log = log
        self.hwnd = None
        self._before = None
        self._launched = 0.0

    # ---- launching -------------------------------------------------------
    def launch(self, url="https://www.youtube.com/"):
        chrome = find_chrome()
        if not chrome:
            raise RuntimeError("Google Chrome isn't installed")
        self._before = set(chrome_windows())
        self._launched = time.time()
        # Off-screen so it doesn't flash before it's pulled into the app (ignored if Chrome is already running).
        subprocess.Popen([chrome, "--window-position=-32000,-32000", f"--app={url}"])

    def waiting(self):
        return self._before is not None and self.hwnd is None

    def find_new(self):
        """The window our launch created, once it exists. None until then."""
        if self._before is None:
            return None
        for h, title in chrome_windows().items():
            if h not in self._before and user32.IsWindowVisible(h) and "YouTube" in title:
                self._before = None
                return h
        return None

    def timed_out(self, seconds=25):
        return self.waiting() and time.time() - self._launched > seconds

    # ---- embedding -------------------------------------------------------
    def embed(self, hwnd, parent_hwnd):
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        style &= ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU | WS_BORDER)
        user32.SetWindowLongW(hwnd, GWL_STYLE, style | WS_CHILD)
        user32.SetParent(hwnd, parent_hwnd)
        self.hwnd = hwnd
        self.log(f"embedded chrome window {hwnd}")

    def alive(self):
        return bool(self.hwnd) and bool(user32.IsWindow(self.hwnd))

    def fit(self, width, height):
        if not self.alive():
            return
        try:
            scale = user32.GetDpiForWindow(self.hwnd) / 96 or 1
        except Exception:
            scale = 1
        cap, border = round(CAPTION * scale), round(BORDER * scale)
        user32.SetWindowPos(self.hwnd, 0, -border, -cap, width + 2 * border, height + cap + border,
                            SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        # Without this Chrome only repaints the part of the page it had drawn before being re-parented.
        user32.RedrawWindow(self.hwnd, None, None, RDW_INVALIDATE | RDW_UPDATENOW | RDW_ALLCHILDREN)

    def show(self, visible):
        if self.alive():
            user32.ShowWindow(self.hwnd, SW_SHOWNA if visible else SW_HIDE)

    def focus(self):
        if self.alive():
            user32.SetFocus(self.hwnd)

    def release(self):
        """Hand the window back to the desktop and close it (closing the app closes its YouTube)."""
        if self.alive():
            user32.SetParent(self.hwnd, 0)
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        self.hwnd = None
