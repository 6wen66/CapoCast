"""Shows what's playing on the TV as your Discord status ("Rich Presence").

Discord's desktop app listens on a local named pipe; an app sends it an "activity" over that pipe. Nothing goes to
any server of ours, and nothing is sent unless the Discord toggle is on. Discord only shows a status for a
registered application, so you make a free one at https://discord.com/developers/applications and paste its
Application ID into CapoCast; the application's name is what Discord shows after "Playing".

Everything happens on one background thread, so a slow or missing Discord can never hold up a cast.
"""
import json
import os
import queue
import re
import struct
import threading
import time
import uuid
from urllib.parse import urlparse

PIPES = range(10)               # Discord uses discord-ipc-0 to -9, whichever was free when it started
HANDSHAKE, FRAME, CLOSE = 0, 1, 2
RETRY = 10.0                    # seconds between attempts to find Discord while a status is waiting
MAX_TEXT = 128                  # Discord's limit for each line
MAX_IMAGE_URL = 256             # not documented by Discord; a precaution against oversized URLs
MIN_GAP = 4.0                   # seconds between updates: Discord allows 5 per 20 s, and the newest one wins


def pipe_path(n):
    return rf"\\.\pipe\discord-ipc-{n}"


def discord_running():
    """Whether the Discord desktop app has its pipe open (so a status could be shown)."""
    return any(os.path.exists(pipe_path(n)) for n in PIPES)


def valid_id(text):
    return isinstance(text, str) and text.isdigit() and 15 <= len(text) <= 21


def pack(op, payload):
    data = json.dumps(payload, separators=(",", ":")).encode()
    return struct.pack("<II", op, len(data)) + data


def _read_exact(conn, n):
    out = b""
    while len(out) < n:
        chunk = conn.read(n - len(out))
        if not chunk:
            raise ConnectionError("Discord closed the pipe")
        out += chunk
    return out


def read_packet(conn):
    op, size = struct.unpack("<II", _read_exact(conn, 8))
    body = _read_exact(conn, size)
    return op, (json.loads(body) if body else {})


def open_pipe():
    """A connection to the first Discord pipe that answers, or None."""
    for n in PIPES:
        try:
            return open(pipe_path(n), "r+b", buffering=0)
        except OSError:
            continue
    return None


def clean_image(url):
    """An https image address Discord can fetch, or None. Anything else is left out rather than risk a refusal."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url.startswith("https://") or len(url) > MAX_IMAGE_URL or any(c.isspace() for c in url):
        return None
    return url


def clip(text, fallback):
    text = " ".join(str(text or "").split())
    if len(text) < 2:
        return fallback
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"


SECOND_LEVEL = {"co", "com", "org", "net", "gov", "edu", "ac"}      # the "co" in bbc.co.uk
# Words that only say "this is a streaming page" ("Watch Frieren Episode 5 English Sub Online Free"), not what it is.
NOISE = {"watch", "online", "free", "hd", "stream", "streaming", "english", "eng", "sub", "dub", "subbed", "dubbed",
         "subs", "dubs", "full", "movie", "movies", "anime", "tv", "show", "shows", "series", "now", "on", "in", "for",
         "4k", "1080p", "720p", "episodes"}
STRONG = {"sub", "dub", "subbed", "dubbed", "subs", "dubs", "hd", "4k", "1080p", "720p"}     # noise even when alone
LANG = {"english", "eng"} | {"sub", "dub", "subbed", "dubbed", "subs", "dubs"}
TITLE_SPLIT = re.compile(r"\s+[-–—|·•»]\s+|\s*\|\s*")


def site_label(page_url, og_site=""):
    """A short name for the site a video is on: the page's own og:site_name if it gave a tidy one, else the address's
    main word ('miruro.cx' -> 'Miruro'). '' if there is neither."""
    og = " ".join(str(og_site or "").split())
    if 2 <= len(og) <= 24 and "://" not in og:
        return og
    try:
        host = (urlparse(page_url).hostname or "").lower()
    except ValueError:
        return ""
    labels = [p for p in host.split(".") if p and p != "www"]
    if len(labels) < 2 or host.replace(".", "").isdigit():
        return host if host and host != "localhost" else ""
    stem = labels[-3] if len(labels) >= 3 and labels[-2] in SECOND_LEVEL and len(labels[-1]) == 2 else labels[-2]
    return stem[:1].upper() + stem[1:]


def _squash(text):
    return "".join(c for c in text.lower() if c.isalnum())


def clean_title(title, site="", page_url=""):
    """What's being watched, from a page title: 'Watch Frieren Episode 5 English Sub Online - Miruro' -> 'Frieren
    Episode 5'. Takes off the site's name, throwaway parts such as 'Watch Free Online' and a leading 'Watch'/trailing
    'English Sub' (a lone 'Watch Dogs' is left alone). Returns the title as it came if nothing would be left."""
    original = re.sub(r"^\(\d+\)\s*", "", " ".join(str(title or "").split()))      # '(3) ' is an unread count
    if not original:
        return ""
    tokens = {t for t in (_squash(site), _squash(site_label(page_url)) if page_url else "") if len(t) >= 3}
    kept, trimmed = [], False
    for seg in TITLE_SPLIT.split(original):
        words = seg.split()
        if not words:
            continue
        flat = _squash(seg)
        if any(t in flat and len(flat) <= len(t) + 14 for t in tokens):
            trimmed = True                                  # 'Miruro', 'Watch on Miruro.tv'
            continue
        if all(_squash(w) in NOISE or not _squash(w) for w in words):
            trimmed = True                                  # 'Watch Anime Free Online'
            continue
        end = len(words)
        while end > 1 and _squash(words[end - 1]) in NOISE:
            end -= 1
        run = [_squash(w) for w in words[end:]]
        if run and run[0] == "online" and len(run) > 1 and run[1] in LANG:
            end += 1                                        # 'Sword Art Online English Sub': Online is the title's
            run = run[1:]
        if run and (len(run) >= 2 or run[0] in STRONG):     # a lone 'Online' or 'Movie' could be part of the title
            words, trimmed = words[:end], True              # 'English Sub', 'Free Online', 'HD'
        kept.append(" ".join(words))
    if trimmed and kept:                                    # 'Watch Ghost in the Shell - Miruro': the site came off *after*
        first = kept[0].split()                             # this segment was read, so the leading Watch is dealt with here
        if len(first) > 1 and _squash(first[0]) == "watch":
            kept[0] = " ".join(first[1:])
    cleaned = " · ".join(kept)
    return clip(cleaned, original) if len(_squash(cleaned)) >= 2 else clip(original, "")


def looks_like_logo(url):
    """A site's own logo or icon rather than a picture of what's playing (so a poster is the better choice)."""
    path = urlparse(str(url or "")).path.lower()
    return any(w in path for w in ("logo", "favicon", "apple-touch", "/icon", "default-og", "placeholder"))


MAX_PROBE_BYTES = 262_144       # enough of an image to read its size from the header
BAD_TYPES = ("image/svg", "image/avif", "image/x-icon", "image/vnd.microsoft.icon")


def probe_image(url, timeout=3.0, opener=None):
    """(width, height) of the picture at `url` if it can be fetched the way Discord's servers will (a plain GET, no
    cookies, no Referer, a browser-like agent) and is a picture Discord can show; None otherwise. Reads only the start."""
    try:
        from PIL import ImageFile
        if opener is None:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*,*/*;q=0.5"})
            resp = urllib.request.urlopen(req, timeout=timeout)
        else:
            resp = opener(url)
        with resp:
            kind = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if getattr(resp, "status", 200) != 200 or not kind.startswith("image/") or kind.startswith(BAD_TYPES):
                return None
            if int(resp.headers.get("Content-Length") or 0) > 8_000_000:
                return None                                 # too heavy to be a thumbnail
            parser, got = ImageFile.Parser(), 0
            while got < MAX_PROBE_BYTES:
                chunk = resp.read(16384)
                if not chunk:
                    break
                got += len(chunk)
                parser.feed(chunk)
                if parser.image is not None:
                    return parser.image.size
        return None
    except Exception:
        return None


def pick_image(candidates, probe=probe_image, deadline=9.0, log=lambda m: None):
    """The best thumbnail among page-supplied picture addresses (best guesses first), or ''. Each one is fetched like
    Discord will fetch it, and one that can't be is passed over: otherwise Discord shows its grey placeholder. A poster
    or 16:9 shape wins over a thin banner (which Discord crops to a square) when there is one."""
    end, valid, seen = time.time() + deadline, [], set()
    for url in candidates:
        url = clean_image(url)
        if not url or url in seen or looks_like_logo(url):
            continue
        seen.add(url)
        if time.time() > end or len(seen) > 7:
            break
        size = probe(url)
        log(f"discord picture {'ok ' + 'x'.join(map(str, size)) if size else 'unusable'}: {url[:110]}")
        if not size:
            continue
        if 0.55 <= size[0] / max(size[1], 1) <= 2.0:
            return url
        valid.append(url)
    return valid[0] if valid else ""


_info_cache = {}


def youtube_info(video_id, timeout=4.0, fetch=None):
    """(channel, title) of a YouTube video, looked up by its id through YouTube's public oEmbed address; ('', '') if it
    can't be had. The page's own title can't be trusted for this: YouTube changes it a moment *after* a new video
    starts, so reading it then gives the previous video's name."""
    if not isinstance(video_id, str) or not video_id.replace("-", "").replace("_", "").isalnum():
        return "", ""
    if video_id in _info_cache:
        return _info_cache[video_id]
    try:
        if fetch is None:
            import urllib.parse
            import urllib.request
            url = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(
                f"https://www.youtube.com/watch?v={video_id}", safe="")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read(200_000))
        else:
            data = fetch(video_id)
        info = (str(data.get("author_name") or "").strip(), str(data.get("title") or "").strip())
    except Exception:
        return "", ""                   # not cached, so a later try can work
    if info[1]:
        if len(_info_cache) > 200:
            _info_cache.clear()
        _info_cache[video_id] = info
    return info


class Presence:
    """set() and clear() return at once; the thread does the talking and keeps trying if Discord isn't open yet."""

    def __init__(self, client_id, log=lambda m: None, opener=open_pipe, min_gap=MIN_GAP):
        self.client_id, self.log, self.opener, self.min_gap = str(client_id), log, opener, min_gap
        self.connected = False
        self.images_ok = True           # turned off for good if Discord refuses a status that has a picture
        self._last_sent = 0.0
        self._conn = None
        self._activity = None           # what should be showing
        self._next_try = 0.0
        self._q = queue.Queue()
        self._thread = None
        self._stopping = False

    # ---- what callers use -----------------------------------------------------------------
    def set(self, details, state, since=None, image=None, image_text=None):
        """image: an https picture address (a thumbnail), shown large beside the text, if Discord accepts it."""
        activity = {"details": clip(details, "Watching"), "state": clip(state, "On the TV"),
                    "timestamps": {"start": int(since or time.time())}, "instance": False}
        image = clean_image(image)
        if image:
            activity["assets"] = {"large_image": image, "large_text": clip(image_text or details, "Watching")}
        self._send(activity)

    def clear(self):
        self._send(None)

    def stop(self):
        """Clear the status and let go of Discord."""
        self._stopping = True
        self._q.put(("stop", None))
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    # ---- the thread --------------------------------------------------------------------------
    def _send(self, activity):
        if not valid_id(self.client_id) or self._stopping:
            return
        self._q.put(("set", activity))
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="discord-presence", daemon=True)
            self._thread.start()

    def _run(self):
        dirty = False                                   # Discord hasn't been told the current wish yet
        while True:
            try:
                kind, activity = self._q.get(timeout=1.0)
            except queue.Empty:
                kind = None
            if kind == "stop":
                if self.connected:
                    self._try(None)                     # best effort: take the status down
                self._drop()
                return
            if kind == "set":
                self._activity, dirty = activity, True
            if dirty and self._activity is None and not self.connected:
                dirty = False                           # nothing was ever shown, so there's nothing to take down
            now = time.time()
            if dirty and now >= self._next_try and now - self._last_sent >= self.min_gap:
                dirty = not self._try(self._activity)
                if not dirty:
                    self._last_sent = time.time()

    def _try(self, activity):
        """Tell Discord. True if it accepted it."""
        try:
            if self._conn is None:
                self._connect()
            reply = self._set_activity(activity)
            if reply.get("evt") == "ERROR" and activity and activity.get("assets"):
                # the picture is the likeliest thing to be turned down; the text alone is still worth showing
                self.log(f"discord refused the picture: {(reply.get('data') or {}).get('message', '')[:100]}")
                self.images_ok = False
                reply = self._set_activity(activity)
            if reply.get("evt") == "ERROR":
                self.log(f"discord refused the status: {(reply.get('data') or {}).get('message', '')[:100]}")
            return True
        except (OSError, ConnectionError, ValueError, struct.error) as e:
            if self.connected or self._conn is not None:
                self.log(f"discord link dropped: {e!r}")
            self._drop()
            self._next_try = time.time() + RETRY
            return False

    def _set_activity(self, activity):
        if activity and not self.images_ok:
            activity = {k: v for k, v in activity.items() if k != "assets"}
        self._conn.write(pack(FRAME, {"cmd": "SET_ACTIVITY", "nonce": str(uuid.uuid4()),
                                      "args": {"pid": os.getpid(), "activity": activity}}))
        _, reply = read_packet(self._conn)
        return reply

    def _connect(self):
        conn = self.opener()
        if conn is None:
            raise ConnectionError("Discord isn't open")
        try:
            conn.write(pack(HANDSHAKE, {"v": 1, "client_id": self.client_id}))
            op, reply = read_packet(conn)
        except Exception:
            try:
                conn.close()
            except OSError:
                pass
            raise
        if op == CLOSE or reply.get("evt") == "ERROR":
            conn.close()
            raise ConnectionError((reply.get("message") or "Discord turned the application ID down")[:100])
        self._conn, self.connected = conn, True
        self.log("discord: connected")

    def _drop(self):
        conn, self._conn, self.connected = self._conn, None, False
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass


class Status:
    """One Discord status shared by everything that can show one (the Web tab's casts, the YouTube tab's videos).

    Each source has its own on/off switch and the most recent one to show something is what Discord displays. A source
    can only take down its own status, so the YouTube tab stopping never wipes out a Web cast that started later."""

    def __init__(self, client_id="", log=lambda m: None, factory=Presence):
        self.client_id = client_id if valid_id(client_id) else ""
        self.log, self._factory = log, factory
        self.enabled = {}                       # source -> switched on
        self.showing = None                     # the source whose status is up
        self._presence = None
        self._lock = threading.Lock()

    def set_id(self, client_id):
        """Use a new Application ID (an empty or invalid one turns everything off)."""
        with self._lock:
            self.client_id = client_id if valid_id(client_id) else ""
            self._drop_presence()

    def is_on(self, source):
        return bool(self.enabled.get(source) and self.client_id)

    def enable(self, source, on):
        with self._lock:
            self.enabled[source] = bool(on)
        if not on:
            self.clear(source)

    def show(self, source, title, state, since=None, image=None, image_text=None):
        """Put `title` up as `source`'s status. Does nothing if that source is off or there is no Application ID."""
        with self._lock:
            if not self.is_on(source):
                return False
            if self._presence is None or self._presence.client_id != self.client_id:
                self._drop_presence()
                self._presence = self._factory(self.client_id, self.log)
            self.showing = source
            self._presence.set(title, state, since, image=image, image_text=image_text or title)
            return True

    def clear(self, source):
        """Take `source`'s status down, if it is the one showing."""
        with self._lock:
            if self.showing == source and self._presence is not None:
                self._presence.clear()
                self.showing = None

    def stop(self):
        with self._lock:
            self._drop_presence()

    def _drop_presence(self):
        presence, self._presence, self.showing = self._presence, None, None
        if presence is not None:
            presence.stop()


ID_PROMPT = ("Discord shows a status only for an application you create (free, takes a minute):\n\n"
             "1. Open discord.com/developers/applications and press New Application\n"
             "2. Name it what you want to see after \"Playing\" (for example CapoCast)\n"
             "3. Copy its Application ID from the General Information page and paste it here:")


def ask_application_id(parent):
    """Ask for the Application ID. Returns (id, problem): id is '' if cancelled or wrong, problem says why if wrong."""
    from tkinter import simpledialog
    text = (simpledialog.askstring("Discord status", ID_PROMPT, parent=parent) or "").strip()
    if not text:
        return "", ""
    if not valid_id(text):
        return "", "That doesn't look like an Application ID (it's a long number)."
    return text, ""
