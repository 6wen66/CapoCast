"""A deliberately hostile test site for the Web tab. Not a unit test: run guard_smoke.py (automatic) or run this
file directly and open the printed URL in the Web tab (manual).

Two servers on different "sites" (127.0.0.1 and localhost), so the player iframe is cross-site like on real
streaming pages. The player plays a 12 s ad, then a 150 s episode.
"""
import os
import re
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")


def make_media(folder):
    """Generate the two clips with ffmpeg (small, plays natively in Chromium)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg isn't on PATH")
    for name, seconds in (("ad.mp4", 12), ("real.mp4", 150)):
        out = os.path.join(folder, name)
        if os.path.exists(out):
            continue
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"testsrc=duration={seconds}:size=320x180:rate=10", "-f", "lavfi", "-i",
                        f"sine=frequency=440:duration={seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-preset", "veryfast", "-crf", "35", "-c:a", "aac", "-b:a", "32k", "-movflags",
                        "+faststart", "-shortest", out], check=True)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    media = ""
    ports = {"MAIN": 0, "OTHER": 0}
    hits = []

    def log_message(self, *a):
        pass

    def handle_error(self, *a):          # (on the server class; see _Quiet below)
        pass

    def _send(self, body, ctype="text/html; charset=utf-8", status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _page(self, name):
        text = open(os.path.join(FIXTURES, name), encoding="utf-8").read()
        text = re.sub(r"\{(MAIN|OTHER)\}", lambda m: str(self.ports[m.group(1)]), text)
        self._send(text.encode())

    def do_GET(self):
        path = self.path.split("?")[0]
        _Handler.hits.append(path)
        if path == "/":
            return self._page("decoy_page.html")
        if path == "/player.html":
            return self._page("decoy_player.html")
        if path in ("/ad.mp4", "/real.mp4"):
            data = open(os.path.join(self.media, path[1:]), "rb").read()
            rng = self.headers.get("Range")
            if rng:
                a, _, b = rng.replace("bytes=", "").partition("-")
                a, b = int(a or 0), int(b) if b else len(data) - 1
                return self._send(data[a:b + 1], "video/mp4", 206,
                                  {"Content-Range": f"bytes {a}-{b}/{len(data)}", "Accept-Ranges": "bytes"})
            return self._send(data, "video/mp4", 200, {"Accept-Ranges": "bytes"})
        if path in ("/pop", "/ad", "/page2"):
            return self._send(f"<title>{path}</title><h1>{path}</h1>".encode())
        self._send(b"not found", "text/plain", 404)

    do_HEAD = do_GET


class _Quiet(ThreadingHTTPServer):
    """A browser resetting its connections is normal here; don't print a traceback for each one."""
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


def start(media=None):
    """Start both servers. Returns (main_url, other_origin, hits, stop)."""
    media = media or os.path.join(tempfile.gettempdir(), "rl-decoy-media")
    os.makedirs(media, exist_ok=True)
    make_media(media)
    _Handler.media, _Handler.hits = media, []
    main = _Quiet(("127.0.0.1", 0), _Handler)
    other = _Quiet(("127.0.0.1", 0), _Handler)       # reached as http://localhost:<port>
    _Handler.ports = {"MAIN": main.server_address[1], "OTHER": other.server_address[1]}
    for s in (main, other):
        threading.Thread(target=s.serve_forever, daemon=True).start()

    def stop():
        main.shutdown()
        other.shutdown()
    return f"http://127.0.0.1:{_Handler.ports['MAIN']}/", _Handler.ports["OTHER"], _Handler.hits, stop


if __name__ == "__main__":
    url, other, _, stop = start()
    print("Open in the Web tab:", url)
    print("Add ?bounce=1 to test the redirect guard:", url + "?bounce=1")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        stop()
