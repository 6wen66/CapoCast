"""End-to-end check of the Web tab's protection and input path against the hostile decoy site, using real Brave.

    python tests/guard_smoke.py

Runs fully headless: no window opens and nothing takes focus. Not part of `unittest discover` (it needs Brave and
ffmpeg and takes ~45 s). Prints PASS/FAIL per check.
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brave_profile  # noqa: E402
import decoy_site  # noqa: E402
from PIL import Image  # noqa: E402
from stream_finder import apply_page_videos, choose  # noqa: E402
from web_guard import WebGuard  # noqa: E402
from web_view import key_events  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def wait_for(fn, seconds=15, step=0.25):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


def main():
    brave = brave_profile.find_brave()
    if not brave:
        sys.exit("Brave isn't installed (winget install Brave.Brave)")
    profile = os.path.join(tempfile.gettempdir(), "rl-smoke-profile")
    shutil.rmtree(profile, ignore_errors=True)
    check("filter lists seeded into the profile", brave_profile.seed_filters(profile, print))

    url, other, hits, stop_site = decoy_site.start()
    pf = brave_profile.port_file(profile)
    guard = WebGuard(lambda m: print("   log:", m), home=url)
    guard.set_view(900, 600, True)
    proc = subprocess.Popen([brave, *brave_profile.launch_args(profile, url)],
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        if not wait_for(lambda: os.path.exists(pf), 25):
            sys.exit("Brave never wrote its DevTools port")
        guard.attach(pf)
        if not wait_for(lambda: guard._main and guard._main_frame, 15):
            sys.exit("guard never attached to the page")
        main_session = guard._main

        def call(method, params=None):
            done, box = threading.Event(), {}
            guard._send(method, params or {}, main_session, cb=lambda r: (box.update(r), done.set()))
            done.wait(5)
            return box

        def eval_js(expr):
            return call("Runtime.evaluate", {"expression": expr, "returnByValue": True}).get("result", {}).get("value")

        def click(x, y):
            guard.mouse("mouseMoved", x, y)
            time.sleep(0.15)
            guard.mouse("mousePressed", x, y, "left", 1, 1)
            guard.mouse("mouseReleased", x, y, "left", 0, 1)
            time.sleep(1.5)

        def center(elem_id):
            r = json.loads(eval_js(f"JSON.stringify(document.getElementById('{elem_id}').getBoundingClientRect())"))
            return r["x"] + r["width"] / 2, r["y"] + r["height"] / 2

        # 0. the headless browser's picture reaches us, at the size we asked for
        wait_for(lambda: guard.frame()[1], 10)
        seq, data = guard.frame()
        img = Image.open(io.BytesIO(data)) if data else None
        colours = len(img.resize((64, 64)).getcolors(4096)) if img else 0
        check("page is streamed as frames and drawn", colours > 15, f"frame #{seq} {img.size if img else None}, {colours} colours")
        check("page sees the requested viewport", eval_js("innerWidth + 'x' + innerHeight") == "900x600")
        check("not flagged as automation", eval_js("navigator.webdriver") is False)

        # 1. the click trap is removed before it can steal a click
        wait_for(lambda: guard.snapshot()["stats"]["overlays"] >= 1, 8)
        check("click-trap overlay removed", guard.snapshot()["stats"]["overlays"] >= 1, str(guard.snapshot()["stats"]))
        check("trap element is gone from the page", eval_js("document.getElementById('trap') === null") is True)

        # 2. a cross-site 'download' link opens a pop-up tab -> closed
        before = guard.snapshot()["stats"]["popups"]
        click(*center("fake"))
        check("cross-site pop-up blocked", guard.snapshot()["stats"]["popups"] == before + 1, str(guard.snapshot()["stats"]))
        check("still on the decoy page", guard.snapshot()["url"].startswith(url), guard.snapshot()["url"])

        # 3. typing, translated from Tk key presses the way the app does it, reaches a text box
        click(*center("q"))
        for ch in "hello":
            for kind, ev in key_events(ch, ch, 0):
                guard.key(ev)
        time.sleep(0.8)
        check("typed text reaches the page", eval_js("document.getElementById('q').value") == "hello",
              repr(eval_js("document.getElementById('q').value")))
        for kind, ev in key_events("BackSpace", "\x08", 0):
            guard.key(ev)
        time.sleep(0.5)
        check("Backspace works", eval_js("document.getElementById('q').value") == "hell")
        guard.text(" world")                    # what Ctrl+V sends
        time.sleep(0.5)
        check("pasted text arrives", eval_js("document.getElementById('q').value") == "hell world")

        # 4. mouse-wheel scrolling
        guard.mouse("mouseWheel", 450, 300, dy=400)
        time.sleep(1.0)
        check("mouse wheel scrolls the page", (eval_js("scrollY") or 0) > 100, f"scrollY={eval_js('scrollY')}")
        guard.mouse("mouseWheel", 450, 300, dy=-2000)
        time.sleep(0.8)

        # 5. a spam alert() must not freeze the page
        click(*center("alertbtn"))
        ok = wait_for(lambda: eval_js("document.title") == "alerted", 5)
        check("alert() is dismissed instead of freezing the page", bool(ok), repr(eval_js("document.title")))

        # 6. a normal same-site link that wants a new tab opens in the main view instead
        click(*center("real"))
        ok = wait_for(lambda: guard.snapshot()["url"].endswith("/page2"), 6)
        check("same-site new-tab link opened in the main view", bool(ok), guard.snapshot()["url"])

        # 7. back on the decoy: the ad plays first, then the episode; the filter must pick the episode
        guard.navigate(url)
        wait_for(lambda: guard.snapshot()["url"] == url, 8)
        time.sleep(11)
        cands = guard.candidates()
        names = sorted(c.url.rsplit("/", 1)[-1] for c in cands)
        check("both clips were seen", names == ["ad.mp4", "real.mp4"], f"{names}; videos={guard.page_videos()}")
        apply_page_videos(cands, guard.page_videos())
        d = choose(cands, guard.active_frames())
        best = d.best.url.rsplit("/", 1)[-1] if d.best else None
        check("the episode wins, not the ad", best == "real.mp4", f"best={best} rejected={d.rejected}")
        check("page video duration was learned", bool(d.best and 140 < d.best.duration < 160),
              str(d.best.duration if d.best else None))

        # 7a. subtitles: the file the player fetched, and the <track> element that names it
        files, dom = guard.subtitle_sources()
        check("the subtitle file the player fetched is seen", any(f["url"].endswith("/en.vtt") for f in files), repr(files))
        check("the video's <track> element is reported", any(t.get("label") == "English" for t in dom), repr(dom))
        tracks = __import__("subtitles").build_tracks(files, dom)
        check("they make one named track", [t["label"] for t in tracks] == ["English"], repr(tracks))

        # 7b. the thumbnail for Discord: the page's own preview image; none on a page that has neither
        check("page_image finds the page's og:image", guard.page_image(3.0) == "https://thumbs.example/decoy-ep1.jpg",
              repr(guard.page_image(3.0)))
        info = guard.page_info(3.0)
        check("page_info reads the live title and address", info.get("title", "").startswith("Decoy:")
              and info.get("url", "").startswith("http://127.0.0.1"), repr(info))
        guard.navigate(url + "page2" if url.endswith("/") else url + "/page2")
        wait_for(lambda: guard.snapshot()["url"].endswith("/page2"), 8)
        time.sleep(1.0)
        check("page_image is empty when the page has no picture", guard.page_image(3.0) == "", repr(guard.page_image(3.0)))
        guard.navigate(url)
        wait_for(lambda: guard.snapshot()["url"] == url, 8)
        time.sleep(1.0)

        # 8. a script redirect with no click is stopped
        guard.navigate(url + "?bounce=1")
        time.sleep(6)
        snap = guard.snapshot()
        check("scripted redirect blocked", snap["stats"]["redirects"] >= 1 and "/ad" not in snap["url"],
              f"{snap['url']} {snap['stats']}")
    finally:
        guard.close_browser()
        time.sleep(1.0)
        guard.stop()
        try:
            proc.wait(4)
        except Exception:
            proc.terminate()
        stop_site()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
