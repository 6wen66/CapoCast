"""Does the Web tab find and relay each kind of streaming site? Real headless Brave, real players (hls.js / dash.js
from a CDN), real relay, generated test video. Nothing appears on screen.

    python tests/formats_smoke.py

Sites it serves (all on localhost):
  hls          plain HLS, master + media playlists
  hls-referer  HLS whose every request must carry the page's Referer (like most real streaming CDNs)
  hls-hidden   HLS whose playlist is called list.txt (text/plain) and whose segments are called sN.jpg (image/jpeg)
  dash         DASH with relative segment URLs
  dash-abs     DASH whose manifest uses absolute CDN-style URLs
Needs Brave, ffmpeg and internet (for the two player libraries). Takes ~2 min.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import brave_profile  # noqa: E402
from drm import DrmError  # noqa: E402
from media_relay import MediaRelay, probe_dash, probe_hls  # noqa: E402
from stream_finder import apply_page_videos, choose, score  # noqa: E402
from web_guard import WebGuard  # noqa: E402

HLS_JS = "https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.17/hls.min.js"
DASH_JS = "https://cdnjs.cloudflare.com/ajax/libs/dashjs/4.7.4/dash.all.min.js"
WORK = os.path.join(tempfile.gettempdir(), "rl-formats-media")
REFERER_SITE = {}
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def make_media():
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        sys.exit("ffmpeg isn't on PATH")
    os.makedirs(os.path.join(WORK, "hls"), exist_ok=True)
    os.makedirs(os.path.join(WORK, "dash"), exist_ok=True)
    src = ["-f", "lavfi", "-i", "testsrc=duration=130:size=320x180:rate=10", "-f", "lavfi", "-i",
           "sine=frequency=440:duration=130", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "36",
           "-g", "20", "-c:a", "aac", "-b:a", "32k", "-shortest"]
    if not os.path.exists(os.path.join(WORK, "hls", "v.m3u8")):
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", *src, "-f", "hls", "-hls_time", "6", "-hls_playlist_type", "vod",
                        "-hls_segment_filename", os.path.join(WORK, "hls", "seg%d.ts"),
                        os.path.join(WORK, "hls", "v.m3u8")], check=True)
    if not os.path.exists(os.path.join(WORK, "dash", "manifest.mpd")):
        # ffmpeg writes DASH segments into the *working directory*, not next to the manifest: run it from the
        # scratch folder so they don't land in whatever directory this was started from.
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", *src, "-f", "dash", "-seg_duration", "6", "-use_template", "1",
                        "-use_timeline", "0", "manifest.mpd"], check=True, cwd=os.path.join(WORK, "dash"))


def read(*parts):
    return open(os.path.join(WORK, *parts), "rb").read()


class Site(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    ports = {"main": 0, "cdn": 0}

    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def send(self, body, ctype, status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def page(self, js, script):
        html = (f'<!doctype html><title>player</title><video id="v" muted style="width:640px;height:360px"></video>'
                f'<script src="{js}"></script><script>{script}</script>')
        self.send(html.encode(), "text/html; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?")[0]
        cdn = f"http://localhost:{self.ports['cdn']}"
        if path == "/hls.html":
            return self.page(HLS_JS, f"var h=new Hls();h.loadSource('{cdn}/hls/master.m3u8');h.attachMedia(v);"
                                     "h.on(Hls.Events.MANIFEST_PARSED,()=>v.play());")
        if path == "/hls-referer.html":
            return self.page(HLS_JS, f"var h=new Hls();h.loadSource('{cdn}/guarded/master.m3u8');h.attachMedia(v);"
                                     "h.on(Hls.Events.MANIFEST_PARSED,()=>v.play());")
        if path == "/hls-hidden.html":
            return self.page(HLS_JS, f"var h=new Hls();h.loadSource('{cdn}/hidden/list.txt');h.attachMedia(v);"
                                     "h.on(Hls.Events.MANIFEST_PARSED,()=>v.play());")
        if path == "/dash.html":
            return self.page(DASH_JS, f"var p=dashjs.MediaPlayer().create();p.initialize(v,'{cdn}/dash/manifest.mpd',true);")
        if path == "/dash-abs.html":
            return self.page(DASH_JS, f"var p=dashjs.MediaPlayer().create();p.initialize(v,'{cdn}/dashabs/manifest.mpd',true);")
        # ---- plain HLS ----
        if path == "/hls/master.m3u8":
            return self.send(b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=320x180\nv.m3u8\n",
                             "application/vnd.apple.mpegurl")
        if path in ("/hls/v.m3u8",):
            return self.send(read("hls", "v.m3u8"), "application/vnd.apple.mpegurl")
        m = re.fullmatch(r"/hls/(seg\d+\.ts)", path)
        if m:
            return self.send(read("hls", m.group(1)), "video/mp2t")
        # ---- HLS that needs the Referer ----
        if path.startswith("/guarded/"):
            if not (self.headers.get("Referer") or "").startswith(f"http://127.0.0.1:{self.ports['main']}/"):
                return self.send(b"forbidden", "text/plain", 403)
            if path == "/guarded/master.m3u8":
                return self.send(b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=320x180\nv.m3u8\n",
                                 "application/vnd.apple.mpegurl")
            if path == "/guarded/v.m3u8":
                return self.send(read("hls", "v.m3u8"), "application/vnd.apple.mpegurl")
            m = re.fullmatch(r"/guarded/(seg\d+\.ts)", path)
            if m:
                return self.send(read("hls", m.group(1)), "video/mp2t")
        # ---- HLS in disguise: playlist is list.txt (text/plain), segments are sN.jpg (image/jpeg) ----
        if path == "/hidden/list.txt":
            text = read("hls", "v.m3u8").decode()
            return self.send(re.sub(r"seg(\d+)\.ts", r"s\1.jpg", text).encode(), "text/plain")
        m = re.fullmatch(r"/hidden/s(\d+)\.jpg", path)
        if m:
            return self.send(read("hls", f"seg{m.group(1)}.ts"), "image/jpeg")
        # ---- DASH ----
        if path == "/dash/manifest.mpd":
            return self.send(read("dash", "manifest.mpd"), "application/dash+xml")
        m = re.fullmatch(r"/dash/([\w.-]+\.(?:m4s|mp4))", path)
        if m:
            return self.send(read("dash", m.group(1)), "video/mp4")
        if path == "/dashabs/manifest.mpd":
            text = read("dash", "manifest.mpd").decode()
            text = re.sub(r'(initialization|media)="([^"]+)"', lambda m: f'{m.group(1)}="http://localhost:{self.ports["cdn"]}/dash/{m.group(2)}"', text)
            return self.send(text.encode(), "application/dash+xml")
        self.send(b"not found", "text/plain", 404)


def serve():
    main = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    cdn = ThreadingHTTPServer(("127.0.0.1", 0), Site)               # reached as localhost: a different site
    for s in (main, cdn):
        s.daemon_threads = True
        threading.Thread(target=s.serve_forever, daemon=True).start()
    Site.ports = {"main": main.server_address[1], "cdn": cdn.server_address[1]}
    return main, cdn


def run_case(guard, page_url, label, kind):
    guard.navigate(page_url)
    deadline = time.time() + 40
    cands = []
    while time.time() < deadline:
        time.sleep(2)
        cands = [c for c in guard.candidates() if c.kind == kind]
        if cands and any(v.get("playing") for v in guard.page_videos()) and time.time() > deadline - 28:
            break
    time.sleep(2)
    cands = guard.candidates()
    apply_page_videos(cands, guard.page_videos())
    active = guard.active_frames()
    covered, pool = set(), []
    for c in sorted(cands, key=lambda c: score(c, active), reverse=True)[:5]:
        try:
            if c.kind == "hls":
                covered.update(probe_hls(c))
            elif c.kind == "dash":
                probe_dash(c)
        except DrmError:
            continue
        except Exception as e:
            print(f"   (probe of {c.url[-40:]} failed: {e!r})")
        pool.append(c)
    for c in pool:
        c.covered = c.url in covered
    d = choose(pool, active)
    check(f"{label}: the stream was found and chosen", bool(d.best) and d.best.kind == kind,
          f"best={d.best.label() if d.best else None}; seen={[c.kind + ':' + c.url.rsplit('/', 1)[-1] for c in cands]}")
    if not d.best:
        return
    check(f"{label}: length was measured", 100 < d.best.duration < 140, f"{d.best.duration:.0f}s")
    relay = MediaRelay("127.0.0.1", allow=lambda: "0.0.0.0", allow_loopback=True)
    relay.start()
    try:
        url = relay.add(d.best.url, d.best.headers, d.best.kind)
        entry = urllib.request.urlopen(url, timeout=20)
        body = entry.read().decode(errors="replace")
        check(f"{label}: relay serves the entry", entry.status == 200 and len(body) > 50, f"{len(body)} bytes")
        if d.best.kind == "hls":
            uris = lambda text: [l for l in text.splitlines() if l and not l.startswith("#")]
            if "#EXT-X-STREAM-INF" in body:               # a master playlist: go one level down to a media playlist
                body = urllib.request.urlopen(relay.base + uris(body)[0], timeout=20).read().decode(errors="replace")
            seg = uris(body)[0]
            seg_resp = urllib.request.urlopen(relay.base + seg, timeout=20)
            data = seg_resp.read(2000)
            check(f"{label}: a video segment comes through the relay as video",
                  seg.endswith(".ts") and seg_resp.headers["Content-Type"] == "video/mp2t" and data[:1] == b"G",
                  f"{seg} {seg_resp.headers['Content-Type']} starts {data[:4]!r}")
        else:
            init = re.search(r'initialization="([^"]+)"', body).group(1).replace("$RepresentationID$", "0")
            target = relay.base + init if init.startswith("/") else url.rsplit("/", 1)[0] + "/" + init
            resp = urllib.request.urlopen(target, timeout=20)
            data = resp.read(64)
            check(f"{label}: a DASH segment comes through the relay", resp.status == 200 and b"ftyp" in data,
                  f"{init} starts {data[:12]!r}")
    finally:
        relay.stop()


def main():
    for lib in (HLS_JS, DASH_JS):
        try:
            urllib.request.urlopen(lib, timeout=10).read(10)
        except Exception as e:
            sys.exit(f"can't reach {lib}: {e}")
    make_media()
    main_srv, cdn_srv = serve()
    base = f"http://127.0.0.1:{Site.ports['main']}"
    profile = os.path.join(tempfile.gettempdir(), "rl-formats-profile")
    shutil.rmtree(profile, ignore_errors=True)
    brave_profile.seed_filters(profile)
    pf = brave_profile.port_file(profile)
    guard = WebGuard(lambda m: None, home="about:blank")
    guard.set_view(900, 600, False)
    proc = subprocess.Popen([brave_profile.find_brave(), *brave_profile.launch_args(profile, "about:blank")],
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        end = time.time() + 25
        while not os.path.exists(pf) and time.time() < end:
            time.sleep(0.3)
        guard.attach(pf)
        end = time.time() + 15
        while not (guard._main and guard._main_frame) and time.time() < end:
            time.sleep(0.2)
        for page, label, kind in (("hls.html", "plain HLS", "hls"), ("hls-referer.html", "HLS with a Referer check", "hls"),
                                  ("hls-hidden.html", "HLS disguised as .txt/.jpg", "hls"), ("dash.html", "DASH", "dash"),
                                  ("dash-abs.html", "DASH with absolute CDN URLs", "dash")):
            run_case(guard, f"{base}/{page}", label, kind)
    finally:
        guard.close_browser()
        time.sleep(1)
        guard.stop()
        try:
            proc.wait(4)
        except Exception:
            proc.terminate()
        main_srv.shutdown()
        cdn_srv.shutdown()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
