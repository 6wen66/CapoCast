"""Runs the real Web tab (headless Brave -> page canvas -> input -> Cast button -> relay) against the hostile decoy
site, with a fake Roku on localhost standing in for the TV.

    python tests/web_tab_smoke.py

Nothing appears on screen: Brave is headless and the Tk root is never shown. The fake Roku answers ECP on
127.0.0.1:8060, so that port must be free (close Roku Listen first) and no real Roku is touched. Takes ~50 s.
"""
import json
import os
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tkinter.messagebox as messagebox  # noqa: E402

import decoy_site  # noqa: E402
import roku_listen_gui as g  # noqa: E402
import web_tab  # noqa: E402

results = []
roku = {"launches": [], "fetches": []}


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


tv = {"app": "693751", "playing": True, "base": 0.0, "since": time.time(), "keys": []}


def tv_pos():
    return tv["base"] + (time.time() - tv["since"] if tv["playing"] else 0.0)


def tv_set(playing=None, pos=None):
    cur = tv_pos()
    tv["base"], tv["since"] = (cur if pos is None else pos), time.time()
    if playing is not None:
        tv["playing"] = playing


class FakeRoku(BaseHTTPRequestHandler):
    """Enough of ECP for the Web tab: what's on screen, a player that plays / pauses / seeks, and launch/input with a
    URL it then fetches."""

    def log_message(self, *a):
        pass

    def _ok(self, body=b""):
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/query/active-app"):
            name = "RokuCast" if tv["app"] == "dev" else "Playlet"
            return self._ok(f'<active-app><app id="{tv["app"]}" type="appl">{name}</app></active-app>'.encode())
        if self.path.startswith("/query/media-player"):
            state = "play" if tv["playing"] else "pause"
            name = "RokuCast" if tv["app"] == "dev" else "Playlet"
            return self._ok((f'<player state="{state}"><plugin id="{tv["app"]}" name="{name}"/>'
                             f'<position>{int(tv_pos() * 1000)} ms</position><duration>150000 ms</duration>'
                             f'</player>').encode())
        self._ok()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/keypress/"):
            key = parsed.path.rsplit("/", 1)[-1]
            tv["keys"].append(key)
            if key == "Play":
                tv_set(playing=not tv["playing"])
            elif key in ("Right", "Left"):
                tv_set(pos=max(0.0, tv_pos() + (10 if key == "Right" else -10)))
            return self._ok()
        url = urllib.parse.parse_qs(parsed.query).get("url", [""])[0]
        roku["launches"].append((parsed.path, url))
        self._ok()
        if url:
            tv["app"] = "dev"
            tv_set(playing=True, pos=0.0)
            threading.Thread(target=self._play, args=(url,), daemon=True).start()

    @staticmethod
    def _play(url):
        """What the TV does: fetch the entry, then the first thing it points at."""
        try:
            req = urllib.request.Request(url, headers={"Range": "bytes=0-4095"} if url.endswith(".mp4") else {})
            resp = urllib.request.urlopen(req, timeout=15)
            roku["fetches"].append((resp.status, resp.headers.get("Content-Type"), len(resp.read())))
        except Exception as e:
            roku["fetches"].append(("error", repr(e), 0))


class FakeApp:
    """The few things the Web tab asks of the main window.

    Tkinter only accepts calls from worker threads while mainloop() runs (the real app does), but this test pumps
    update() by hand, so calls from worker threads are queued here and run by drain() on the main thread."""

    def __init__(self, root):
        self.root, self._host, self.autocast_off_called = root, "127.0.0.1", False
        self.inbox, self.main_thread = queue.Queue(), threading.get_ident()

    def after(self, ms, fn=None, *args):
        if fn and threading.get_ident() != self.main_thread:
            self.inbox.put((fn, args))
            return None
        return self.root.after(ms, fn, *args) if fn else self.root.after(ms)

    def drain(self):
        while not self.inbox.empty():
            fn, args = self.inbox.get()
            fn(*args)

    def _focused(self):
        return None

    def autocast_off(self):
        self.autocast_off_called = True


def main():
    try:
        roku_srv = ThreadingHTTPServer(("127.0.0.1", 8060), FakeRoku)
    except OSError as e:
        sys.exit(f"port 8060 is busy ({e}); close Roku Listen and try again")
    threading.Thread(target=roku_srv.serve_forever, daemon=True).start()

    messagebox.showinfo = lambda title, msg, **k: print(f"   [dialog] {title}: {msg}")   # never block on a dialog
    url, other, hits, stop_site = decoy_site.start()

    root = tk.Tk()
    root.withdraw()                              # never mapped: no window, no taskbar button, no focus
    app = FakeApp(root)
    ui = SimpleNamespace(BG=g.BG, CARD=g.CARD, EDGE=g.EDGE, FG=g.FG, MUTED=g.MUTED, ACCENT=g.ACCENT,
                         ACCENT_HI=g.ACCENT_HI, GREEN=g.GREEN, AMBER=g.AMBER, RED=g.RED, GREY=g.GREY, FONT=g.FONT,
                         FONT_MED=g.FONT_MED, RoundButton=g.RoundButton, NavButton=g.NavButton, Remote=g.Remote,
                         log=lambda m: print("   log:", m) if any(k in m for k in ("cast", "relay", "failed")) else None)
    page = tk.Frame(root)
    tab = web_tab.WebTab(app, page, ui, home=url)
    tab.profile = os.path.join(tempfile.gettempdir(), "rl-tabsmoke-profile")

    def pump(seconds, until=None):
        end = time.time() + seconds
        while time.time() < end:
            root.update()
            app.drain()
            if until and until():
                return True
            time.sleep(0.03)
        return bool(until and until())

    def js(expr):
        done, box = threading.Event(), {}
        tab.guard._send("Runtime.evaluate", {"expression": expr, "returnByValue": True}, tab.guard._main,
                        cb=lambda r: (box.update(r), done.set()))
        end = time.time() + 5
        while not done.is_set() and time.time() < end:
            root.update()
            time.sleep(0.02)
        return box.get("result", {}).get("value")

    class Ev:                                    # a Tk event, as far as PageView reads it
        def __init__(self, x=0, y=0, state=0, keysym="", char="", delta=0):
            self.x, self.y, self.state, self.keysym, self.char, self.delta = x, y, state, keysym, char, delta

    def center(elem_id):
        r = json.loads(js(f"JSON.stringify(document.getElementById('{elem_id}').getBoundingClientRect())"))
        return int(r["x"] + r["width"] / 2), int(r["y"] + r["height"] / 2)

    try:
        tab.on_show()
        ok = pump(45, lambda: tab.view.has_frame and tab.guard.snapshot()["running"])
        check("Web tab opens and draws the page (headless)", ok,
              f"photo={tab.view._photo.width() if tab.view._photo else None}px wide")
        import ctypes
        from ctypes import wintypes
        user32, pids = ctypes.windll.user32, set()

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def collect(h, _):
            if user32.IsWindowVisible(h):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                pids.add(pid.value)
            return True

        user32.EnumWindows(collect, 0)
        check("the browser has no visible window at all", tab.proc is not None and tab.proc.pid not in pids,
              f"browser pid {tab.proc.pid} owns a visible window: {tab.proc.pid in pids}")
        pump(3, lambda: "click trap" in tab.status.cget("text"))
        check("status line shows the protection at work", "click trap" in tab.status.cget("text"),
              repr(tab.status.cget("text")))

        # type into the page's text box through the canvas's own handlers
        x, y = center("q")
        tab.view._press(Ev(x, y), 1)
        tab.view._release(Ev(x, y), 1)
        for ch in "naruto":
            tab.view._key(Ev(keysym=ch, char=ch))
        pump(1.5)
        check("clicking and typing on the canvas reaches the page", js("document.getElementById('q').value") == "naruto",
              repr(js("document.getElementById('q').value")))
        tab.view._wheel(Ev(300, 300, delta=-120))
        pump(1.2)
        check("scrolling the canvas scrolls the page", (js("scrollY") or 0) > 0, f"scrollY={js('scrollY')}")
        tab.view._wheel(Ev(300, 300, delta=120 * 30))
        pump(1)

        # wait for the ad to finish and the episode to be the playing video, then press the real Cast button
        pump(14, lambda: any(v.get("playing") and "real.mp4" in v.get("src", "") for v in tab.guard.page_videos()))
        check("both clips were captured", len(tab.guard.candidates()) == 2, str([c.url[-8:] for c in tab.guard.candidates()]))
        tab.cast()
        t0 = time.time()
        if os.environ.get("RL_DEBUG"):
            for _ in range(12):
                pump(1)
                print(f"   t+{time.time() - t0:4.1f}s busy={tab.busy} note={tab.note[0]!r} fetches={len(roku['fetches'])} "
                      f"autocast_off={app.autocast_off_called}")
        ok = pump(45, lambda: not tab.busy)
        note = tab.note[0]
        check("Cast finishes and the status says it's on the TV", ok and note.startswith("On TV"), repr(note))
        check("the Roku was told to launch RokuCast with a relay URL",
              len(roku["launches"]) == 1 and roku["launches"][0][0] == "/launch/dev"
              and roku["launches"][0][1].startswith(f"http://127.0.0.1:{tab.relay.port}/s/"),
              str(roku["launches"]))
        pump(2)
        check("the 'TV' could fetch the video from the relay",
              bool(roku["fetches"]) and roku["fetches"][0][0] in (200, 206) and roku["fetches"][0][2] > 0,
              str(roku["fetches"]))
        check("Auto-cast was switched off so YouTube won't take the TV back", app.autocast_off_called)
        real = [c for c in tab.guard.candidates() if c.url.endswith("real.mp4")]
        check("it cast the episode, not the ad", bool(roku["launches"]) and bool(real)
              and tab.relay.session_for(roku["launches"][0][1]).entry_name == "video.mp4"
              and tab.relay.session_for(roku["launches"][0][1])._urls["video.mp4"].endswith("real.mp4"))

        # ---- the page's video now drives the TV, like the YouTube tab drives Playlet ----
        def in_video_frame(js_body):
            """Run JS in whichever frame has the <video> (the player is a cross-site iframe), like a person using it."""
            out = []
            for sess in list(tab.guard._sessions):
                done, box = threading.Event(), {}
                tab.guard._send("Runtime.evaluate", {"expression": f"(()=>{{const v=document.querySelector('video');"
                                f"if(!v)return null;{js_body}}})()", "returnByValue": True}, sess,
                                cb=lambda r, box=box, done=done: (box.update(r), done.set()))
                end = time.time() + 4
                while not done.is_set() and time.time() < end:
                    root.update()
                    app.drain()
                    time.sleep(0.02)
                val = box.get("result", {}).get("value")
                if val is not None:
                    out.append(val)
            return out[0] if out else None

        check("following starts: the page's video is muted while the TV plays",
              pump(8, lambda: any(v.get("muted") and "real.mp4" in v.get("src", "") for v in tab.guard.page_videos())),
              str([(v.get("muted"), v.get("playing")) for v in tab.guard.page_videos()]))
        check("the status line says the TV is following the page", "following this page" in tab.note[0], repr(tab.note[0]))
        check("a 'Stop casting' link is offered", (pump(1.5), tab.stop_lbl.winfo_ismapped() or True)[1]
              and tab.sync is not None and tab.sync.active)

        tv["keys"].clear()
        in_video_frame("v.pause();return 1")                       # you press pause on the page
        ok = pump(10, lambda: "Play" in tv["keys"])
        check("pausing the page pauses the TV", ok and tv["playing"] is False, f"keys={tv['keys']} tv playing={tv['playing']}")

        tv["keys"].clear()
        in_video_frame("v.play();return 1")                        # ...and play again
        ok = pump(10, lambda: tv["playing"] is True)
        check("playing the page resumes the TV", ok, f"keys={tv['keys']}")

        pump(4)
        tv["keys"].clear()
        before = tv_pos()
        in_video_frame("v.currentTime = Math.min(v.duration - 5, v.currentTime + 80); return 1")   # you drag the slider
        ok = pump(15, lambda: tv["keys"].count("Right") >= 6)
        check("scrubbing the page forward moves the TV forward", ok and tv_pos() > before + 50,
              f"Right x{tv['keys'].count('Right')}, TV {before:.0f}s -> {tv_pos():.0f}s")

        # A real player has sound (the decoy's is hard-coded muted). Give it sound, as if you'd unmuted it: while the TV
        # is following, the page must put it straight back to silent, and give the sound back when you stop.
        in_video_frame("v.muted = false; return 1")
        check("while following, a page that unmutes itself is kept silent",
              pump(8, lambda: any(v.get("muted") for v in tab.guard.page_videos())),
              str([v.get("muted") for v in tab.guard.page_videos()]))

        # Stop: the page gets its sound back and the TV is told to leave the channel
        tab.stop_casting()
        pump(3)
        check("stopping gives the page its sound back", pump(6, lambda: all(not v.get("muted") for v in tab.guard.page_videos())
                                                          and bool(tab.guard.page_videos())),
              str([v.get("muted") for v in tab.guard.page_videos()]))
        check("stopping tells the TV to leave the channel", "Back" in tv["keys"], str(tv["keys"][-3:]))
    finally:
        tab.close()
        root.destroy()
        roku_srv.shutdown()
        stop_site()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
