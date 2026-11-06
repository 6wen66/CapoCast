"""A remote control for the phone: a small web server on the home network, plus the page it serves.

Open the page on an iPhone (or any phone), choose Share > Add to Home Screen, and it behaves like an app: the Roku's
d-pad and playback keys, volume, typing, the TV's apps, and "send this link to the TV". No app store, no Apple account,
no signing.

Every request needs the pairing token (shown in the link/QR code in CapoCast), only private-network addresses are
served, and the whole thing is off until you turn it on.
"""
import hmac
import html
import ipaddress
import json
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765
MAX_BODY = 8192

# Everything the phone may press. Power and the like are left out on purpose: a stray tap shouldn't turn the TV off.
KEYS = frozenset({
    "Up", "Down", "Left", "Right", "Select", "Back", "Home", "Play", "Rev", "Fwd", "InstantReplay", "Info",
    "Backspace", "Enter", "VolumeUp", "VolumeDown", "VolumeMute",
})

YOUTUBE_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be")
_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PLAYLIST = re.compile(r"^[A-Za-z0-9_-]{10,64}$")


def new_token():
    return secrets.token_urlsafe(9)


def youtube_target(url):
    """A YouTube link -> (video_id, playlist_id or None, start seconds), or None if it isn't one."""
    try:
        p = urllib.parse.urlparse(url.strip())
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or host not in YOUTUBE_HOSTS:
        return None
    q = urllib.parse.parse_qs(p.query)
    parts = [x for x in p.path.split("/") if x]
    vid = None
    if host == "youtu.be":
        vid = parts[0] if parts else None
    elif parts[:1] == ["watch"]:
        vid = (q.get("v") or [None])[0]
    elif len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v"):
        vid = parts[1]
    if not vid or not _ID.match(vid):
        return None
    playlist = (q.get("list") or [None])[0]
    if playlist and not _PLAYLIST.match(playlist):
        playlist = None
    start = 0
    m = re.match(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?$", (q.get("t") or q.get("start") or [""])[0])
    if m and any(m.groups()):
        h, mi, s = (int(x or 0) for x in m.groups())
        start = h * 3600 + mi * 60 + s
    return vid, playlist, start


def private_client(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_private or a.is_loopback


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass                                              # phones drop connections all the time (sleep, Wi-Fi roaming)


class PhoneRemote:
    """The server. `backend` is whatever drives the TV:

        state() -> {"tv": {...} | None, "volume": {...}, "note": str}
        key(name)  text(s)  seek(delta)  volume(step)  launch(app_id)  stop_cast()
        apps() -> [{"id", "name"}]   icon(app_id) -> png bytes   cast(url) -> message
    Any of them may raise; the message goes back to the phone."""

    def __init__(self, backend, token, icon_png=b"", port=PORT, log=lambda m: None, bind="0.0.0.0"):
        self.backend, self.token, self.icon_png, self.log = backend, token, icon_png, log
        self.port, self.bind = port, bind
        self.server, self.thread = None, None
        self.fails, self.fail_lock = 0, threading.Lock()

    @property
    def running(self):
        return self.server is not None

    def start(self):
        if self.server:
            return self.port
        last = None
        for port in range(self.port, self.port + 10):      # another program may be sitting on the first choice
            try:
                self.server = _Server((self.bind, port), self._handler())
                break
            except OSError as e:
                last = e
        if not self.server:
            raise last
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.port

    def stop(self):
        server, self.server = self.server, None
        if server:
            threading.Thread(target=lambda: (server.shutdown(), server.server_close()), daemon=True).start()

    def link(self, ip):
        return f"http://{ip}:{self.port}/?k={self.token}"

    def _token_ok(self, given):
        return bool(given) and hmac.compare_digest(str(given).encode(), self.token.encode())

    # ---- HTTP ----------------------------------------------------------------------------------------------
    def _handler(self):
        remote = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 15

            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="application/json", extra=None):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode()
                elif isinstance(body, str):
                    body = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _authorised(self, query):
                given = self.headers.get("X-Token") or (query.get("k") or [""])[0]
                if remote._token_ok(given):
                    return True
                with remote.fail_lock:                       # guessing is slow, not just unlikely
                    remote.fails += 1
                    delay = min(remote.fails * 0.3, 3.0)
                time.sleep(delay)
                return False

            def _route(self, method):
                if not private_client(self.client_address[0]):
                    return self._send(403, {"error": "Only devices on your home network can use this."})
                url = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(url.query)
                path = url.path
                if method == "GET" and path == "/icon.png":
                    return self._send(200, remote.icon_png, "image/png")
                if method == "GET" and path in ("/", "/index.html"):
                    ok = remote._token_ok((query.get("k") or [""])[0])
                    return self._send(200, page_html(remote.token if ok else ""), "text/html; charset=utf-8")
                if method == "GET" and path in ("/send", "/setup"):
                    if not self._authorised(query):
                        return self._send(401, plain_page("This link is out of date",
                                                          "Open CapoCast on your PC and scan the code again."),
                                          "text/html; charset=utf-8")
                    if path == "/setup":
                        host = (self.headers.get("Host") or "").strip() or f"127.0.0.1:{remote.port}"
                        return self._send(200, setup_page(f"http://{host}", remote.token), "text/html; charset=utf-8")
                    try:
                        msg = remote.backend.cast(shared_link((query.get("u") or [""])[0]))
                        return self._send(200, plain_page("Sent to the TV", msg, remote.token), "text/html; charset=utf-8")
                    except Exception as e:
                        remote.log(f"phone remote /send: {e!r}")
                        return self._send(200, plain_page("Couldn't send that", str(e) or "The TV didn't answer.",
                                                          remote.token), "text/html; charset=utf-8")
                if method == "GET" and path == "/manifest.webmanifest":
                    if not self._authorised(query):
                        return self._send(401, {"error": "bad token"})
                    return self._send(200, manifest(remote.token), "application/manifest+json")
                if not path.startswith("/api/"):
                    return self._send(404, {"error": "not found"})
                if not self._authorised(query):
                    return self._send(401, {"error": "This link is out of date. Scan the code in CapoCast again."})
                with remote.fail_lock:
                    remote.fails = 0
                body = {}
                if method == "POST":
                    try:
                        n = int(self.headers.get("Content-Length") or 0)
                        if n > MAX_BODY:
                            return self._send(413, {"error": "too big"})
                        body = json.loads(self.rfile.read(n) or b"{}")
                        if not isinstance(body, dict):
                            raise ValueError
                    except (ValueError, TypeError):
                        return self._send(400, {"error": "bad request"})
                try:
                    code, out, ctype, *more = remote.dispatch(method, path, body, query)
                except (ValueError, KeyError) as e:
                    return self._send(400, {"error": str(e).strip("'\"") or "bad request"})
                except Exception as e:
                    remote.log(f"phone remote {path}: {e!r}")
                    return self._send(502, {"error": str(e) or "The TV didn't answer."})
                self._send(code, out, ctype, more[0] if more else None)

            def do_GET(self):
                self._route("GET")

            def do_HEAD(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

        return Handler

    def dispatch(self, method, path, body, query=None):
        """-> (status, payload, content type[, extra headers]). Raises ValueError for a bad request."""
        b = self.backend
        query = query or {}
        if path.startswith("/api/web/"):
            return self.dispatch_web(method, path[len("/api/web/"):], body, query)
        if method == "GET":
            if path == "/api/state":
                return 200, b.state(), "application/json"
            if path == "/api/apps":
                return 200, {"apps": b.apps()}, "application/json"
            if path == "/api/playlet":
                return 200, b.playlet(), "application/json"
            if path.startswith("/api/icon/"):
                png = b.icon(urllib.parse.unquote(path[len("/api/icon/"):]))
                return 200, png, "image/png"
            return 404, {"error": "not found"}, "application/json"
        if path == "/api/key":
            name = str(body.get("name", ""))
            if name not in KEYS:
                raise ValueError("unknown key")
            b.key(name)
        elif path == "/api/text":
            text = str(body.get("text", ""))[:200]
            if not text:
                raise ValueError("nothing to type")
            b.text(text)
        elif path == "/api/seek":
            delta = float(body.get("delta", 0))
            if not -600 <= delta <= 600:
                raise ValueError("out of range")
            b.seek(delta)
        elif path == "/api/volume":
            step = int(body.get("step", 0))
            if not -20 <= step <= 20 or step == 0:
                raise ValueError("out of range")
            b.volume(step)
        elif path == "/api/launch":
            app_id = str(body.get("id", ""))
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", app_id):
                raise ValueError("bad app id")
            b.launch(app_id)
        elif path == "/api/seekto":
            t = float(body.get("t", -1))
            if not 0 <= t <= 86400:
                raise ValueError("out of range")
            b.seek_to(t)
        elif path == "/api/cast":
            return 200, {"message": b.cast(shared_link(body.get("url")))}, "application/json"
        elif path == "/api/stop":
            b.stop_cast()
        else:
            return 404, {"error": "not found"}, "application/json"
        return 200, {"ok": True}, "application/json"


    def dispatch_web(self, method, name, body, query):
        """The Browse tab: the PC's browser as a live picture, and taps/scrolls/typing sent back to it."""
        b = self.backend
        js = "application/json"
        if method == "GET" and name == "frame":
            after = _num(query, "after", 0, 0, 2**31)
            w, h = _num(query, "w", 390, 200, 2000), _num(query, "h", 700, 200, 3000)
            got = b.web_frame(int(after), int(w), int(h))
            if not got:
                return 204, b"", "image/jpeg"
            seq, jpeg, (vw, vh) = got
            return 200, jpeg, "image/jpeg", {"X-Seq": str(seq), "X-View-W": str(vw), "X-View-H": str(vh),
                                             "Access-Control-Expose-Headers": "X-Seq, X-View-W, X-View-H"}
        if method == "GET" and name == "info":
            return 200, b.web_info(), js
        if method == "GET" and name == "pages":
            return 200, b.web_pages(), js
        if method != "POST":
            return 404, {"error": "not found"}, js
        if name == "input":
            kind = body.get("type")
            if kind == "tap":
                b.web_tap(_coord(body, "x"), _coord(body, "y"))
            elif kind == "scroll":
                b.web_scroll(_coord(body, "x"), _coord(body, "y"), _coord(body, "dx", 4000), _coord(body, "dy", 4000))
            elif kind == "text":
                text = str(body.get("text", ""))[:500]
                if not text:
                    raise ValueError("nothing to type")
                b.web_text(text)
            elif kind == "key":
                key = str(body.get("name", ""))
                if key not in WEB_KEYS:
                    raise ValueError("unknown key")
                b.web_key(key)
            else:
                raise ValueError("unknown input")
        elif name == "nav":
            action = str(body.get("action", ""))
            if action not in ("back", "forward", "reload", "home", "go"):
                raise ValueError("unknown action")
            url = str(body.get("url", "")).strip()
            if action == "go" and (not url or len(url) > 2000):
                raise ValueError("Type an address or a search first.")
            b.web_nav(action, url)
        elif name == "bookmark":
            action = str(body.get("action", ""))
            if action not in ("toggle", "remove", "clear_history"):
                raise ValueError("unknown action")
            return 200, {"bookmarked": b.web_bookmark(action, str(body.get("url", ""))[:2000])}, js
        elif name == "transport":
            action = str(body.get("action", ""))
            if action not in ("toggle", "skip"):
                raise ValueError("unknown action")
            value = _coord(body, "value", 600)
            b.web_transport(action, value)
        elif name == "cast":
            return 200, {"message": b.web_cast()}, js
        elif name == "youtube":
            go = str(body.get("go") or "")[:300]
            b.web_youtube(bool(body.get("on")), go)
        else:
            return 404, {"error": "not found"}, js
        return 200, {"ok": True}, js


def shared_link(text):
    """The address in what a share sheet sent: the link itself, or text that contains one ('Watch this https://...')."""
    text = str(text or "").strip()
    if not text:
        raise ValueError("Paste a link first.")
    if len(text) > 4000:
        raise ValueError("That's too long to be a link.")
    if not re.search(r"\s", text):
        if len(text) > 2000:
            raise ValueError("That link is too long.")
        return text
    m = re.search(r"https?://[^\s<>\"']+", text)
    if not m:
        raise ValueError("I couldn't find a link in that.")
    return m.group(0).rstrip(".,;)")


WEB_KEYS = frozenset({"Enter", "Backspace", "Tab", "Escape"})


def _num(query, name, default, lo, hi):
    try:
        v = float((query.get(name) or [default])[0])
    except (TypeError, ValueError):
        raise ValueError(f"bad {name}")
    if not lo <= v <= hi:
        raise ValueError(f"{name} out of range")
    return v


def _coord(body, name, limit=5000):
    try:
        v = float(body.get(name, 0))
    except (TypeError, ValueError):
        raise ValueError(f"bad {name}")
    if not -limit <= v <= limit:
        raise ValueError(f"{name} out of range")
    return v


def _shell(title, body):
    return ("<!doctype html><html lang=en><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1,viewport-fit=cover'>"
            "<meta name=apple-mobile-web-app-capable content=yes><meta name=theme-color content='#0f0f12'>"
            f"<title>{html.escape(title)}</title><style>"
            "body{margin:0;background:#0f0f12;color:#f1f1f3;font:16px -apple-system,Segoe UI,Roboto,sans-serif;"
            "padding:calc(24px + env(safe-area-inset-top)) 18px 40px;max-width:520px;margin:auto;line-height:1.5}"
            "h1{font-size:22px;margin:0 0 10px}a{color:#ff6a6a}.m{color:#9a9aa5}ol{padding-left:20px}li{margin:10px 0}"
            "code{display:block;background:#1a1a1f;border:1px solid #2a2a31;border-radius:10px;padding:10px;margin:6px 0;"
            "font:13px ui-monospace,Menlo,Consolas,monospace;word-break:break-all;-webkit-user-select:all;user-select:all}"
            f"</style><body>{body}")


def plain_page(title, message, token=""):
    """What the share sheet link shows: whether the TV got it, and a way back to the remote."""
    back = f'<p><a href="/?k={html.escape(urllib.parse.quote(token))}">Open the CapoCast remote</a></p>' if token else ""
    return _shell(title, f"<h1>{html.escape(title)}</h1><p class=m>{html.escape(message)}</p>{back}")


def setup_page(base, token):
    """How to make 'Cast to TV' appear in the iPhone's Share menu (an iOS Shortcut that calls this server)."""
    k = urllib.parse.quote(token)
    e = html.escape
    return _shell("Share to TV", f"""<h1>Cast from the Share menu</h1>
<p class=m>Make a Shortcut once, and then <b>Share &rarr; Cast to TV</b> appears in Safari, YouTube and most apps.</p>
<h2 style="font-size:17px">Recommended: silent</h2>
<ol>
<li>Open the <b>Shortcuts</b> app, tap <b>+</b>, and name it <b>Cast to TV</b>.</li>
<li>Tap the <b>(i)</b> button and turn on <b>Show in Share Sheet</b>. Under Share Sheet Types, keep <b>URLs</b>, <b>Safari web pages</b> and <b>Text</b>.</li>
<li>Add the action <b>Get Contents of URL</b> and set its URL to:<code>{e(base)}/api/cast</code></li>
<li>Tap <b>Show More</b>. Set <b>Method</b> to <b>POST</b>. Under <b>Headers</b> add <b>X-Token</b> with the value:<code>{e(token)}</code></li>
<li>Set <b>Request Body</b> to <b>JSON</b>. Add a <b>Text</b> field named <code>url</code> and for its value choose <b>Shortcut Input</b>.</li>
<li>Optional: add <b>Get Dictionary Value</b> (key <code>message</code>) and then <b>Show Notification</b> to see the answer.</li>
</ol>
<h2 style="font-size:17px">Simpler: opens a confirmation page</h2>
<ol>
<li>Add a <b>Text</b> action containing this, then the <b>Shortcut Input</b> variable right after it:<code>{e(base)}/send?k={e(k)}&amp;u=</code></li>
<li>Add <b>Open URLs</b> (it uses the text above).</li>
</ol>
<p class=m>Your PC must be on and CapoCast running, with the phone on the same Wi-Fi. If you make a new link in CapoCast,
update the token above. YouTube links go straight to Playlet; other sites open in the Web tab and cast from there.</p>
<p><a href="/?k={e(k)}">&larr; Back to the CapoCast remote</a></p>""")


def manifest(token):
    return json.dumps({
        "name": "CapoCast", "short_name": "CapoCast", "start_url": f"/?k={token}", "scope": "/",
        "display": "standalone", "background_color": "#0f0f12", "theme_color": "#0f0f12",
        "icons": [{"src": "/icon.png", "sizes": "any", "type": "image/png"}],
    })


def page_html(token):
    """The whole phone app. Without the token it is only an explanation, so a stranger on the network learns nothing."""
    return PAGE.replace("__TOKEN__", json.dumps(token)[1:-1])


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="CapoCast">
<meta name="theme-color" content="#0f0f12">
<title>CapoCast</title>
<link rel="apple-touch-icon" href="/icon.png">
<link rel="icon" href="/icon.png">
<link rel="manifest" id="manifest">
<style>
:root{--bg:#0f0f12;--card:#1a1a1f;--edge:#2a2a31;--fg:#f1f1f3;--muted:#9a9aa5;--accent:#ff3d3d;--accent2:#ff6a6a;--green:#3ddc84;--amber:#ffb84d}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent;-webkit-touch-callout:none}
html,body{margin:0;background:var(--bg);color:var(--fg);font:16px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
body{min-height:100vh;padding:env(safe-area-inset-top) 16px calc(88px + env(safe-area-inset-bottom));max-width:480px;margin:0 auto;
  -webkit-user-select:none;user-select:none;overscroll-behavior:none}
input{-webkit-user-select:text;user-select:text}
header{padding:18px 0 10px}
header h1{margin:0 0 6px;font-size:22px}
#now{color:var(--muted);font-size:14px;min-height:20px}
#now b{color:var(--fg);font-weight:600}
#bar{height:4px;background:var(--edge);border-radius:2px;margin-top:10px;overflow:hidden}
#bar i{display:block;height:100%;width:0;background:var(--accent)}
#note{margin:10px 0 0;font-size:13px;min-height:18px;color:var(--amber)}
#note.ok{color:var(--green)}
.tab{display:none}.tab.on{display:block}
.row{display:flex;gap:10px;margin:12px 0}
button{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--edge);border-radius:14px;min-height:54px;flex:1;
  cursor:pointer;touch-action:manipulation}
button:active,button.hot{background:#2c2c34}
button.go{background:var(--accent);border-color:var(--accent);font-weight:600}
button.go:active{background:var(--accent2)}
button.small{min-height:44px;font-size:14px;color:var(--muted)}
.pad{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;width:min(300px,100%);margin:18px auto}
.pad button{aspect-ratio:1;min-height:0;font-size:26px;border-radius:20px}
.pad .ok{background:var(--card);font-size:16px;font-weight:600;border-radius:50%}
.pad span{display:block}
input[type=text],input[type=url],input:not([type]){width:100%;font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--edge);
  border-radius:14px;padding:14px;min-height:54px;outline:none}
input:focus{border-color:var(--accent)}
label{display:block;color:var(--muted);font-size:13px;margin:16px 0 6px}
.apps{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:12px}
.apps button{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;padding:10px 4px;min-height:96px;font-size:12px}
.apps img{width:72px;height:54px;object-fit:contain;border-radius:8px;background:#000}
nav{position:fixed;left:0;right:0;bottom:0;display:flex;gap:0;background:rgba(15,15,18,.94);backdrop-filter:blur(14px);
  -webkit-backdrop-filter:blur(14px);border-top:1px solid var(--edge);padding:6px 8px calc(6px + env(safe-area-inset-bottom))}
nav button{border:0;background:none;color:var(--muted);min-height:48px;font-size:12px;border-radius:10px}
nav button.on{color:var(--fg)}
nav button.on b{color:var(--accent)}
nav b{display:block;font-size:20px;line-height:24px}
#gone{display:none;text-align:center;padding:60px 10px;color:var(--muted);line-height:1.5}
.hint{color:var(--muted);font-size:13px;line-height:1.45;margin:10px 0}
body.browsing{max-width:none;padding:env(safe-area-inset-top) 0 0}
body.browsing header{display:none}
#t-browse.on{display:flex;flex-direction:column;height:calc(100vh - 62px - env(safe-area-inset-bottom) - env(safe-area-inset-top));
  height:calc(100dvh - 62px - env(safe-area-inset-bottom) - env(safe-area-inset-top))}
#bbar{display:flex;gap:6px;padding:8px 8px 6px;align-items:center}
#bbar button{flex:0 0 40px;min-height:40px;font-size:20px;border-radius:12px;padding:0}
#goform{flex:1;min-width:0;margin:0}
#addr{min-height:40px;padding:8px 12px;font-size:16px;border-radius:12px}
#view{position:relative;flex:1;min-height:0;background:#000;overflow:hidden;touch-action:none;-webkit-user-select:none}
#shot{position:absolute;inset:0;width:100%;height:100%;object-fit:fill;pointer-events:none;-webkit-user-drag:none}
#vmsg{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;padding:30px;color:var(--muted);
  font-size:14px;line-height:1.5;pointer-events:none}
#bstat{position:absolute;left:8px;right:8px;top:8px;padding:6px 10px;border-radius:10px;background:rgba(15,15,18,.82);font-size:12px;
  color:var(--green);pointer-events:none;display:none}
#kb{position:absolute;left:0;right:0;bottom:0;display:none;gap:6px;padding:8px;background:rgba(15,15,18,.95);border-top:1px solid var(--edge)}
#kb.on{display:flex}
#kb input{min-height:44px;padding:8px 12px;font-size:16px;flex:1;min-width:0;width:auto}
#kb button{flex:0 0 auto;min-width:48px;min-height:44px;padding:0 10px;font-size:14px}
#watch{display:none;align-items:center;gap:8px;margin:10px 0 0;padding:8px 10px;border:1px solid var(--edge);border-radius:12px;background:var(--card);font-size:13px}
#watch .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#watch .t span{color:var(--green);margin-right:6px}
#watch button{flex:0 0 auto;min-height:32px;padding:0 10px;font-size:12px;border-radius:9px;color:var(--muted)}
#btrans{display:flex;gap:8px;padding:0 8px}
#btrans button{min-height:42px;font-size:15px}
#np{display:none;margin:10px 0 0;padding:12px 14px;border:1px solid var(--edge);border-radius:14px;background:var(--card)}
#np .r{display:flex;justify-content:space-between;gap:10px;align-items:baseline}
#nptitle{font-size:15px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;word-break:break-word}
#npapp{font-size:12px;color:var(--muted);flex:0 0 auto}
#npseek{width:100%;margin:10px 0 2px;accent-color:var(--accent);height:28px;touch-action:pan-y}
#np .tm{display:flex;justify-content:space-between;color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
#sheet{position:fixed;inset:0;z-index:20;background:rgba(0,0,0,.6);display:none;align-items:flex-end}
#sheet.on{display:flex}
#sheet .sh{width:100%;max-width:520px;margin:0 auto;max-height:78vh;display:flex;flex-direction:column;background:var(--bg);
  border:1px solid var(--edge);border-radius:18px 18px 0 0;padding:12px 12px calc(12px + env(safe-area-inset-bottom))}
#sheet .shh{display:flex;gap:8px;margin-bottom:8px}
#sheet .shh button{min-height:40px;font-size:14px}
#sheet .shh button.on{background:var(--accent);border-color:var(--accent);font-weight:600}
#sheet .shh #shclose{flex:0 0 48px}
#shlist{overflow:auto;-webkit-overflow-scrolling:touch;flex:1}
.pg{display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--edge)}
.pg .l{flex:1;min-width:0;padding:10px 4px;cursor:pointer}
.pg .l b{display:block;font-weight:500;font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pg .l span{display:block;font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pg button{flex:0 0 40px;min-height:40px;font-size:16px;color:var(--muted);background:none;border:0}
.empty{color:var(--muted);text-align:center;padding:30px 10px;font-size:14px}
#bact #kbd,#bact #star,#bact #pages{flex:0 0 52px;font-size:19px}
#bact{display:flex;gap:8px;padding:8px}
#ytlabel,#plbtn{display:none}
body.ytmode #goform,body.ytmode [data-nav=home],body.ytmode #star,body.ytmode #pages,body.ytmode #bcast,body.ytmode #btrans{display:none}
body.ytmode #ytlabel{display:block;flex:1;font-weight:600;padding-left:4px}
body.ytmode #plbtn{display:block}
body.ytmode #view{background:#000}
body.ytmode #bstat{display:none!important}
#bact button{min-height:46px}
#t-playlet.on{display:flex;flex-direction:column;height:calc(100vh - 62px - env(safe-area-inset-bottom) - env(safe-area-inset-top));
  height:calc(100dvh - 62px - env(safe-area-inset-bottom) - env(safe-area-inset-top))}
#plbar{display:flex;gap:8px;align-items:center;padding:8px 10px;font-size:14px;color:var(--muted)}
#plbar span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#plbar button{flex:0 0 44px;min-height:40px;font-size:20px;border-radius:12px;padding:0}
#plview{position:relative;flex:1;min-height:0;background:#000}
#plframe{position:absolute;inset:0;width:100%;height:100%;border:0;display:none;background:#0f0f0f}
#plmsg{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;text-align:center;
  padding:30px;color:var(--muted);font-size:14px;line-height:1.5}
#plmsg button{flex:0 0 auto;min-width:200px;padding:0 20px}
</style></head><body>
<div id="gone"><h2 style="color:var(--fg)">Open this from CapoCast</h2>
On your PC, open CapoCast, click <b>Phone remote</b>, and scan the code with your phone's camera. This page needs the link it shows.</div>
<div id="app">
<header><h1>CapoCast</h1><div id="now">Connecting…</div><div id="bar"><i id="fill"></i></div>
<div id="watch"><div class="t"><span>● Watching</span><b id="wtitle"></b></div><button id="wopen">Open</button><button id="wauto"></button></div>
<div id="note"></div></header>

<section class="tab on" id="t-remote">
  <div id="np"><div class="r"><b id="nptitle"></b><span id="npapp"></span></div>
    <input type="range" id="npseek" min="0" max="1" step="1" value="0" aria-label="Position">
    <div class="tm"><span id="nppos">0:00</span><span id="npdur">0:00</span></div></div>
  <div class="row"><button data-seek="-10">⟲ 10s</button><button id="pp" class="go" data-key="Play">⏯</button><button data-seek="10">10s ⟳</button></div>
  <div class="pad">
    <span></span><button data-key="Up" data-rep>▲</button><span></span>
    <button data-key="Left" data-rep>◀</button><button class="ok" data-key="Select">OK</button><button data-key="Right" data-rep>▶</button>
    <span></span><button data-key="Down" data-rep>▼</button><span></span>
  </div>
  <div class="row"><button data-key="Back">← Back</button><button data-key="Home">⌂ Home</button><button data-key="Info">✱ Options</button></div>
  <div class="row"><button data-key="Rev">⏪</button><button data-key="InstantReplay">↺ Replay</button><button data-key="Fwd">⏩</button></div>
  <div class="row"><button data-vol="-2" data-rep>🔉 −</button><button data-key="VolumeMute" id="mute">🔇</button><button data-vol="2" data-rep>🔊 +</button></div>
  <div class="hint" id="volnote"></div>
  <label for="typing">Type on the TV</label>
  <div class="row" style="margin-top:0"><input id="typing" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="Search or sign-in text">
    <button class="go" id="sendtext" style="flex:0 0 84px">Send</button></div>
  <div class="row"><button class="small" data-key="Backspace" data-rep>⌫ Delete</button><button class="small" data-key="Enter">⏎ Enter</button></div>
</section>

<section class="tab" id="t-browse">
  <div id="bbar">
    <button data-nav="back" aria-label="Back">‹</button><button data-nav="forward" aria-label="Forward">›</button>
    <form id="goform" action="javascript:void(0)"><input id="addr" type="text" inputmode="url" enterkeyhint="go" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="Search or enter address"></form>
    <button data-nav="reload" aria-label="Reload">⟳</button><button data-nav="home" aria-label="Home page">⌂</button>
    <span id="ytlabel">YouTube</span><button id="plbtn" aria-label="Playlet settings">⚙</button>
  </div>
  <div id="view"><img id="shot" alt="" draggable="false"><div id="vmsg">Opening the browser on your PC…</div><div id="bstat"></div>
    <div id="kb"><input id="kbtext" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" enterkeyhint="send" placeholder="Type into the page">
      <button id="kbsend" class="go">Send</button><button id="kbenter">⏎</button><button id="kbdel">⌫</button></div></div>
  <div id="btrans"><button data-skip="-10">⟲ 10s</button><button id="bpp">⏯</button><button data-skip="10">10s ⟳</button></div>
  <div id="bact"><button id="kbd" aria-label="Keyboard">⌨</button><button id="star" aria-label="Bookmark this page">☆</button><button id="pages" aria-label="Bookmarks and history">☰</button><button id="bcast" class="go">Cast this page</button></div>
</section>

<section class="tab" id="t-playlet">
  <div id="plbar"><button id="plback" aria-label="Back to YouTube">‹</button><span id="plwho">Playlet settings</span><button id="plhome" aria-label="Back to Playlet's home">⌂</button></div>
  <div id="plview"><iframe id="plframe" title="Playlet" allow="clipboard-write"></iframe>
    <div id="plmsg"><div id="plmsgtext">Looking for Playlet on the TV…</div><button class="go" id="plopen" style="display:none">Open Playlet on the TV</button></div></div>
</section>

<section class="tab" id="t-cast">
  <label for="link">Link to play on the TV</label>
  <input id="link" type="url" inputmode="url" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="Paste a video or page link">
  <div class="row"><button class="small" id="paste">Paste</button><button class="go" id="cast">Send to TV</button></div>
  <div class="hint">YouTube links open straight in Playlet on the TV. Other sites are opened in CapoCast's ad-blocking browser on your PC and cast from there, so CapoCast has to be running on the PC.</div>
  <div class="row"><button id="stopcast">Stop casting</button></div>
  <div class="hint">From other apps: <a id="setuplink" href="#" style="color:var(--accent2)">make "Cast to TV" appear in the Share menu</a>.</div>
</section>

<section class="tab" id="t-apps">
  <div class="hint">Tap an app to open it on the TV.</div>
  <div class="apps" id="apps"></div>
</section>
</div>
<div id="sheet"><div class="sh"><div class="shh"><button data-pg="bookmarks" class="on">Bookmarks</button><button data-pg="history">History</button><button id="shclose" aria-label="Close">✕</button></div>
  <div id="shlist"></div><button class="small" id="shclear" style="display:none;margin-top:8px;flex:0 0 auto">Clear history</button></div></div>
<nav id="nav"><button data-tab="remote" class="on"><b>◉</b>Remote</button><button data-tab="browse"><b>◍</b>Browse</button><button data-tab="yt"><b>▶</b>YouTube</button><button data-tab="cast"><b>⇪</b>Cast</button><button data-tab="apps"><b>▦</b>Apps</button></nav>
<script>
"use strict";
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
let TOKEN="__TOKEN__";
try{ if(TOKEN) localStorage.setItem("capo_k",TOKEN); else TOKEN=localStorage.getItem("capo_k")||""; }catch(e){}
if(!TOKEN){ $("#app").style.display="none"; $("#nav").style.display="none"; $("#gone").style.display="block"; }
else { $("#manifest").href="/manifest.webmanifest?k="+encodeURIComponent(TOKEN); }

let noteTimer=0;
function note(text,ok){ const n=$("#note"); n.textContent=text||""; n.className=ok?"ok":""; clearTimeout(noteTimer); if(text) noteTimer=setTimeout(()=>n.textContent="",7000); }
async function api(path,body){
  const opt={headers:{"X-Token":TOKEN}};
  if(body!==undefined){ opt.method="POST"; opt.headers["Content-Type"]="application/json"; opt.body=JSON.stringify(body); }
  let r; try{ r=await fetch(path,opt); }catch(e){ throw new Error("Can't reach your PC. Is CapoCast running?"); }
  const data=r.headers.get("content-type")?.includes("json")?await r.json():null;
  if(!r.ok) throw new Error((data&&data.error)||("Error "+r.status));
  return data;
}
async function act(path,body){ try{ const d=await api(path,body); return d; }catch(e){ note(e.message); } }
const fmt=s=>{s=Math.max(0,Math.floor(s));return Math.floor(s/60)+":"+String(s%60).padStart(2,"0");};

async function poll(){
  try{
    const s=await api("/api/state");
    const tv=s.tv;
    if(!tv) $("#now").textContent="Can't reach the Roku";
    else if(tv.state==="none") $("#now").textContent="Nothing playing";
    else{
      const lab=tv.state==="play"?"Playing":tv.state==="pause"?"Paused":tv.state[0].toUpperCase()+tv.state.slice(1);
      $("#now").innerHTML="<b>"+esc(tv.app||"Roku")+"</b> · "+lab+(tv.dur?" · "+fmt(tv.pos)+" / "+fmt(tv.dur):"");
    }
    $("#fill").style.width=tv&&tv.dur?Math.min(100,100*tv.pos/tv.dur)+"%":"0";
    $("#pp").textContent=tv&&tv.state==="play"?"⏸":"▶";
    const v=s.volume||{};
    $("#volnote").textContent=v.where==="pc"?"Sound is playing on the PC, so these change the PC volume"+(v.level!=null?" ("+v.level+"%)":"")+".":
      (v.level!=null?"TV volume "+v.level+(v.muted?" · muted":""):"");
    if(s.note) note(s.note.text,s.note.ok);
    watching(s.watching, tv);
    nowPlaying(s.now);
  }catch(e){ $("#now").textContent=e.message; }
  setTimeout(poll,1500);
}
// The now-playing card: title, and a bar you can drag to jump to a time
let seekHold=0, scrubbing=false;
const seek=$("#npseek");
function nowPlaying(n){
  const box=$("#np");
  if(!n){ box.style.display="none"; return; }
  box.style.display="block";
  $("#nptitle").textContent=n.title||n.app; $("#npapp").textContent=n.app+(n.state==="pause"?" · paused":"");
  $("#npdur").textContent=n.dur?fmt(n.dur):"";
  seek.disabled=!n.seekable;
  if(!scrubbing&&Date.now()>seekHold){ seek.max=Math.max(1,Math.floor(n.dur||1)); seek.value=Math.min(n.pos,n.dur||n.pos); $("#nppos").textContent=fmt(n.pos); }
}
seek.addEventListener("input",()=>{ scrubbing=true; $("#nppos").textContent=fmt(+seek.value); });
seek.addEventListener("change",async()=>{
  scrubbing=false; seekHold=Date.now()+3000;
  const d=await act("/api/seekto",{t:+seek.value}); if(d) note("Jumping to "+fmt(+seek.value),true);
});

// What the PC is casting right now: shown on every tab, and (if "Auto-open" is on) the phone jumps to it when it starts.
let follow=true, lastWatch=null;
try{ follow=localStorage.getItem("capo_follow")!=="0"; }catch(e){}
function paintAuto(){ $("#wauto").textContent="Auto-open: "+(follow?"on":"off"); }
paintAuto();
$("#wauto").onclick=()=>{ follow=!follow; try{ localStorage.setItem("capo_follow",follow?"1":"0"); }catch(e){} paintAuto(); };
const goBrowse=()=>$("#nav button[data-tab=browse]").click();
$("#wopen").onclick=goBrowse;
function watching(w,tv){
  const box=$("#watch");
  if(!w){ box.style.display="none"; lastWatch=0; $("#bpp").textContent="⏯"; return; }
  box.style.display="flex";
  let name=w.title; if(!name){ try{ name=new URL(w.url).hostname; }catch(e){ name="the PC"; } }
  $("#wtitle").textContent=name;
  $("#bpp").textContent=tv&&tv.state==="play"?"⏸":"▶";
  if(w.id!==lastWatch){ lastWatch=w.id; if(follow&&!browsing) goBrowse(); }
}
function esc(t){return String(t).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}

// buttons: a tap sends once; arrows, volume and delete repeat while held
$$("button[data-key],button[data-vol],button[data-seek]").forEach(b=>{
  const fire=()=>{
    if(b.dataset.key) act("/api/key",{name:b.dataset.key});
    else if(b.dataset.vol) act("/api/volume",{step:+b.dataset.vol});
    else act("/api/seek",{delta:+b.dataset.seek});
  };
  let t1=0,t2=0;
  const stop=()=>{clearTimeout(t1);clearInterval(t2);b.classList.remove("hot");};
  b.addEventListener("pointerdown",e=>{ e.preventDefault(); b.classList.add("hot"); fire();
    if("rep" in b.dataset) t1=setTimeout(()=>{t2=setInterval(fire,130);},380); });
  ["pointerup","pointerleave","pointercancel"].forEach(ev=>b.addEventListener(ev,stop));
  b.addEventListener("contextmenu",e=>e.preventDefault());
});

$("#sendtext").onclick=async()=>{ const i=$("#typing"); if(!i.value) return; const d=await act("/api/text",{text:i.value}); if(d){ i.value=""; } };
$("#typing").addEventListener("keydown",e=>{ if(e.key==="Enter"){ e.preventDefault(); $("#sendtext").click(); } });
$("#paste").onclick=async()=>{ try{ $("#link").value=await navigator.clipboard.readText(); }catch(e){ note("Long-press the box and choose Paste."); } };
$("#cast").onclick=async()=>{
  const u=$("#link").value.trim(); if(!u) return note("Paste a link first.");
  note("Sending…",true); const d=await act("/api/cast",{url:u}); if(d){ note(d.message||"Sent",true); $("#link").value=""; }
};
$("#stopcast").onclick=async()=>{ const d=await act("/api/stop",{}); if(d) note("Stopped",true); };

// The YouTube tab is the real youtube.com, in the PC's browser (the Browse tab's picture), drawn black. Tapping a video
// there plays it on the TV through Playlet instead of in the browser. Playlet's own settings page is one tap away (⚙).
let ytSub=false, curTab="remote";
function showTab(tab){
  curTab=tab;
  const yt=tab==="yt", sec=yt?(ytSub?"playlet":"browse"):tab;
  $$("#nav button").forEach(x=>x.classList.toggle("on",x.dataset.tab===tab));
  $$(".tab").forEach(t=>t.classList.toggle("on",t.id==="t-"+sec));
  document.body.classList.toggle("browsing",sec==="browse"||sec==="playlet");
  document.body.classList.toggle("ytmode",yt&&!ytSub);
  if(tab==="apps") loadApps();
  browse(sec==="browse");
  ytSession(yt&&!ytSub);
  plShow(yt&&ytSub);
}
$$("#nav button").forEach(b=>b.onclick=()=>{ if(b.dataset.tab==="yt") ytSub=false; showTab(b.dataset.tab); });
$("#plbtn").onclick=()=>{ ytSub=true; showTab("yt"); };
$("#plback").onclick=()=>{ ytSub=false; showTab("yt"); };

// While this tab is open the phone renews a switch on the PC (every few seconds; it lapses by itself if the phone goes away):
// YouTube is drawn black and a tapped video goes to the TV. The first time, it also opens YouTube if the browser is elsewhere.
let ytBeat=0, ytWasOn=false, ytGo=false;
const ytHost=u=>/(^|\.)youtube\.com$|^accounts\.google\./.test(host(u||""));
async function ytTick(){
  try{
    let go="";
    if(ytGo){
      const i=await api("/api/web/info");
      if(i.ready){ if(!ytHost(i.url)) go="https://www.youtube.com/"; ytGo=false; }
    }
    await api("/api/web/youtube",{on:true,go});
  }catch(e){}
}
function ytSession(on){
  clearInterval(ytBeat); ytBeat=0;
  if(on){ ytWasOn=true; ytGo=true; ytTick(); ytBeat=setInterval(ytTick,5000); }
  else if(ytWasOn){ ytWasOn=false; api("/api/web/youtube",{on:false}).catch(()=>{}); }
}

// ---- Playlet's own web app (its settings, and the feed of the account signed in on the Roku). The Roku serves it, on its own,
// while Playlet is open on the TV, so this only has to find it, open Playlet if it isn't, and show it.
const plf=$("#plframe");
let plOn=false, plRun=0, plLoaded=false, plDown=false, plBase="";
function plMsg(text,button){ $("#plmsg").style.display=text?"flex":"none"; $("#plmsgtext").textContent=text||""; $("#plopen").style.display=button?"block":"none"; }
function plShow(on){ plOn=on; plRun++; if(on) plLoop(plRun); }
async function plLoop(id){
  while(plOn&&id===plRun){
    let p;
    try{ p=await api("/api/playlet"); }
    catch(e){ plMsg(e.message,false); await sleep(2500); continue; }
    if(!plOn||id!==plRun) return;
    if(p.up){
      plBase=p.url;
      $("#plwho").textContent="Playlet"+(p.account?" · signed in as "+p.account:"");
      if(!plLoaded||plDown){ plf.src=plBase; plLoaded=true; plDown=false; }
      plf.style.display="block"; plMsg("");
      return;                                        // it stays open while you use it; coming back here checks again
    }
    if(plLoaded) plDown=true;                        // Playlet was closed on the TV: reload it once it is back
    plf.style.display="none";
    plMsg("Playlet isn't open on the TV. It has to be open for its settings to show here.",true);
    $("#plopen").dataset.id=p.id||"";
    await sleep(2000);
  }
}
$("#plopen").onclick=async()=>{
  const id=$("#plopen").dataset.id; if(!id) return;
  plMsg("Opening Playlet on the TV…",false);
  await act("/api/launch",{id});
};
$("#plhome").onclick=()=>{ if(plBase&&plLoaded) plf.src=plBase; else plShow(true); };

// ---- Browse: the PC's browser as a live picture. Taps, drags and typing go back to it.
const view=$("#view"), shot=$("#shot");
let browsing=false, runId=0, seq=0, sx=1, sy=1, lastUrl="";
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function browse(on){
  browsing=on; runId++; clearInterval(fling); touch=null;
  if(on){ seq=0; frames(runId); infoLoop(runId); }
}
document.addEventListener("visibilitychange",()=>{ if(browsing){ runId++; if(!document.hidden){ seq=0; frames(runId); infoLoop(runId); } } });
async function frames(id){
  while(browsing&&id===runId){
    const w=Math.round(view.clientWidth), h=Math.round(view.clientHeight);
    if(w<50||h<50){ await sleep(300); continue; }
    try{
      const r=await fetch("/api/web/frame?after="+seq+"&w="+w+"&h="+h,{headers:{"X-Token":TOKEN}});
      if(id!==runId) return;
      if(r.status===200){
        seq=+r.headers.get("X-Seq");
        sx=(+r.headers.get("X-View-W")||w)/view.clientWidth; sy=(+r.headers.get("X-View-H")||h)/view.clientHeight;
        const url=URL.createObjectURL(await r.blob()), old=shot.src;
        shot.onload=()=>{ if(old.startsWith("blob:")) URL.revokeObjectURL(old); $("#vmsg").style.display="none"; };
        shot.src=url;
      } else if(r.status!==204){ await sleep(1200); }
    }catch(e){ await sleep(1500); }
  }
}
async function infoLoop(id){
  while(browsing&&id===runId){
    try{
      const i=await api("/api/web/info");
      if(id!==runId) return;
      if(document.activeElement!==$("#addr")&&i.url&&i.url!==lastUrl){ $("#addr").value=i.url; lastUrl=i.url; }
      if(!i.alive||!i.ready){ $("#vmsg").style.display="flex"; $("#vmsg").textContent="Opening the browser on your PC…"; }
      const bits=[]; if(i.note) bits.push(i.note.text); else if(i.streams) bits.push(i.streams+" video stream"+(i.streams>1?"s":"")+" found");
      const st=$("#bstat"); st.textContent=bits.join(" · "); st.style.display=bits.length?"block":"none";
      st.style.color=i.note&&!i.note.ok?"var(--amber)":"var(--green)";
      $("#bcast").textContent=i.busy?"Cancel":i.following?"Recast this page":"Cast this page";
      if(bookmarked!==!!i.bookmarked){ bookmarked=!!i.bookmarked; paintStar(); }
    }catch(e){}
    await sleep(1000);
  }
}
$$("[data-nav]").forEach(b=>b.addEventListener("click",()=>act("/api/web/nav",{action:b.dataset.nav})));
$("#goform").addEventListener("submit",async e=>{
  e.preventDefault(); const u=$("#addr").value.trim(); if(!u) return;
  $("#addr").blur(); lastUrl=""; await act("/api/web/nav",{action:"go",url:u});
});
$("#addr").addEventListener("focus",()=>setTimeout(()=>$("#addr").select(),0));
$$("[data-skip]").forEach(b=>b.addEventListener("click",()=>act("/api/web/transport",{action:"skip",value:+b.dataset.skip})));
$("#bpp").onclick=()=>act("/api/web/transport",{action:"toggle",value:0});
let bookmarked=false;
$("#star").onclick=async()=>{ const d=await act("/api/web/bookmark",{action:"toggle"}); if(d){ bookmarked=d.bookmarked; paintStar(); note(bookmarked?"Bookmarked":"Bookmark removed",true); } };
function paintStar(){ $("#star").textContent=bookmarked?"★":"☆"; $("#star").style.color=bookmarked?"var(--amber)":""; }
let sheetPage="bookmarks", pagesData=null;
async function openSheet(){ $("#sheet").classList.add("on"); pagesData=await act("/api/web/pages"); renderSheet(); }
function closeSheet(){ $("#sheet").classList.remove("on"); }
function host(u){ try{ return new URL(u).hostname.replace(/^www\./,""); }catch(e){ return u; } }
function renderSheet(){
  $$("#sheet [data-pg]").forEach(b=>b.classList.toggle("on",b.dataset.pg===sheetPage));
  $("#shclear").style.display=sheetPage==="history"&&pagesData&&pagesData.history.length?"block":"none";
  const list=$("#shlist"); list.innerHTML="";
  const rows=pagesData?pagesData[sheetPage]:[];
  if(!rows.length){ list.innerHTML='<div class="empty">'+(sheetPage==="bookmarks"?"No bookmarks yet. Tap ☆ on a page to save it.":"Nothing here yet.")+'</div>'; return; }
  rows.forEach(r=>{
    const row=document.createElement("div"); row.className="pg";
    row.innerHTML='<div class="l"><b></b><span></span></div>'+(sheetPage==="bookmarks"?'<button aria-label="Remove">✕</button>':'');
    row.querySelector("b").textContent=r.title||host(r.url); row.querySelector("span").textContent=host(r.url)+(r.title?"":"");
    row.querySelector(".l").onclick=()=>{ closeSheet(); lastUrl=""; act("/api/web/nav",{action:"go",url:r.url}); };
    const x=row.querySelector("button");
    if(x) x.onclick=async()=>{ await act("/api/web/bookmark",{action:"remove",url:r.url}); pagesData=await act("/api/web/pages"); renderSheet(); };
    list.appendChild(row);
  });
}
$("#pages").onclick=openSheet; $("#shclose").onclick=closeSheet;
$("#sheet").addEventListener("click",e=>{ if(e.target.id==="sheet") closeSheet(); });
$$("#sheet [data-pg]").forEach(b=>b.onclick=()=>{ sheetPage=b.dataset.pg; renderSheet(); });
$("#shclear").onclick=async()=>{ await act("/api/web/bookmark",{action:"clear_history"}); pagesData=await act("/api/web/pages"); renderSheet(); };
$("#setuplink").onclick=e=>{ e.preventDefault(); location.href="/setup?k="+encodeURIComponent(TOKEN); };
$("#bcast").onclick=async()=>{ const d=await act("/api/web/cast",{}); if(d) note(d.message,true); };
$("#kbd").onclick=()=>{ const k=$("#kb"); k.classList.toggle("on"); if(k.classList.contains("on")) $("#kbtext").focus(); else $("#kbtext").blur(); };
async function kbSend(){ const i=$("#kbtext"); if(!i.value) return true; const d=await act("/api/web/input",{type:"text",text:i.value}); if(d){ i.value=""; return true; } return false; }
$("#kbsend").onclick=kbSend;
$("#kbenter").onclick=async()=>{ if(await kbSend()) act("/api/web/input",{type:"key",name:"Enter"}); };
$("#kbtext").addEventListener("keydown",e=>{ if(e.key==="Enter"){ e.preventDefault(); $("#kbenter").click(); } });
(()=>{ const b=$("#kbdel"); let t1=0,t2=0; const fire=()=>act("/api/web/input",{type:"key",name:"Backspace"});
  const stop=()=>{clearTimeout(t1);clearInterval(t2);};
  b.addEventListener("pointerdown",e=>{ e.preventDefault(); fire(); t1=setTimeout(()=>{t2=setInterval(fire,110);},380); });
  ["pointerup","pointerleave","pointercancel"].forEach(ev=>b.addEventListener(ev,stop)); })();

// touch: a tap clicks, a drag scrolls (and keeps coasting when you let go)
let touch=null, acc={dx:0,dy:0,x:0,y:0}, busy=false, fling=0;
function at(t){ const r=view.getBoundingClientRect(); return {x:(t.clientX-r.left)*sx, y:(t.clientY-r.top)*sy}; }
async function flush(){
  if(busy||(!acc.dx&&!acc.dy)) return; busy=true; const a={...acc}; acc.dx=acc.dy=0;
  try{ await api("/api/web/input",{type:"scroll",x:a.x,y:a.y,dx:a.dx,dy:a.dy}); }catch(e){}
  busy=false; if(acc.dx||acc.dy) flush();
}
function scrollBy(dx,dy,x,y){ acc.dx+=dx; acc.dy+=dy; acc.x=x; acc.y=y; flush(); }
view.addEventListener("touchstart",e=>{
  e.preventDefault(); clearInterval(fling);
  if(e.touches.length!==1){ touch=null; return; }
  const p=at(e.touches[0]); touch={x0:p.x,y0:p.y,lx:p.x,ly:p.y,moved:false,hist:[]};
},{passive:false});
view.addEventListener("touchmove",e=>{
  e.preventDefault(); if(!touch||e.touches.length!==1) return;
  const p=at(e.touches[0]);
  if(!touch.moved&&Math.hypot(p.x-touch.x0,p.y-touch.y0)<10*Math.max(sx,sy)) return;
  touch.moved=true; const dx=touch.lx-p.x, dy=touch.ly-p.y; touch.lx=p.x; touch.ly=p.y;
  touch.hist.push([performance.now(),dx,dy]); if(touch.hist.length>6) touch.hist.shift();
  scrollBy(dx,dy,p.x,p.y);
},{passive:false});
view.addEventListener("touchend",e=>{
  e.preventDefault(); const t=touch; touch=null; if(!t) return;
  if(!t.moved){ act("/api/web/input",{type:"tap",x:t.x0,y:t.y0}); return; }
  const now=performance.now(), recent=t.hist.filter(h=>now-h[0]<90); if(!recent.length) return;
  const span=Math.max(16,now-recent[0][0]);
  const cap=v=>Math.max(-40,Math.min(40,v));
  let vx=cap(recent.reduce((a,h)=>a+h[1],0)/span*16), vy=cap(recent.reduce((a,h)=>a+h[2],0)/span*16);   // px per 16 ms, capped
  fling=setInterval(()=>{ vx*=.94; vy*=.94; if(Math.hypot(vx,vy)<.6){ clearInterval(fling); return; } scrollBy(vx*2,vy*2,t.lx,t.ly); },32);
},{passive:false});
view.addEventListener("touchcancel",()=>{ touch=null; });
let appsLoaded=0;
async function loadApps(){
  if(Date.now()-appsLoaded<30000) return;
  const d=await act("/api/apps"); if(!d) return; appsLoaded=Date.now();
  const box=$("#apps"); box.innerHTML="";
  d.apps.forEach(a=>{
    const b=document.createElement("button");
    b.innerHTML='<img alt="" loading="lazy" src="/api/icon/'+encodeURIComponent(a.id)+'?k='+encodeURIComponent(TOKEN)+'"><span>'+esc(a.name)+'</span>';
    b.onclick=async()=>{ const r=await act("/api/launch",{id:a.id}); if(r) note("Opening "+a.name,true); };
    box.appendChild(b);
  });
}
if(TOKEN) poll();
</script></body></html>
"""
