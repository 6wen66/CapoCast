"""End-to-end check of browsing from the phone, using real headless Brave (no window opens, nothing takes focus).

    python tests/phone_browse_smoke.py

Serves a tiny page, attaches the real WebGuard, puts the real WebTab phone methods behind the real phone server, and
then does what a phone would over HTTP: ask for a frame at its size, tap a link, scroll, type into a field, go back.
Not part of `unittest discover` (needs Brave). Prints PASS/FAIL per check.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import brave_profile  # noqa: E402
import phone_remote  # noqa: E402
import web_tab  # noqa: E402
from PIL import Image  # noqa: E402
from web_guard import WebGuard  # noqa: E402

PAGE = b"""<!doctype html><meta name=viewport content="width=device-width">
<body style="margin:0;font:20px sans-serif"><a id=l href="/next" style="display:block;margin:20px;padding:30px;background:#0a0;color:#fff">go next</a>
<input id=i style="margin:20px;padding:16px;width:280px;font-size:20px"><video id=v src="/v.mp4" muted autoplay loop width=300></video><div style="height:4000px;background:linear-gradient(#fff,#888)">tall</div>
<script>addEventListener("scroll",()=>document.title="scrolled "+Math.round(scrollY))</script>"""
NEXT = b"<!doctype html><title>next page</title><h1>next page</h1>"

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def wait_for(fn, seconds=15, step=0.2):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


VIDEO = {"data": b""}


class Site(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/v.mp4"):                      # with Range support, so the browser can seek
            data, rng = VIDEO["data"], self.headers.get("Range")
            start, end = 0, len(data) - 1
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                start = int(a or 0)
                end = int(b) if b else end
            chunk = data[start:end + 1]
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(chunk)))
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            self.end_headers()
            try:
                self.wfile.write(chunk)
            except OSError:
                pass
            return
        body = NEXT if self.path.startswith("/next") else PAGE
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    brave = brave_profile.find_brave()
    if not brave:
        sys.exit("Brave isn't installed (winget install Brave.Brave)")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        sys.exit("ffmpeg isn't on PATH")
    clip = os.path.join(tempfile.gettempdir(), "rl-phone-smoke.mp4")
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=60:size=320x180:rate=10",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "35", "-movflags", "+faststart",
                    clip], check=True)
    VIDEO["data"] = open(clip, "rb").read()
    class Quiet(ThreadingHTTPServer):
        def handle_error(self, *a):
            pass                                              # the browser drops connections while seeking

    site = Quiet(("127.0.0.1", 0), Site)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{site.server_address[1]}/"
    profile = os.path.join(tempfile.gettempdir(), "rl-phone-smoke-profile")
    shutil.rmtree(profile, ignore_errors=True)

    guard = WebGuard(lambda m: None, home=url)
    guard.set_view(1280, 700, False)                       # the app's tab is hidden, like when you're on the phone
    guard.PHONE_IDLE = 2.0
    proc = subprocess.Popen([brave, *brave_profile.launch_args(profile, url)],
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    srv = None
    try:
        pf = brave_profile.port_file(profile)
        if not wait_for(lambda: os.path.exists(pf), 25):
            sys.exit("Brave never wrote its DevTools port")
        guard.attach(pf)
        if not wait_for(lambda: guard._main and guard._main_frame, 15):
            sys.exit("guard never attached to the page")
        session = guard._main

        def eval_js(expr):
            done, box = threading.Event(), {}
            guard._send("Runtime.evaluate", {"expression": expr, "returnByValue": True}, session,
                        cb=lambda r: (box.update(r), done.set()))
            done.wait(5)
            return box.get("result", {}).get("value")

        # The real tab methods, bound to the real guard
        tab = web_tab.WebTab.__new__(web_tab.WebTab)
        tab.guard, tab.home, tab.launched_at, tab.busy, tab.link, tab.sync = guard, url, time.time(), False, None, None
        tab.note, tab.note_until = ("", None), 0.0
        tab.ui = SimpleNamespace(GREEN="g", log=lambda m: None)
        tab.alive = lambda: True
        tab._post = lambda fn: fn()

        class Backend:
            def web_frame(self, after, w, h): return tab.phone_frame(after, w, h, timeout=4)
            def web_info(self): return tab.phone_info()
            def web_tap(self, x, y): tab.phone_tap(x, y)
            def web_scroll(self, x, y, dx, dy): tab.phone_scroll(x, y, dx, dy)
            def web_text(self, t): tab.phone_text(t)
            def web_key(self, k): tab.phone_key(k)
            def web_nav(self, a, u): tab.phone_nav(a, u)
            def web_cast(self): return tab.phone_cast()
            def web_transport(self, a, v): tab.phone_transport(a, v)

        srv = phone_remote.PhoneRemote(Backend(), "tok", b"", port=0, bind="127.0.0.1")
        srv.start()
        base = f"http://127.0.0.1:{srv.port}"

        def call(path, body=None):
            req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                         headers={"X-Token": "tok", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read()

        # 1. a frame at the phone's size
        code, headers, jpeg = call("/api/web/frame?after=0&w=390&h=700")
        img = Image.open(io.BytesIO(jpeg))
        check("frame arrives at the phone's size", code == 200 and img.size == (390, 700), str(img.size))
        check("frame headers say what the page is rendered at", (headers["X-View-W"], headers["X-View-H"]) == ("390", "700"))
        check("the page itself is laid out 390 CSS px wide", eval_js("innerWidth") == 390, str(eval_js("innerWidth")))
        seq = int(headers["X-Seq"])

        # 2. tap into the field and type
        field = eval_js("(()=>{const r=document.getElementById('i').getBoundingClientRect();return [r.x+30,r.y+20]})()")
        call("/api/web/input", {"type": "tap", "x": field[0], "y": field[1]})
        check("a tap focuses the field under it", wait_for(lambda: eval_js("document.activeElement.id") == "i"))
        call("/api/web/input", {"type": "text", "text": "hello phone"})
        check("typed text lands in the field", wait_for(lambda: eval_js("document.getElementById('i').value") == "hello phone"),
              str(eval_js("document.getElementById('i').value")))
        call("/api/web/input", {"type": "key", "name": "Backspace"})
        check("backspace deletes a character", wait_for(lambda: eval_js("document.getElementById('i').value") == "hello phon"))

        # 3. scroll
        call("/api/web/input", {"type": "scroll", "x": 100, "y": 300, "dx": 0, "dy": 250})
        y = wait_for(lambda: eval_js("Math.round(scrollY)"))
        check("a drag scrolls the page", y and 100 <= y <= 400, str(y))

        # 3b. play / pause / skip act on the page's video, and the page reports them (that is what WebSync follows)
        tab.sync = None
        eval_js("scrollTo(0,0)")
        playing = lambda: eval_js("!document.getElementById('v').paused")
        check("the test video is playing to begin with", wait_for(playing))
        while not guard.page_events.empty():
            guard.page_events.get()
        call("/api/web/transport", {"action": "toggle", "value": 0})
        check("phone pause pauses the page's video", wait_for(lambda: not playing()))
        kinds = []
        wait_for(lambda: (kinds.extend(guard.page_events.get()["kind"] for _ in range(guard.page_events.qsize())), "pause" in kinds)[1])
        check("...and the page reports it as a pause event for the TV to follow", "pause" in kinds, str(kinds))
        call("/api/web/transport", {"action": "toggle", "value": 0})
        check("phone play plays it again", wait_for(playing))
        t0 = eval_js("document.getElementById('v').currentTime")
        call("/api/web/transport", {"action": "skip", "value": 10})
        t1 = wait_for(lambda: (lambda t: t if t >= t0 + 9 else None)(eval_js("document.getElementById('v').currentTime")))
        check("phone skip +10s jumps the video", t1, f"{t0:.1f} -> {t1 or 0:.1f}")
        kinds = []
        wait_for(lambda: (kinds.extend(guard.page_events.get()["kind"] for _ in range(guard.page_events.qsize())), "seeked" in kinds)[1])
        check("...and reports it as a seek", "seeked" in kinds, str(kinds))
        call("/api/web/transport", {"action": "skip", "value": -10})
        check("phone skip -10s goes back", wait_for(lambda: eval_js("document.getElementById('v').currentTime") < t0 + 5))

        # 4. tap the link: the page navigates
        eval_js("scrollTo(0,0)")
        time.sleep(0.4)
        link = eval_js("(()=>{const r=document.getElementById('l').getBoundingClientRect();return [r.x+40,r.y+20]})()")
        call("/api/web/input", {"type": "tap", "x": link[0], "y": link[1]})
        check("tapping a link follows it", wait_for(lambda: guard.snapshot()["url"].endswith("/next")), guard.snapshot()["url"])
        info = json.loads(call("/api/web/info")[2])
        check("info reports the new address", info["url"].endswith("/next") and info["alive"], info["url"])

        # 5. back, and the address box
        call("/api/web/nav", {"action": "back"})
        check("back goes back", wait_for(lambda: not guard.snapshot()["url"].endswith("/next")))
        call("/api/web/nav", {"action": "go", "url": url + "next?x=1"})
        check("an address typed on the phone loads", wait_for(lambda: guard.snapshot()["url"].endswith("/next?x=1")))

        # 6. a newer frame follows the change
        got = call(f"/api/web/frame?after={seq}&w=390&h=700")
        check("a newer frame arrives after the page changes", got[0] == 200 and int(got[1]["X-Seq"]) > seq)

        # 7. let go: the browser returns to the app's own size
        check("while the phone watches, the guard knows", guard.phone_active)
        time.sleep(guard.PHONE_IDLE + 0.5)
        guard.phone_expire()
        check("after the phone goes quiet the view is handed back",
              wait_for(lambda: eval_js("innerWidth") == 1280), str(eval_js("innerWidth")))
    finally:
        if srv:
            srv.stop()
        guard.stop()
        proc.terminate()
        site.shutdown()
        time.sleep(0.5)
        shutil.rmtree(profile, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
