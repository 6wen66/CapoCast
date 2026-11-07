"""Local bridge between the Chrome extension and CapoCast.

The extension (running in the app's embedded Chrome) POSTs player events to /event (load / play / pause / seek)
and polls /command for settings and things the app wants Chrome to do (navigate, history, reload). Only requests
carrying X-RokuListen from the extension (or with no Origin) are served, so ordinary web pages can't talk to it.
"""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 8765


class _Server(ThreadingHTTPServer):
    """Never share the port: Windows lets two sockets bind the same port with SO_REUSEADDR, and then a second copy
    of the app silently receives (or misses) the extension's traffic."""
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Bridge:
    def __init__(self, log=lambda msg: None):
        self.log = log
        self._lock = threading.Lock()
        self._commands = []
        self.settings = {"autocast": True}   # sent to the extension on every poll
        self.on_event = None                   # called (on a server thread) with each player event
        self.on_poll = None                    # called (timer_gap, round_trip in seconds) with each poll, for the health panel
        self.seen = 0.0                        # last time the extension was heard from
        self.server = None
        self._rejected = 0

    # ---- app side --------------------------------------------------------
    def start(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep the console/log quiet
                pass

            def _ok_origin(self):
                # Chrome omits Origin on some extension requests (host-permission fetches skip CORS), so a missing
                # Origin is fine. A web page can't add X-RokuListen without a CORS preflight, which do_OPTIONS
                # refuses for non-extension origins, and a page's request always carries its own Origin.
                origin = self.headers.get("Origin", "")
                ok = self.headers.get("X-RokuListen") == "1" and (not origin or origin.startswith("chrome-extension://"))
                if not ok and bridge._rejected < 5:
                    bridge._rejected += 1
                    bridge.log(f"bridge rejected {self.command} {self.path} origin={origin!r} "
                               f"x-rokulisten={self.headers.get('X-RokuListen')!r}")
                return ok, origin

            def _reply(self, code, body=None, origin=""):
                data = json.dumps(body if body is not None else {}).encode()
                self.send_response(code)
                if origin:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Access-Control-Allow-Headers", "Content-Type, X-RokuListen")
                    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                    self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_OPTIONS(self):
                origin = self.headers.get("Origin", "")
                self._reply(204 if origin.startswith("chrome-extension://") else 403, origin=origin)

            def do_GET(self):
                ok, origin = self._ok_origin()
                if not ok:
                    return self._reply(403)
                if self.path.startswith("/command"):
                    q = parse_qs(urlparse(self.path).query)
                    bridge._note_player(q)
                    bridge._note_poll(q)
                    return self._reply(200, bridge._pop_command(), origin)
                self._reply(404, origin=origin)

            def do_POST(self):
                ok, origin = self._ok_origin()
                if not ok:
                    return self._reply(403)
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(n) or b"{}")
                except Exception:
                    return self._reply(400, origin=origin)
                if self.path.startswith("/event"):
                    bridge.seen = time.time()
                    if bridge.on_event:
                        bridge.on_event(body)
                    return self._reply(200, origin=origin)
                self._reply(404, origin=origin)

        self.server = _Server(("127.0.0.1", PORT), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.log(f"bridge listening on 127.0.0.1:{PORT}")

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def send(self, command):
        """Queue a command for the extension, e.g. {"navigate": "https://..."} or {"pause": True}."""
        with self._lock:
            self._commands.append(command)

    def connected(self):
        return time.time() - self.seen < 10

    def _note_player(self, q):
        """Where the embedded player is (sent with every poll), stamped with when we heard it."""
        try:
            self._player = {"id": q["id"][0], "t": float(q["t"][0]), "paused": q.get("paused", ["0"])[0] == "1",
                            "ad": q.get("ad", ["0"])[0] == "1", "at": time.time()}
        except (KeyError, ValueError):
            self._player = None

    def _note_poll(self, q):
        """Tell the health panel a poll arrived, with what the page measured about itself (seconds; 0 if it didn't say)."""
        if not self.on_poll:
            return
        num = lambda k: max(0.0, min(3600.0, float(q.get(k, ["0"])[0])))
        try:
            self.on_poll(num("gap"), num("rtt"))
        except Exception as e:                  # a broken callback must not make the extension's poll fail
            self.log(f"poll callback failed: {e!r}")

    def player(self):
        """{id, t, paused} with t extrapolated to now, or None if no video is being cast."""
        p = getattr(self, "_player", None)
        if not p or time.time() - p["at"] > 2:
            return None
        t = p["t"] if p["paused"] else p["t"] + (time.time() - p["at"])
        return {"id": p["id"], "t": t, "paused": p["paused"], "ad": p.get("ad", False)}

    def _pop_command(self):
        with self._lock:
            if not self.connected():
                self.log("extension connected")
            self.seen = time.time()   # polling counts as a heartbeat too
            command = self._commands.pop(0) if self._commands else {}
            return {**command, "settings": dict(self.settings)}   # settings ride along on every poll
