"""Casts the decoy site's episode from the real Web tab with a subtitle language chosen, against a fake Roku on
localhost, and checks the TV is handed a stream with the words burned into the picture.

    python tests/subtitle_smoke.py

Nothing appears on screen (headless Brave, a Tk root that is never mapped). Needs an NVIDIA GPU, FFmpeg with libass
and hevc_nvenc, and port 8060 free (the fake Roku). Takes about a minute.
"""
import os
import subprocess
import sys
import tempfile
import time
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import tkinter.messagebox as messagebox  # noqa: E402

import decoy_site  # noqa: E402
import roku_listen_gui as g  # noqa: E402
import upscaler  # noqa: E402
import web_tab  # noqa: E402
import web_tab_smoke as smoke  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def main():
    why = upscaler.missing("burn", True)
    if why:
        sys.exit(why)
    try:
        roku_srv = ThreadingHTTPServer(("127.0.0.1", 8060), smoke.FakeRoku)
    except OSError as e:
        sys.exit(f"port 8060 is busy ({e})")
    import threading
    threading.Thread(target=roku_srv.serve_forever, daemon=True).start()
    messagebox.showinfo = lambda title, msg, **k: print(f"   [dialog] {title}: {msg}")
    url, other, hits, stop_site = decoy_site.start()
    root = tk.Tk()
    root.withdraw()
    app = smoke.FakeApp(root)
    ui = SimpleNamespace(BG=g.BG, CARD=g.CARD, EDGE=g.EDGE, FG=g.FG, MUTED=g.MUTED, ACCENT=g.ACCENT, ACCENT_HI=g.ACCENT_HI,
                         GREEN=g.GREEN, AMBER=g.AMBER, RED=g.RED, GREY=g.GREY, FONT=g.FONT, FONT_MED=g.FONT_MED,
                         RoundButton=g.RoundButton, NavButton=g.NavButton, Chip=g.Chip, Tooltip=g.Tooltip, Remote=g.Remote,
                         log=lambda m: print("   log:", m) if any(k in m for k in ("subtitles", "cast", "upscale", "failed")) else None)
    page = tk.Frame(root)
    tab = web_tab.WebTab(app, page, ui, home=url, subs="english")
    tab.profile = os.path.join(tempfile.gettempdir(), "rl-subsmoke-profile")

    def pump(seconds, until=None):
        end = time.time() + seconds
        while time.time() < end:
            root.update()
            app.drain()
            if until and until():
                return True
            time.sleep(0.03)
        return bool(until and until())

    try:
        tab.on_show()
        check("the Web tab opens", pump(45, lambda: tab.view.has_frame and tab.guard.snapshot()["running"]))
        check("the label says which language is chosen", tab.subs_lbl.cget("text") == "Subtitles: English",
              tab.subs_lbl.cget("text"))
        pump(14, lambda: any(v.get("playing") and "real.mp4" in v.get("src", "") for v in tab.guard.page_videos()))
        pump(2)
        files, dom = tab.guard.subtitle_sources()
        check("the page's subtitle file and track were seen", bool(files) and bool(dom), f"{len(files)} files, {len(dom)} tracks")

        tab.cast()
        ok = pump(90, lambda: not tab.busy)
        note = tab.note[0]
        check("the cast finishes and says it's on the TV", ok and note.startswith("On TV"), repr(note))
        launched = smoke.roku["launches"][-1][1] if smoke.roku["launches"] else ""
        sess = tab.relay.session_for(launched) if launched else None
        check("the TV was given a subtitle-burning stream (not the plain relay)",
              isinstance(sess, upscaler.UpscaleSession) and sess.mode == "burn" and sess.subs,
              type(sess).__name__ + (f" mode={getattr(sess, 'mode', None)}" if sess else ""))
        if sess is None:
            return
        ass = os.path.join(sess.dir, "subs.ass")
        text = open(ass, encoding="utf-8").read() if os.path.exists(ass) else ""
        check("the subtitle file on this PC holds the page's words, styled for a TV",
              "Decoy subtitle" in text and "Style: Default,Roboto" in text, repr(text[-160:]))
        check("the tab remembers this cast has subtitles (so Discord and seeks keep them)",
              bool((tab.upscaled or {}).get("subs")) and (tab.upscaled or {}).get("mode") == "burn", repr(tab.upscaled))

        # a frame from what the TV is given
        piece = sess.file_for(f"v{sess.first + 1:05d}.m4s", timeout=30)
        init = sess.file_for("v_init.mp4", timeout=5)
        check("the encoder is producing pieces", bool(piece and init))
        if piece and init:
            out = os.path.join(tempfile.gettempdir(), "rl-subsmoke-frame")
            os.makedirs(out, exist_ok=True)
            clip = os.path.join(out, "piece.mp4")
            with open(clip, "wb") as f:
                f.write(open(init, "rb").read())
                f.write(open(piece, "rb").read())
            png = os.path.join(out, "frame.png")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", clip, "-frames:v", "1", png], check=False)
            print(f"   a frame of what the TV gets: {png}")
            check("a frame was decoded", os.path.exists(png) and os.path.getsize(png) > 1000)
        log = open(os.path.join(sess.run_dirs[0], "ffmpeg.log"), errors="replace").read()
        check("FFmpeg reported no errors", "error" not in log.lower(), log[-200:])
    finally:
        tab.close()
        root.destroy()
        roku_srv.shutdown()
        stop_site()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
