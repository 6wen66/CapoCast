"""The phone's YouTube tab against the real youtube.com, in real headless Brave (no window opens, nothing takes focus).

    python tests/phone_youtube_smoke.py

Uses a throwaway browser profile (so nobody is signed in) and needs internet. Checks that the YouTube switch opens the
page drawn black, that a tap on a video is handed to the TV instead of opening it, and that the switch turns off.
Not part of `unittest discover`. Prints PASS/FAIL per check.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import brave_profile  # noqa: E402
from web_guard import WebGuard  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def wait_for(fn, seconds=20, step=0.2):
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
    profile = os.path.join(tempfile.gettempdir(), "rl-yt-smoke-profile")
    shutil.rmtree(profile, ignore_errors=True)
    tapped = []
    guard = WebGuard(lambda m: None, home="about:blank")
    guard.set_view(1280, 700, False)
    guard.on_youtube = tapped.append
    proc = subprocess.Popen([brave, *brave_profile.launch_args(profile, "about:blank")],
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        pf = brave_profile.port_file(profile)
        if not wait_for(lambda: os.path.exists(pf), 25):
            sys.exit("Brave never wrote its DevTools port")
        guard.attach(pf)
        if not wait_for(lambda: guard._main, 15):
            sys.exit("guard never attached to the page")

        def eval_js(expr):
            done, box = threading.Event(), {}
            guard._send("Runtime.evaluate", {"expression": expr, "returnByValue": True}, guard._main,
                        cb=lambda r: (box.update(r), done.set()))
            done.wait(8)
            return box.get("result", {}).get("value")

        def tap(x, y):
            guard.mouse("mouseMoved", x, y)
            guard.mouse("mousePressed", x, y, "left", 1, 1)
            guard.mouse("mouseReleased", x, y, "left", 0, 1)

        guard.phone_watch(390, 760)
        guard.youtube_mode(True, "https://www.youtube.com/results?search_query=lofi+hip+hop")
        ok = wait_for(lambda: "youtube.com/results" in guard.snapshot()["url"]
                      and eval_js("document.querySelectorAll('a[href*=\"/watch?v=\"]').length") or 0, 30)
        check("YouTube opens", ok, guard.snapshot()["url"])
        check("the page is drawn in YouTube's dark theme", eval_js("document.documentElement.hasAttribute('dark')"))
        check("...and in true black", eval_js("getComputedStyle(document.body).backgroundColor") == "rgb(0, 0, 0)",
              str(eval_js("getComputedStyle(document.body).backgroundColor")))
        check("the switch's cookie is on the page", eval_js("document.cookie.includes('rl_yt=1')"))

        pos = eval_js("""(()=>{const a=[...document.querySelectorAll('a[href]')].find(a=>/\\/watch\\?v=/.test(a.href)
            &&a.getBoundingClientRect().width>100&&a.getBoundingClientRect().top>0&&a.getBoundingClientRect().top<700);
            if(!a)return null;const r=a.getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]})()""")
        check("there is a video to tap", pos)
        before = guard.snapshot()["url"]
        tap(*pos)
        check("tapping it hands the video to the TV", wait_for(lambda: tapped, 5) and "/watch?v=" in tapped[0], str(tapped))
        time.sleep(2)
        check("...and the browser stays on the results", guard.snapshot()["url"] == before, guard.snapshot()["url"])

        guard.youtube_mode(False)
        check("turning the switch off removes its cookie", wait_for(lambda: not eval_js("document.cookie.includes('rl_yt=1')"), 5))
        tapped.clear()
        tap(*pos)
        time.sleep(3)
        check("with it off, a tap opens the video here as normal", not tapped and "/watch?v=" in guard.snapshot()["url"],
              guard.snapshot()["url"])
    finally:
        guard.stop()
        proc.terminate()
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
