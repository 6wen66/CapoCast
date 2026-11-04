"""CapoCast - GUI. Roku private-listening audio on this PC, plus a YouTube tab (embedded Chrome, auto-cast).

Reuses the protocol helpers from roku_listen.py. Instead of ffplay, audio is decoded by ffmpeg to raw PCM,
scaled by the volume slider in Python (audioop) and played with sounddevice, so volume works live.
"""
import asyncio
import base64
import ctypes
import audioop
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from tkinter import ttk

import sounddevice as sd
from PIL import Image, ImageTk
import websockets

import web_tab
import yt_embed
from types import SimpleNamespace
from yt_bridge import Bridge

from roku_listen import (
    DEFAULT_HOST, ECP_PORT, FFPLAY_PORT, RTCP_PORT, RTCP_RR, RTP_PORT, SDP,
    auth_response, check_support, local_ip_for, rtcp_app,
)

CONFIG = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "RokuListen.json")
CHUNK = 960 * 4  # 20 ms of 48 kHz stereo s16le
RR_EVERY = 1.0   # seconds between RTCP receiver reports; the Roku drops the stream if they're too sparse
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOG = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "RokuListen.log")
PLAYLET_ID = "693751"   # the Roku channel id of Playlet, which plays the YouTube tab's videos


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}" + chr(10))
    except OSError:
        pass


class Remote:
    """Transport controls over Roku's ECP (port 8060); Playlet's API (8888) gives exact seeking."""

    @staticmethod
    def _open(url, data=None, headers=None, timeout=3, method=None):
        method = method or ("POST" if data is not None else "GET")
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        return urllib.request.urlopen(req, timeout=timeout).read()

    @classmethod
    def key(cls, host, name):
        cls._open(f"http://{host}:{ECP_PORT}/keypress/{name}", data=b"")

    @classmethod
    def state(cls, host):
        """-> {state, app, pos, dur} in seconds, or None if nothing is playing / unreachable."""
        try:
            root = ET.fromstring(cls._open(f"http://{host}:{ECP_PORT}/query/media-player"))
        except Exception:
            return None
        st = root.get("state", "none")
        if st in ("none", "close", "stop"):
            return {"state": "none", "app": "", "pos": 0, "dur": 0}
        ms = lambda tag: (int((root.findtext(tag) or "0").split()[0]) / 1000)
        plugin = root.find("plugin")
        return {"state": st, "app": plugin.get("name", "") if plugin is not None else "",
                "pos": ms("position"), "dur": ms("duration")}

    @classmethod
    def seek(cls, host, delta):
        st = cls.state(host)
        if st and st["app"] == "Playlet" and st["dur"] > 0:
            queue = json.loads(cls._open(f"http://{host}:8888/api/queue"))
            vid = (queue.get("nowplaying") or {}).get("videoid")
            if vid:
                target = max(0, min(st["pos"] + delta, st["dur"] - 2))
                body = json.dumps({"videoId": vid, "timestamp": int(target)}).encode()
                cls._open(f"http://{host}:8888/api/queue/play", data=body,
                          headers={"Content-Type": "application/json", "X-Playlet": "1"})
                return
        cls.key(host, "Right" if delta > 0 else "Left")  # most other apps skip ~10s on the arrow keys

    @classmethod
    def cast(cls, host, video_id, timestamp=0, playlist_id=None):
        """Play a YouTube video in Playlet (same call the Chrome extension makes)."""
        body = {"videoId": video_id}
        if playlist_id:
            body["playlistId"] = playlist_id
        if timestamp > 3:
            body["timestamp"] = int(timestamp)
        send = lambda: cls._open(f"http://{host}:8888/api/queue/play", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Playlet": "1"})
        try:
            send()
        except urllib.error.HTTPError:
            raise
        except OSError:
            # Playlet's server only runs while Playlet is on screen. After a Web-tab cast (or any other app) the
            # TV is somewhere else, so open Playlet and try once more.
            cls._ensure_playlet(host)
            send()

    @classmethod
    def _ensure_playlet(cls, host):
        cls._open(f"http://{host}:{ECP_PORT}/launch/{PLAYLET_ID}", data=b"")
        for _ in range(24):
            time.sleep(0.5)
            try:
                cls._open(f"http://{host}:8888/api/state", timeout=2)
                return
            except OSError:
                pass

    @classmethod
    def active_app(cls, host):
        """ECP id of the app on screen ('dev' is the sideloaded RokuCast channel), or ''."""
        try:
            app = ET.fromstring(cls._open(f"http://{host}:{ECP_PORT}/query/active-app")).find("app")
            return app.get("id", "") if app is not None else ""
        except Exception:
            return ""

    @classmethod
    def cast_url(cls, host, url):
        """Play an http(s) URL in the RokuCast channel: launch it, or if it's already running hand it the URL."""
        enc = urllib.parse.quote(url, safe="")
        running = cls.active_app(host) == "dev"
        path = f"/input?url={enc}" if running else f"/launch/dev?url={enc}"
        try:
            cls._open(f"http://{host}:{ECP_PORT}{path}", data=b"", timeout=6)
        except urllib.error.HTTPError as e:
            if e.code == 404 and not running:
                raise RuntimeError("The RokuCast channel isn't installed on this Roku (the Web tab plays video "
                                   "through it).")
            if e.code == 403:
                raise RuntimeError("The Roku refused the command. On the Roku: Settings > System > Advanced system "
                                   "settings > Control by mobile apps > Network access > Permissive.")
            raise RuntimeError(f"The Roku answered HTTP {e.code}.")
        except OSError as e:
            raise RuntimeError(f"Couldn't reach the Roku at {host}: {e}")

    @classmethod
    def close_player(cls, host):
        """Clear Playlet's queue, which also closes its player and returns to Playlet's home screen."""
        cls._open(f"http://{host}:8888/api/queue", headers={"X-Playlet": "1"}, method="DELETE")


class KeySender:
    """Sends keypresses to the Roku one at a time, in order.

    Typing needs a single queue: a thread per key (like the transport buttons use) lets the HTTP requests
    overtake each other, so "hello" could arrive as "hlelo".
    """

    def __init__(self, host_fn):
        self.host_fn, self.error = host_fn, ""
        self.q = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def key(self, name):
        self.q.put(name)

    def text(self, s):
        for ch in s:
            if ch.isprintable():
                self.q.put("Lit_" + urllib.parse.quote(ch, safe=""))   # ECP takes one URL-encoded char per press

    def _run(self):
        while True:
            name = self.q.get()
            try:
                Remote.key(self.host_fn(), name)
                self.error = ""
            except Exception as e:
                log(f"keypress {name} failed: {e!r}")
                if isinstance(e, urllib.error.HTTPError) and e.code == 403:
                    self.error = "Roku blocked typing: allow Control by mobile apps"
                else:
                    self.error = "Can't reach the Roku"
                with self.q.mutex:          # don't sit through a 3 s timeout for every queued key
                    self.q.queue.clear()


class KeyboardWatch:
    """Hears when a text field opens on the TV, the same signal the official phone app uses to pop up its keyboard.

    Keeps its own ecp-2 session open (the listening one only exists while audio is connected), subscribes to the
    textedit events and calls on_change({"text", "masked"}) while a field is open, or on_change(None).
    """

    EVENTS = "+textedit-opened,+textedit-changed,+textedit-closed,+volume-changed"

    def __init__(self, host_fn, on_change, on_volume=None):
        self.host_fn, self.on_change, self.on_volume = host_fn, on_change, on_volume
        self.stop_evt = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.stop_evt.set()

    def _run(self):
        delay = 2
        while not self.stop_evt.is_set():
            try:
                asyncio.run(self._session(self.host_fn()))
                delay = 2                       # left on purpose (IP changed): reconnect promptly
            except Exception as e:
                log(f"keyboard watch: {e!r}")
                delay = min(30, delay * 2)      # TV asleep / unreachable: back off
            self.on_change(None)
            self.stop_evt.wait(delay)

    def _emit(self, d, prefix):
        tid = d.get(prefix + "textedit-id")
        if tid in (None, "none"):
            self.on_change(None)
        else:
            self.on_change({"text": d.get(prefix + "text", ""), "masked": d.get(prefix + "masked") == "true"})

    async def _session(self, host):
        async with websockets.connect(f"ws://{host}:{ECP_PORT}/ecp-session", subprotocols=["ecp-2"],
                                      additional_headers={"Sec-WebSocket-Origin": "Android"},
                                      open_timeout=5) as ws:
            while not self.stop_evt.is_set() and self.host_fn() == host:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), 2))
                except asyncio.TimeoutError:
                    continue
                note, resp = msg.get("notify"), msg.get("response")
                if note == "authenticate":
                    await ws.send(json.dumps({"request": "authenticate", "request-id": "0",
                                              "param-response": auth_response(msg["param-challenge"])}))
                elif resp == "authenticate":
                    if msg.get("status") != "200":
                        raise RuntimeError(f"auth refused: {msg}")
                    await ws.send(json.dumps({"request": "request-events", "request-id": "1",
                                              "param-events": self.EVENTS}))
                    # a field may already be open when we connect
                    await ws.send(json.dumps({"request": "query-textedit-state", "request-id": "2"}))
                elif resp == "request-events" and msg.get("status") not in ("200", "202"):
                    raise RuntimeError(f"TV won't send keyboard events: {msg}")
                elif resp == "query-textedit-state" and msg.get("status") == "200":
                    state = json.loads(base64.b64decode(msg["content-data"])).get("textedit-state", {})
                    self._emit(state, "")
                elif note in ("textedit-opened", "textedit-changed"):
                    self._emit(msg, "param-")
                elif note == "textedit-closed":
                    self.on_change(None)
                elif note == "volume-changed" and self.on_volume:
                    try:
                        self.on_volume({"volume": int(msg.get("param-volume", "")), "mute": msg.get("param-mute") == "true",
                                        "dest": msg.get("param-audio-destination", "")})
                    except ValueError:
                        pass


class CastSync:
    """Mirrors the app's YouTube player onto Playlet and keeps the two lined up.

    Events come from the extension and run in order on one thread. Playlet can only seek by reloading the video
    (a 2-3 s rebuffer), so a seek is sent to where the PC player *will be* once the TV is playing again (`lead`,
    learned from how long Playlet actually takes). Afterwards the muted PC player is nudged onto the TV's timeline:
    nudging the PC is instant, re-seeking the TV never is.
    """

    SETTLE = 3.0    # after a (re)load Playlet still reports the previous stream's state for a moment
    DRIFT = 0.6     # seconds of PC-vs-TV difference worth correcting
    RECHECK = 20.0  # seconds between drift checks while both are playing
    APPLY = 0.3     # a nudge reaches the page on its next poll (every 250 ms)

    def __init__(self, host_fn, bridge):
        self.host_fn, self.bridge = host_fn, bridge
        self.q = queue.Queue()
        self.title, self.error = "", ""
        self.settle_until = 0.0
        self.lead = 2.5          # learned: seconds from "play at t" until the TV is really playing
        self.current = None      # video id on the TV
        self.last_check = 0.0
        self._last_seek = None   # (id, t, when) of the last seek sent to the TV
        threading.Thread(target=self._run, daemon=True).start()

    def push(self, event):
        self.q.put(event)

    def _next(self):
        ev = self.q.get(timeout=1.0)
        while ev.get("kind") == "seek":   # scrubbing: only the latest of several queued seeks matters
            with self.q.mutex:
                if not self.q.queue or self.q.queue[0].get("kind") != "seek":
                    break
                ev = self.q.queue.popleft()
        return ev

    def _run(self):
        while True:
            try:
                ev = self._next()
            except queue.Empty:
                if self.current and time.time() - self.last_check > self.RECHECK:
                    self._safe("align", lambda: self._align(self.host_fn()))
                continue
            self._safe(ev.get("kind"), lambda: self._handle(ev))

    def _safe(self, what, fn):
        try:
            fn()
            self.error = ""
        except Exception as e:
            log(f"sync error on {what}: {e!r}")
            self.error = "Can't reach Playlet on the Roku"

    def _handle(self, ev):
        host, kind = self.host_fn(), ev.get("kind")
        log(f"sync {kind} {ev.get('id')} t={ev.get('t')}")
        if kind == "load":
            t0 = time.time()
            Remote.cast(host, ev["id"], ev.get("t", 0), ev.get("list"))
            self.title, self.current = ev.get("title") or ev["id"], ev["id"]
            self.settle_until = t0 + self.SETTLE
            if self._started(host, ev.get("t", 0), t0):
                self._align(host)
        elif kind == "seek":
            t, last = ev.get("t", 0), self._last_seek
            if last and last[0] == ev["id"] and abs(t - last[1]) < 1.0 and time.time() - last[2] < 6:
                log("sync seek skipped: YouTube's own correction of the previous seek")
                return
            self._last_seek = (ev["id"], t, time.time())
            paused = ev.get("paused")
            target = max(0.0, ev.get("t", 0) + (0 if paused else self.lead))
            t0 = time.time()
            Remote.cast(host, ev["id"], target)   # Playlet seeks by replaying at a timestamp
            self.settle_until = t0 + self.SETTLE
            if paused:
                self._ensure(host, "pause")
            elif self._started(host, target, t0):
                self._align(host)
        elif kind in ("pause", "play"):
            self._ensure(host, kind)
            if kind == "play":
                self._align(host)
        elif kind == "leave":
            Remote.close_player(host)   # Playlet closes its player and shows its home screen
            self.title, self.current = "", None

    def _started(self, host, target, t0):
        """Wait (abandoning it if a newer event arrives) until the TV is really playing near target; learn the lead."""
        last = None
        while time.time() < t0 + 10 and self.q.empty():
            st = Remote.state(host)
            if st and st["state"] == "play" and abs(st["pos"] - target) < 6:
                if last is not None and st["pos"] > last:      # the position is moving: it's actually playing
                    took = time.time() - t0 - 0.1                # ~half a poll interval of detection delay
                    self.lead = min(6.0, max(0.5, 0.6 * self.lead + 0.4 * took))
                    log(f"tv playing after {took:.2f}s, lead now {self.lead:.2f}s")
                    return True
                last = st["pos"]
            time.sleep(0.2)
        return False

    def _tv_position(self, host):
        """The TV's position right now. Roku updates it in steps, so wait for a step and extrapolate from it."""
        first = Remote.state(host)
        if not first or first["state"] != "play":
            return None
        start = time.time()
        while time.time() - start < 1.5:
            time.sleep(0.05)
            st = Remote.state(host)
            if not st or st["state"] != "play":
                return None
            if st["pos"] != first["pos"]:
                return st["pos"]          # fresh reading, taken just now
        return first["pos"] + (time.time() - start)

    def _align(self, host):
        """Snap the muted PC player onto the TV's timeline if they've drifted apart."""
        self.last_check = time.time()
        p = self.bridge.player()
        if not p or p["id"] != self.current or p["paused"]:
            return
        tv = self._tv_position(host)
        p = self.bridge.player()          # re-read: _tv_position took a moment
        if tv is None or not p or p["id"] != self.current or p["paused"]:
            return
        drift = tv - p["t"]
        if abs(drift) > self.DRIFT:
            self.bridge.send({"sync": round(tv + self.APPLY, 2), "syncId": self.current})
            log(f"aligned PC player to TV (was {drift:+.2f}s off)")

    def _ensure(self, host, want):
        """The Roku Play key toggles, so only press it when the TV is in the other state."""
        time.sleep(max(0.0, self.settle_until - time.time()))
        deadline = time.time() + 8
        while time.time() < deadline:
            st = Remote.state(host)
            state = st and st["state"]
            if state in ("play", "pause"):
                if state != want:
                    Remote.key(host, "Play")
                return
            time.sleep(0.4)   # buffering / starting up


class TvVolume:
    """The TV's own volume, for when the sound is playing on the Roku and not routed to this PC.

    The Roku can't be asked what its volume is, but it announces every change (volume, mute and where the sound is
    going), so the level is tracked from those announcements, wherever the change came from: this app or the remote.
    It can't be set directly either, only nudged with VolumeUp / VolumeDown, so set_target() presses those until the
    announced level matches. The first press also tells us where we started."""

    def __init__(self, host_fn, on_change):
        self.host_fn, self.on_change = host_fn, on_change
        self.level, self.muted, self.dest = None, False, ""
        self.target = None
        self._moved, self._wake = threading.Event(), threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def heard(self, d):
        self.level, self.muted, self.dest = d["volume"], d["mute"], d["dest"]
        self._moved.set()
        self.on_change()

    def set_target(self, v):
        self.target = max(0, min(100, int(v)))
        self._wake.set()

    def toggle_mute(self):
        def press():
            try:
                Remote.key(self.host_fn(), "VolumeMute")
            except Exception as e:
                log(f"tv mute failed: {e!r}")
        threading.Thread(target=press, daemon=True).start()

    def _run(self):
        while True:
            self._wake.wait()
            self._wake.clear()
            stuck = 0
            while self.target is not None:
                target, level = self.target, self.level
                if level is not None and level == target:
                    if self.target == target:
                        self.target = None
                    break
                self._moved.clear()
                try:
                    Remote.key(self.host_fn(), "VolumeUp" if (level is None or target > level) else "VolumeDown")
                except Exception as e:
                    log(f"tv volume press failed: {e!r}")
                    self.target = None
                    break
                if self._moved.wait(0.8):
                    stuck = 0
                else:                                  # no announcement: at the limit, or the TV isn't listening
                    stuck += 1
                    if stuck >= 2:
                        self.target = None
                        break


class Listener:
    """One private-listening session: websocket + RTP socket + ffmpeg decoder + audio output."""

    def __init__(self, host, delay_ms, on_status):
        self.host, self.delay_us, self.on_status = host, delay_ms * 1000, on_status
        self.volume = 1.0
        self.stop_evt = threading.Event()
        self.threads, self.sock, self.proc, self.sdp_path, self.loop = [], None, None, None, None
        self.ws_task, self.failed = None, False

    def _fail(self, text):
        self.failed = True
        self.on_status(text, ended=True)

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found on PATH")
        try:
            check_support(self.host)
        except SystemExit as e:
            raise RuntimeError(str(e.code))
        except Exception as e:
            raise RuntimeError(f"Can't reach Roku at {self.host}: {e}")
        self.my_ip = local_ip_for(self.host)

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", RTP_PORT))

        f = tempfile.NamedTemporaryFile("w", suffix=".sdp", delete=False)
        f.write(SDP)
        f.close()
        self.sdp_path = f.name
        self.proc = subprocess.Popen(
            [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "warning", "-protocol_whitelist", "file,udp,rtp",
             "-fflags", "nobuffer", "-flags", "low_delay", "-i", self.sdp_path,
             "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=open(LOG, "a"), creationflags=NO_WINDOW)

        for fn in (self._rtp_loop, self._audio_loop, self._ws_thread):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self.threads.append(t)
        self.on_status("Connecting…")

    def stop(self):
        self.stop_evt.set()
        if self.loop and self.ws_task:
            self.loop.call_soon_threadsafe(self.ws_task.cancel)
        for obj in (self.sock,):
            try:
                obj and obj.close()
            except OSError:
                pass
        if self.proc:
            self.proc.terminate()
        if self.sdp_path:
            try:
                os.unlink(self.sdp_path)
            except OSError:
                pass

    # ---- worker threads --------------------------------------------------
    def _rtp_loop(self):
        fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        roku = (self.host, RTCP_PORT)
        self.sock.settimeout(0.5)
        started, last_rr, last_pkt, count = False, 0.0, time.monotonic(), 0
        while not self.stop_evt.is_set():
            now = time.monotonic()
            # Keep the Roku's keepalive satisfied on a timer (not only when packets arrive).
            if started and now - last_rr >= RR_EVERY:
                self.sock.sendto(RTCP_RR, roku)
                last_rr = now
            try:
                data, _ = self.sock.recvfrom(2048)
            except socket.timeout:
                if started and now - last_pkt > 1.5:
                    log(f"no RTP for {now - last_pkt:.1f}s (packets so far: {count})")
                continue
            except OSError:
                break
            if len(data) < 12 or data[0] >> 6 != 2:
                continue
            last_pkt, count = time.monotonic(), count + 1
            fwd.sendto(data, ("127.0.0.1", FFPLAY_PORT))
            if not started:
                started = True
                self.sock.sendto(rtcp_app(b"VDLY", self.delay_us), roku)
                self.sock.sendto(rtcp_app(b"CVER", 808464434), roku)
                self.sock.sendto(RTCP_RR, roku)
                last_rr = time.monotonic()
                log("first RTP packet received")
                self.on_status("Connected - playing")

    def _audio_loop(self):
        try:
            with sd.RawOutputStream(samplerate=48000, channels=2, dtype="int16", blocksize=960, latency="low") as out:
                while not self.stop_evt.is_set():
                    buf = self.proc.stdout.read(CHUNK)
                    if not buf:
                        if not self.stop_evt.is_set():
                            log(f"ffmpeg exited rc={self.proc.poll()}")
                            self._fail("Audio decoder stopped - see RokuListen.log in %APPDATA%")
                        break
                    v = self.volume
                    out.write(buf if v == 1.0 else audioop.mul(buf, 2, v))
        except Exception as e:
            if not self.stop_evt.is_set():
                log(f"audio error: {e!r}")
                self._fail(f"Audio error: {e}")

    def _ws_thread(self):
        self.loop = asyncio.new_event_loop()
        try:
            self.ws_task = self.loop.create_task(self._session())
            self.loop.run_until_complete(self.ws_task)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if not self.stop_evt.is_set():
                self.on_status(f"Roku connection lost: {e}")
        finally:
            if not self.stop_evt.is_set() and not self.failed:
                self.on_status("Disconnected by Roku", ended=True)

    async def _session(self):
        uri = f"ws://{self.host}:{ECP_PORT}/ecp-session"
        async with websockets.connect(uri, subprotocols=["ecp-2"],
                                      additional_headers={"Sec-WebSocket-Origin": "Android"}) as ws:
            async for text in ws:
                msg = json.loads(text)
                if msg.get("notify") == "authenticate":
                    await ws.send(json.dumps({"request": "authenticate", "request-id": "0",
                                              "param-response": auth_response(msg["param-challenge"])}))
                elif msg.get("response") == "authenticate":
                    if msg.get("status") != "200":
                        self._fail("Roku rejected authentication")
                        return
                    await ws.send(json.dumps({"request": "set-audio-output", "request-id": "1",
                                              "param-audio-output": "datagram",
                                              "param-devname": f"{self.my_ip}:{RTP_PORT}"}))
                elif msg.get("response") == "set-audio-output" and msg.get("status") != "200":
                    self._fail(f"Roku refused the stream ({msg.get('status-msg')})")
                    return


BG, CARD, EDGE = "#000000", "#0e0e10", "#222226"
FG, MUTED = "#f2f2f4", "#7d7d86"
ACCENT, ACCENT_HI = "#ff4e45", "#ff6a62"
GREEN, AMBER, RED, GREY = "#34d399", "#fbbf24", "#f87171", "#52525b"
BAR_H = 60


def asset(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", name)


def _load_fonts():
    """Roboto (YouTube's UI font) is bundled and loaded privately, so nothing gets installed system-wide."""
    try:
        return all(ctypes.windll.gdi32.AddFontResourceExW(asset(f), 0x10, 0) > 0   # FR_PRIVATE
                   for f in ("Roboto-Regular.ttf", "Roboto-Medium.ttf", "Roboto-Bold.ttf"))
    except Exception:
        return False


_ROBOTO = _load_fonts()
FONT = "Roboto" if _ROBOTO else "Segoe UI"
FONT_MED = "Roboto Medium" if _ROBOTO else "Segoe UI Semibold"


def text_y(font, cy):
    """y for create_text so the text's capitals sit visually centered on cy (Tk centers the whole
    ascent+descent box, which makes text look a pixel or two high next to icons)."""
    f = tkfont.Font(font=font)
    asc, desc = f.metrics("ascent"), f.metrics("descent")
    return round(cy + (asc * 0.75) / 2 - asc / 2 + desc / 2)


def rounded(canvas, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
           x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


class RoundButton(tk.Canvas):
    def __init__(self, parent, text, command, width=328, height=46):
        super().__init__(parent, width=width, height=height, bg=BG, highlightthickness=0, cursor="hand2")
        self.w, self.h, self.command = width, height, command
        self.text, self.fill, self.fg, self.hover, self.enabled, self.hot = text, ACCENT, "#ffffff", ACCENT_HI, True, False
        self.bind("<Enter>", lambda e: self._hot(True))
        self.bind("<Leave>", lambda e: self._hot(False))
        self.bind("<Button-1>", lambda e: self.enabled and self.command())
        self.draw()

    def _hot(self, on):
        self.hot = on
        self.draw()

    def draw(self):
        self.delete("all")
        fill = self.hover if (self.hot and self.enabled) else self.fill
        if not self.enabled:
            fill = "#2a2a2e"
        rounded(self, 1, 1, self.w - 1, self.h - 1, 14, fill=fill, outline="")
        self.create_text(self.w / 2, self.h / 2, text=self.text, fill=self.fg if self.enabled else MUTED,
                         font=(FONT, 11, "bold"))

    def style(self, text, fill, hover, enabled=True):
        self.text, self.fill, self.hover, self.enabled = text, fill, hover, enabled
        self.draw()


class IconButton(tk.Canvas):
    """Small rounded button with a drawn icon: back, back10, rev, play, pause, fwd, fwd10, up, down, left,
    right, ok. With repeat=True, holding it down repeats the command like a held remote button."""

    REPEAT_DELAY, REPEAT_EVERY = 400, 140   # ms, roughly like holding a real Roku remote's arrow

    def __init__(self, parent, kind, command, width=58, height=44, repeat=False):
        super().__init__(parent, width=width, height=height, bg=BG, highlightthickness=0, cursor="hand2")
        self.w, self.h, self.kind, self.command, self.hot = width, height, kind, command, False
        self.repeat, self.down, self._job = repeat, False, None
        self.bind("<Enter>", lambda e: self._hot(True))
        self.bind("<Leave>", lambda e: (self._up(), self._hot(False)))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", lambda e: self._up())
        self.draw()

    def _hot(self, on):
        self.hot = on
        self.draw()

    def _press(self, e):
        self.down = True
        self.draw()
        self.command()
        if self.repeat:
            self._job = self.after(self.REPEAT_DELAY, self._again)

    def _again(self):
        if self.down:
            self.command()
            self._job = self.after(self.REPEAT_EVERY, self._again)

    def _up(self):
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        if self.down:
            self.down = False
            self.draw()

    def set_kind(self, kind):
        if kind != self.kind:
            self.kind = kind
            self.draw()

    def _tri(self, cx, cy, s, right):
        d = s if right else -s
        self.create_polygon(cx - d * 0.6, cy - s, cx - d * 0.6, cy + s, cx + d * 0.8, cy, fill=FG, outline="")

    def draw(self):
        self.delete("all")
        fill = "#34343a" if self.down else "#26262b" if self.hot else "#151518"
        rounded(self, 1, 1, self.w - 1, self.h - 1, 12, fill=fill, outline=EDGE)
        cx, cy, k = self.w / 2, self.h / 2, self.kind
        if k in ("up", "down"):   # same solid triangles as play / rev / fwd, turned upright
            d = -1 if k == "up" else 1
            self.create_polygon(cx - 8, cy - d * 4.5, cx + 8, cy - d * 4.5, cx, cy + d * 5.5, fill=FG, outline="")
        elif k in ("left", "right"):
            d = 1 if k == "right" else -1
            self.create_polygon(cx - d * 4.5, cy - 8, cx - d * 4.5, cy + 8, cx + d * 5.5, cy, fill=FG, outline="")
        elif k == "ok":
            self.create_text(cx, cy, text="OK", fill=FG, font=(FONT, 11, "bold"))
        elif k == "back":   # the Roku remote's U-turn arrow: points left on top, hooks down on the right
            self.create_line(cx - 9, cy - 4, cx + 3, cy - 4, cx + 9, cy - 4, cx + 9, cy + 6, cx + 3, cy + 6,
                             cx - 3, cy + 6, smooth=True, width=2.5, fill=FG, capstyle="round",
                             arrow="first", arrowshape=(7, 8, 4))
        elif k in ("back10", "fwd10"):
            self.create_text(cx, cy, text=("−10" if k == "back10" else "+10"), fill=FG, font=(FONT, 11, "bold"))
        elif k in ("rev", "fwd"):
            right = k == "fwd"
            self._tri(cx - 6, cy, 7, right)
            self._tri(cx + 6, cy, 7, right)
        elif k == "play":
            self._tri(cx, cy, 9, True)
        else:  # pause
            for dx in (-6, 2):
                self.create_rectangle(cx + dx, cy - 8, cx + dx + 5, cy + 8, fill=FG, outline="")


class DPad(tk.Frame):
    """The remote's arrows and OK as a cross of square buttons, styled like the transport row."""

    # 3 x 54 + 2 x 8 = 178 px: the height of the status card + Connect + transport row it sits beside
    def __init__(self, parent, send, size=54, gap=8):
        super().__init__(parent, bg=BG)
        self.buttons = {}
        for kind, key, row, col in (("up", "Up", 0, 1), ("left", "Left", 1, 0), ("ok", "Select", 1, 1),
                                    ("right", "Right", 1, 2), ("down", "Down", 2, 1)):
            btn = IconButton(self, kind, lambda k=key: send(k), width=size, height=size,
                             repeat=kind != "ok")   # OK doesn't auto-repeat on a Roku remote either
            btn.grid(row=row, column=col, padx=(0 if col == 0 else gap, 0), pady=(0 if row == 0 else gap, 0))
            self.buttons[key] = btn


class Slider(tk.Canvas):
    def __init__(self, parent, lo, hi, value, command, width=328, height=30):
        super().__init__(parent, width=width, height=height, bg=BG, highlightthickness=0, cursor="hand2")
        self.lo, self.hi, self.value, self.command, self.w, self.h = lo, hi, value, command, width, height
        self.pad = 10
        self.mark = 100          # where to draw the 100% tick (None for none)
        self.dragging = False    # true while the mouse is held on it, so live updates don't fight your hand
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "dragging", False))
        self.draw()

    def set_range(self, lo, hi, value, mark=None):
        """Change what the slider measures (PC volume 0-200% vs the TV's 0-100) without calling `command`."""
        self.lo, self.hi, self.mark = lo, hi, mark
        self.value = max(lo, min(hi, value))
        self.draw()

    def get(self):
        return self.value

    def set(self, v):
        self.value = max(self.lo, min(self.hi, v))
        self.draw()

    def _drag(self, e):
        self.dragging = True
        span = self.w - 2 * self.pad
        frac = (e.x - self.pad) / span
        self.set(round(self.lo + frac * (self.hi - self.lo)))
        self.command(self.value)

    def draw(self):
        self.delete("all")
        span = self.w - 2 * self.pad
        x = self.pad + span * (self.value - self.lo) / (self.hi - self.lo)
        y = self.h / 2
        self.create_line(self.pad, y, self.w - self.pad, y, fill="#2a2a2e", width=4, capstyle="round")
        # mark 100% so the boost range is obvious
        if self.mark is not None:
            x100 = self.pad + span * (self.mark - self.lo) / (self.hi - self.lo)
            self.create_line(x100, y - 7, x100, y + 7, fill="#3a3a40", width=1)
        self.create_line(self.pad, y, x, y, fill=ACCENT, width=4, capstyle="round")
        self.create_oval(x - 8, y - 8, x + 8, y + 8, fill="#ffffff", outline=BG, width=2)


class TabSwitch(tk.Canvas):
    """Segmented control: a dark pill with the active segment raised."""

    def __init__(self, parent, names, command, seg_w=104, height=34):
        super().__init__(parent, width=seg_w * len(names) + 8, height=height, bg=BG, highlightthickness=0,
                         cursor="hand2")
        self.names, self.command, self.seg_w, self.h = names, command, seg_w, height
        self.active, self.hot = names[0], None
        self.bind("<Button-1>", lambda e: self._pick(e.x))
        self.bind("<Motion>", lambda e: self._hover(self._at(e.x)))
        self.bind("<Leave>", lambda e: self._hover(None))
        self.draw()

    def _at(self, x):
        i = int((x - 4) // self.seg_w)
        return self.names[i] if 0 <= i < len(self.names) else None

    def _hover(self, name):
        if name != self.hot:
            self.hot = name
            self.draw()

    def _pick(self, x):
        name = self._at(x)
        if name:
            self.command(name)

    def set(self, name):
        self.active = name
        self.draw()

    def draw(self):
        self.delete("all")
        rounded(self, 0, 0, int(self["width"]), self.h, 12, fill="#121215", outline="")
        for i, name in enumerate(self.names):
            x1 = 4 + i * self.seg_w
            if name == self.active:
                rounded(self, x1, 4, x1 + self.seg_w, self.h - 4, 9, fill="#26262c", outline="")
            color = FG if name == self.active else ("#b4b4bc" if name == self.hot else MUTED)
            font = (FONT_MED, 10)
            self.create_text(x1 + self.seg_w / 2, text_y(font, self.h / 2), text=name, fill=color, font=font)


class Toggle(tk.Canvas):
    """iOS-style switch."""

    def __init__(self, parent, on, command, width=46, height=26):
        super().__init__(parent, width=width, height=height, bg=BG, highlightthickness=0, cursor="hand2")
        self.on, self.command, self.w, self.h = on, command, width, height
        self.bind("<Button-1>", lambda e: self._flip())
        self.draw()

    def _flip(self):
        self.on = not self.on
        self.draw()
        self.command(self.on)

    def draw(self):
        self.delete("all")
        r = self.h / 2
        rounded(self, 1, 1, self.w - 1, self.h - 1, r - 1, fill=ACCENT if self.on else "#2a2a30", outline="")
        cx = self.w - r if self.on else r
        self.create_oval(cx - r + 4, 4, cx + r - 4, self.h - 4, fill="#ffffff", outline="")


# Tk keysym -> Roku ECP key for the non-text keys the Type box forwards; the value's glyph is shown as feedback
ECP_KEYS = {
    "Up": ("Up", "↑"), "Down": ("Down", "↓"), "Left": ("Left", "←"), "Right": ("Right", "→"),
    "Return": ("Select", "OK"), "KP_Enter": ("Select", "OK"), "Escape": ("Back", "Back"),
    "BackSpace": ("Backspace", "⌫"), "Delete": ("Backspace", "⌫"),
}
CTRL, ALT = 0x4, 0x20000   # Tk event.state bits on Windows (AltGr arrives as Ctrl+Alt)


class TypeBox(tk.Frame):
    """Click it, then type: text goes into whatever text field is open on the TV, and arrows / Enter / Esc
    work the TV's menus like the remote's d-pad, OK and Back."""

    def __init__(self, parent, sender, width=328, height=58):
        super().__init__(parent, bg=CARD, width=width, height=height, highlightthickness=1, takefocus=1,
                         highlightbackground=EDGE, highlightcolor=ACCENT, cursor="xterm")
        self.pack_propagate(False)
        self.sender, self.typed, self.last = sender, "", ""
        self.tv = None   # {"text", "masked"} while a text field is open on the TV (from KeyboardWatch)
        self.line = tk.Label(self, bg=CARD, font=(FONT, 11), anchor="w")
        self.hint = tk.Label(self, bg=CARD, font=(FONT, 8), anchor="w")
        self.line.pack(fill="x", padx=12, pady=(8, 0))
        self.hint.pack(fill="x", padx=12)
        for w in (self, self.line, self.hint):
            w.bind("<Button-1>", lambda e: self.focus_set())
        self.bind("<KeyPress>", self._key)
        self.bind("<FocusIn>", lambda e: self.render())
        self.bind("<FocusOut>", lambda e: self.render())
        self.render()

    def owns(self, widget):
        return widget in (self, self.line, self.hint)

    @staticmethod
    def wants(e):
        """True for keys the box would send (so a stray press elsewhere can be handed to it)."""
        if e.state & CTRL and not e.state & ALT:
            return e.keysym.lower() == "v"
        return e.keysym in ECP_KEYS or bool(e.char and e.char.isprintable())

    def set_tv(self, state):
        if state is None:
            self.typed, self.last = "", ""
        self.tv = state
        self.render()

    def _key(self, e):
        if e.keysym in ("Tab", "ISO_Left_Tab"):
            return None                                   # keep normal focus traversal
        if e.state & CTRL and not e.state & ALT:
            if e.keysym.lower() == "v":
                self._paste()
            return "break"
        if e.keysym in ECP_KEYS:
            name, self.last = ECP_KEYS[e.keysym]
            self.sender.key(name)
            if name == "Backspace":
                self.typed = self.typed[:-1]
            elif name in ("Select", "Back"):
                self.typed = ""                           # that text is done; start fresh next time
        elif e.char and e.char.isprintable():
            self.sender.text(e.char)
            self.typed, self.last = self.typed + e.char, ""
        else:
            return None                                   # Shift, F-keys, etc.
        self.render()
        return "break"

    def _paste(self):
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return
        text = " ".join(text.split())                     # newlines / tabs can't be typed into a TV field
        self.sender.text(text)
        self.typed, self.last = self.typed + text, "paste"
        self.render()

    def render(self):
        try:
            focused = self.focus_get() is self
        except KeyError:   # focus is inside the embedded Chrome, which Tk doesn't know
            focused = False
        if self.tv is not None:   # the TV's own text is the truth; passwords stay hidden
            text = self.tv["text"]
            text = "•" * len(text) if self.tv["masked"] else text
        else:
            text = self.typed
        if text or focused:
            shown = text if len(text) <= 30 else "…" + text[-29:]
            self.line.config(text=shown + ("▏" if focused else ""), fg=FG)
        else:
            self.line.config(text="Click here to type on the TV", fg=MUTED)
        last = f"  ·  sent {self.last}" if self.last else ""
        if self.sender.error:
            self.hint.config(text=self.sender.error, fg=RED)
        elif self.tv is not None:
            self.hint.config(text=("TV keyboard open · Enter = OK · Esc = Back" + last) if focused
                             else "TV keyboard open · click here to type", fg=GREEN)
        elif focused:
            self.hint.config(text=f"Arrows move · Enter = OK · Esc = Back{last}", fg=MUTED)
        else:
            self.hint.config(text="Search and sign-in boxes, Ctrl+V pastes", fg=MUTED)
        self.config(highlightbackground=GREEN if self.tv is not None and not focused else EDGE)


class NavButton(tk.Canvas):
    """Small round browser button: back, fwd, reload."""

    def __init__(self, parent, kind, command, size=34):
        super().__init__(parent, width=size, height=size, bg=BG, highlightthickness=0, cursor="hand2")
        self.kind, self.command, self.s, self.hot = kind, command, size, False
        self.bind("<Enter>", lambda e: self._set(True))
        self.bind("<Leave>", lambda e: self._set(False))
        self.bind("<Button-1>", lambda e: self.command())
        self.draw()

    def _set(self, hot):
        self.hot = hot
        self.draw()

    def draw(self):
        self.delete("all")
        s, c = self.s, self.s / 2
        if self.hot:
            self.create_oval(1, 1, s - 1, s - 1, fill="#1e1e23", outline="")
        col = FG if self.hot else "#a8a8b0"
        if self.kind in ("back", "fwd"):
            d = -1 if self.kind == "back" else 1
            self.create_line(c - 3 * d, c - 6, c + 3 * d, c, c - 3 * d, c + 6, fill=col, width=2,
                             capstyle="round", joinstyle="round")
        else:  # reload: open arc + arrow head
            self.create_arc(c - 7, c - 7, c + 7, c + 7, start=60, extent=290, style="arc", outline=col, width=2)
            self.create_polygon(c + 3, c - 10, c + 8, c - 5, c + 2, c - 3, fill=col, outline="")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CapoCast")
        self.configure(bg=BG)
        cfg = self._load()
        w, h = 1280, 800
        self.geometry(f"{w}x{h}+{max(0, (self.winfo_screenwidth() - w) // 2)}+{max(0, (self.winfo_screenheight() - h) // 2 - 20)}")
        self.minsize(900, 600)
        self.listener = None
        self.host = tk.StringVar(value=cfg.get("host", DEFAULT_HOST))
        self.delay = tk.StringVar(value=str(cfg.get("delay", 500)))
        self.status = tk.StringVar(value="Not connected")
        # worker threads read the Roku IP from here (Tk variables aren't thread-safe)
        self._host = self.host.get().strip() or DEFAULT_HOST
        self.host.trace_add("write", lambda *a: setattr(self, "_host", self.host.get().strip() or DEFAULT_HOST))
        try:
            self._icon = tk.PhotoImage(file=asset("youtube.png"))
            self.iconphoto(True, self._icon)
        except Exception:
            pass

        self.autocast = cfg.get("autocast", True)
        self.sync = None   # created once the bridge exists
        self.bridge = Bridge(log)
        self.bridge.settings["autocast"] = self.autocast
        self.sync = CastSync(lambda: self._host, self.bridge)
        self.bridge.on_event = lambda ev: self.sync.push(ev) if self.autocast else None
        self.bridge_error = False
        try:
            self.bridge.start()
        except OSError as e:
            self.bridge_error = True
            log(f"bridge failed: {e!r}")
        self.chrome = yt_embed.ChromeEmbed(log)

        self._build_topbar()
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True)
        self.pages = {"Listen": tk.Frame(body, bg=BG), "YouTube": tk.Frame(body, bg=BG), "Web": tk.Frame(body, bg=BG)}
        self._build_listen(self.pages["Listen"])
        self._build_youtube(self.pages["YouTube"])
        ui = SimpleNamespace(BG=BG, CARD=CARD, EDGE=EDGE, FG=FG, MUTED=MUTED, ACCENT=ACCENT, ACCENT_HI=ACCENT_HI,
                             GREEN=GREEN, AMBER=AMBER, RED=RED, GREY=GREY, FONT=FONT, FONT_MED=FONT_MED,
                             RoundButton=RoundButton, NavButton=NavButton, Remote=Remote, log=log)
        self.web = web_tab.WebTab(self, self.pages["Web"], ui, cfg.get("web_home"))
        self.page = None
        self._show("YouTube" if "--youtube" in sys.argv else "Web" if "--web" in sys.argv else "Listen")

        self.protocol("WM_DELETE_WINDOW", self.close)
        if "--autoconnect" in sys.argv:
            self.after(500, self.connect)
        self.update()
        self._dark_titlebar()
        self._poll()
        self._yt_tick()
        self._status_tick()

    # ---- layout ----------------------------------------------------------
    def _build_topbar(self):
        bar = tk.Canvas(self, height=BAR_H, bg=BG, highlightthickness=0)
        bar.pack(fill="x")
        self.bar = bar
        cy = BAR_H // 2

        # brand: real YouTube icon + wordmark, sharing one centerline
        self._logo = ImageTk.PhotoImage(Image.open(asset("youtube.png")).resize((30, 30), Image.LANCZOS))
        bar.create_image(22, cy, image=self._logo, anchor="w")
        brand = (FONT, 13, "bold")
        bar.create_text(60, text_y(brand, cy), text="CapoCast", anchor="w", fill=FG, font=brand)

        self.tabs = TabSwitch(bar, ["Listen", "YouTube", "Web"], self._show, seg_w=88)
        bar.create_window(194, cy, window=self.tabs, anchor="w")

        # browser nav sits next to the tabs, like a browser's toolbar
        self.yt_items = []
        x = 194 + int(self.tabs["width"]) + 18
        for kind, cmd in (("back", lambda: self.bridge.send({"history": -1})),
                          ("fwd", lambda: self.bridge.send({"history": 1})),
                          ("reload", lambda: self.bridge.send({"reload": True}))):
            self.yt_items.append(bar.create_window(x, cy, window=NavButton(bar, kind, cmd), anchor="w"))
            x += 36

        # right side: sync status, then the Auto-cast switch
        label = (FONT_MED, 10)
        self.auto_toggle = Toggle(bar, self.autocast, self._set_autocast)
        self.r_toggle = bar.create_window(0, cy, window=self.auto_toggle, anchor="e")
        self.r_label = bar.create_text(0, text_y(label, cy), text="Auto-cast", anchor="e", fill="#d4d4da", font=label)
        self.r_div = bar.create_line(0, cy - 11, 0, cy + 11, fill=EDGE)
        status = (FONT, 9)
        self.r_status = bar.create_text(0, text_y(status, cy), text="", anchor="e", fill=MUTED, font=status)
        self.r_dot = bar.create_oval(0, 0, 0, 0, fill=GREY, outline="")
        self.yt_items += [self.r_toggle, self.r_label, self.r_div, self.r_status, self.r_dot]
        self.hairline = bar.create_line(0, BAR_H - 1, 0, BAR_H - 1, fill=EDGE)
        bar.bind("<Configure>", lambda e: self._layout_bar(e.width))

    def _layout_bar(self, width=None):
        bar, cy = self.bar, BAR_H // 2
        width = width or bar.winfo_width()
        bar.coords(self.hairline, 0, BAR_H - 1, width, BAR_H - 1)
        x = width - 20
        bar.coords(self.r_toggle, x, cy)
        x -= int(self.auto_toggle["width"]) + 10
        bar.coords(self.r_label, x, bar.coords(self.r_label)[1])
        x -= tkfont.Font(font=bar.itemcget(self.r_label, "font")).measure("Auto-cast") + 16
        bar.coords(self.r_div, x, cy - 11, x, cy + 11)
        x -= 16
        bar.coords(self.r_status, x, bar.coords(self.r_status)[1])
        x -= tkfont.Font(font=bar.itemcget(self.r_status, "font")).measure(bar.itemcget(self.r_status, "text")) + 8
        bar.coords(self.r_dot, x - 8, cy - 4, x, cy + 4)

    def _set_sync_status(self, text, color):
        self.bar.itemconfigure(self.r_status, text=text)
        self.bar.itemconfigure(self.r_dot, fill=color)
        self._layout_bar()

    def _status_tick(self):
        """Top-bar sync status: extension connection + what's on the TV."""
        if self.bridge_error:
            self._set_sync_status("Port 8765 is busy - close other copies of CapoCast", RED)
        elif not self.bridge.connected():
            if self.chrome.waiting():
                self._set_sync_status("Starting YouTube\u2026", AMBER)
            elif not self.chrome.alive():
                self._set_sync_status("YouTube isn't open", GREY)
            elif time.time() - getattr(self, "_embedded_at", 0) < 10:
                self._set_sync_status("Connecting\u2026", AMBER)
            else:
                self._set_sync_status("Reload Playlet Cast in chrome://extensions", GREY)
        elif not self.autocast:
            self._set_sync_status("Auto-cast off", GREY)
        elif self.sync.error:
            self._set_sync_status(self.sync.error, RED)
        elif self.sync.title:
            t = self.sync.title
            self._set_sync_status("On TV  ·  " + (t if len(t) <= 42 else t[:41] + "…"), GREEN)
        else:
            self._set_sync_status("Ready  ·  pick a video", GREEN)
        self.after(700, self._status_tick)

    def _build_listen(self, page):
        wrap = tk.Frame(page, bg=BG)
        wrap.place(relx=0.5, rely=0.47, anchor="center")
        col = tk.Frame(wrap, bg=BG)
        col.pack(side="left", anchor="n")
        tk.Label(col, text="TV audio", bg=BG, fg=FG, font=(FONT, 20, "bold")).pack(anchor="w")
        tk.Label(col, text="Roku private listening, on this PC", bg=BG, fg=MUTED,
                 font=(FONT, 10)).pack(anchor="w", pady=(0, 14))

        card = tk.Frame(col, bg=CARD, highlightbackground=EDGE, highlightthickness=1)
        card.pack(fill="x")
        srow = tk.Frame(card, bg=CARD)
        srow.pack(fill="x", padx=16, pady=14)
        self.dot = tk.Canvas(srow, width=12, height=12, bg=CARD, highlightthickness=0)
        self.dot.pack(side="left")
        self.dot_item = self.dot.create_oval(1, 1, 11, 11, fill=GREY, outline="")
        tk.Label(srow, textvariable=self.status, bg=CARD, fg=FG, font=(FONT, 10), anchor="w",
                 wraplength=270, justify="left").pack(side="left", padx=(10, 0))

        self.btn = RoundButton(col, "Connect", self.toggle)
        self.btn.pack(pady=(16, 18))

        # transport: acts on whatever the Roku is playing, independent of the audio connection
        trow = tk.Frame(col, bg=BG)
        trow.pack(fill="x")
        self.play_btn = IconButton(trow, "play", self._toggle_play, width=48)
        for i, (kind, action) in enumerate([
            ("back", lambda: self.keys.key("Back")),      # through the key queue, so it lands after any typing
            ("back10", lambda: self._remote(Remote.seek, -10)),
            ("rev", lambda: self._remote(Remote.key, "Rev")),
            (None, None),
            ("fwd", lambda: self._remote(Remote.key, "Fwd")),
            ("fwd10", lambda: self._remote(Remote.seek, 10)),
        ]):
            btn = self.play_btn if kind is None else IconButton(trow, kind, action, width=48)
            btn.pack(side="left", padx=(0 if i == 0 else 8, 0))   # 6 x 48 + 5 x 8 = 328, the column width
        self.now = tk.Label(col, text="Nothing playing", bg=BG, fg=MUTED, font=(FONT, 9), anchor="w")
        self.now.pack(fill="x", pady=(8, 14))
        self.play_state, self._polling = "none", False

        tk.Label(col, text="Type on TV", bg=BG, fg=MUTED, font=(FONT, 10)).pack(anchor="w")
        self.keys = KeySender(lambda: self._host)

        # Remote column beside it (stacked below, it wouldn't fit the minimum window height). Same heading and
        # subtitle as "TV audio", so both columns share one top edge and the D-pad lines up with the status card.
        side = tk.Frame(wrap, bg=BG)
        side.pack(side="left", anchor="n", padx=(64, 0))
        tk.Label(side, text="Remote", bg=BG, fg=FG, font=(FONT, 20, "bold")).pack(anchor="w")
        tk.Label(side, text="Arrows and OK, like the Roku remote", bg=BG, fg=MUTED,
                 font=(FONT, 10)).pack(anchor="w", pady=(0, 14))
        self.dpad = DPad(side, self.keys.key)
        self.dpad.pack(anchor="w")
        tk.Label(side, text="Hold an arrow to repeat", bg=BG, fg=MUTED, font=(FONT, 9),
                 anchor="w").pack(fill="x", pady=(8, 0))   # same spacing as the "now playing" line opposite
        self.typebox = TypeBox(col, self.keys)
        self.typebox.pack(pady=(4, 16))
        # clicking anywhere else (except a text field) takes keyboard focus away from the Type box
        self.bind_all("<Button-1>", self._click_away, add="+")
        # typing with nothing else selected goes to the TV too (bound on the window, so Entries are left alone)
        self.bind("<KeyPress>", self._stray_key)
        self._kb_from = None   # tab to go back to when the TV's keyboard closes
        self.tvvol = TvVolume(lambda: self._host, lambda: self._after_safe(self._tv_volume_changed))
        self.kb_watch = KeyboardWatch(lambda: self._host, self._on_tv_keyboard, self.tvvol.heard)

        # One volume control for whichever place the sound is going: this PC while audio is routed here (Connect),
        # the TV's own volume otherwise. It switches by itself when you connect or disconnect.
        vrow = tk.Frame(col, bg=BG)
        vrow.pack(fill="x")
        self.vol_title = tk.Label(vrow, text="Volume", bg=BG, fg=MUTED, font=(FONT, 10))
        self.vol_title.pack(side="left")
        self.vol_label = tk.Label(vrow, bg=BG, fg=FG, font=(FONT, 10, "bold"))
        self.vol_label.pack(side="right")
        self.mute_lbl = tk.Label(vrow, text="Mute", bg=BG, fg=MUTED, font=(FONT, 9), cursor="hand2")
        self.mute_lbl.bind("<Button-1>", lambda e: self.tvvol.toggle_mute())
        self.pc_volume = int(self._load().get("volume", 100))
        self.scale = Slider(col, 0, 200, self.pc_volume, self._on_volume)
        self.scale.pack(pady=(4, 18))
        self._refresh_volume()

        orow = tk.Frame(col, bg=BG)
        orow.pack(fill="x")
        self.host_entry = self._field(orow, "Roku IP", self.host, 15)
        self.delay_entry = self._field(orow, "Delay ms", self.delay, 6, pad=(14, 0))

    def _build_youtube(self, page):
        self.yt_page = page
        self.yt_msg = tk.Label(page, text="", bg=BG, fg=MUTED, font=(FONT, 11), cursor="")
        self.yt_msg.place(relx=0.5, rely=0.45, anchor="center")
        self.yt_msg.bind("<Button-1>", lambda e: self._yt_reopen())
        page.bind("<Configure>", lambda e: self.chrome.fit(e.width, e.height))

    # ---- tabs ------------------------------------------------------------
    def _show(self, name):
        if name == self.page:
            return
        if self.page:
            self.pages[self.page].pack_forget()
        self.page = name
        self.tabs.set(name)
        self.pages[name].pack(fill="both", expand=True)
        for item in self.yt_items:
            self.bar.itemconfigure(item, state="normal" if name == "YouTube" else "hidden")
        if name == "YouTube":
            if self.chrome.alive():
                self.chrome.show(True)
                self.update_idletasks()
                self.chrome.fit(self.yt_page.winfo_width(), self.yt_page.winfo_height())
                self.chrome.focus()
            elif not self.chrome.waiting():
                self._yt_reopen()
        else:
            self.chrome.show(False)
        # each tab hides the other's browser; the Web tab starts its own the first time it's opened
        if name == "Web":
            self.web.on_show()
        else:
            self.web.on_hide()

    # ---- YouTube (embedded Chrome) ----------------------------------------
    def _yt_message(self, text, clickable=False):
        self.yt_msg.config(text=text, fg=FG if clickable else MUTED, cursor="hand2" if clickable else "")

    def _yt_reopen(self):
        if self.chrome.alive() or self.chrome.waiting():
            return
        self._yt_message("Opening YouTube…")
        try:
            self.chrome.launch()
        except Exception as e:
            self._yt_message(f"{e}  ·  click to retry", clickable=True)

    def _yt_tick(self):
        try:
            if self.chrome.waiting():
                hwnd = self.chrome.find_new()
                if hwnd:
                    self.chrome.embed(hwnd, self.yt_page.winfo_id())
                    self._embedded_at = time.time()
                    self.update_idletasks()
                    self.chrome.fit(self.yt_page.winfo_width(), self.yt_page.winfo_height())
                    self.chrome.show(self.page == "YouTube")
                    self._yt_message("")
                elif self.chrome.timed_out():
                    self.chrome._before = None
                    self._yt_message("Couldn't open YouTube  ·  click to retry", clickable=True)
            elif self.chrome.hwnd and not self.chrome.alive():
                self.chrome.hwnd = None
                self._yt_message("YouTube was closed  ·  click to reopen", clickable=True)
        except Exception as e:
            log(f"embed error: {e!r}")
        self.after(250, self._yt_tick)

    def _set_autocast(self, on):
        self.autocast = on
        self.bridge.settings["autocast"] = on
        self._save()

    def autocast_off(self):
        """A Web-tab cast owns the TV now; stop the YouTube tab's auto-cast from taking it back."""
        if self.autocast:
            self.auto_toggle.on = False
            self.auto_toggle.draw()
            self._set_autocast(False)

    # ---- transport -------------------------------------------------------
    def _remote(self, fn, *args):
        """Run a Roku call off the UI thread, then refresh the readout."""
        host = self.host.get().strip() or DEFAULT_HOST

        def work():
            try:
                fn(host, *args)
            except Exception as e:
                log(f"remote error: {e!r}")
            self._fetch(host)
        threading.Thread(target=work, daemon=True).start()

    def _toggle_play(self):
        self.play_state = "pause" if self.play_state == "play" else "play"   # optimistic; next poll corrects it
        self.play_btn.set_kind("pause" if self.play_state == "play" else "play")
        self._remote(Remote.key, "Play")

    def _poll(self):
        if not self._polling:
            self._polling = True
            host = self.host.get().strip() or DEFAULT_HOST
            threading.Thread(target=self._fetch, args=(host,), daemon=True).start()
        self.after(1000, self._poll)

    def _fetch(self, host):
        st = Remote.state(host)
        try:
            self.after(0, self._show_state, st)
        except RuntimeError:  # window already closed
            pass

    def _show_state(self, st):
        self._polling = False
        fmt = lambda s: f"{int(s) // 60}:{int(s) % 60:02d}"
        if not st:
            self.play_state = "none"
            self.now.config(text="Can't reach Roku remote control")
        elif st["state"] == "none":
            self.play_state = "none"
            self.now.config(text="Nothing playing")
        else:
            self.play_state = st["state"]
            label = st["state"].capitalize() if st["state"] != "play" else "Playing"
            time_txt = f"  ·  {fmt(st['pos'])} / {fmt(st['dur'])}" if st["dur"] else ""
            self.now.config(text=f"{st['app'] or 'Roku'}  ·  {label}{time_txt}")
        self.play_btn.set_kind("pause" if self.play_state == "play" else "play")
        self.typebox.render()   # picks up typing errors reported by the sender thread

    def _focused(self):
        try:
            return self.focus_get()
        except KeyError:   # focus is inside the embedded Chrome
            return None

    def _click_away(self, e):
        w = e.widget
        if isinstance(w, tk.Misc) and not self.typebox.owns(w) and not isinstance(w, tk.Entry) \
                and self._focused() is self.typebox:
            self.focus_set()

    def _stray_key(self, e):
        """A key pressed on the Listen tab with nothing selected: hand it to the Type box."""
        if e.widget is self and self.page == "Listen" and self.typebox.wants(e):
            self.typebox.focus_set()
            result = self.typebox._key(e)
            self.after(1, self.typebox.render)   # draw the caret once focus has actually moved
            return result
        return None

    # ---- TV keyboard (pops up like the phone app's) ------------------------
    def _on_tv_keyboard(self, state):
        try:
            self.after(0, self._tv_keyboard, state)   # from the watcher thread: hop to the Tk thread
        except RuntimeError:   # window already closed
            pass

    def _tv_keyboard(self, state):
        was_open = self.typebox.tv is not None
        self.typebox.set_tv(state)
        if state is not None and not was_open:
            log("TV keyboard opened")
            self._kb_from = self.page if self.page != "Listen" else None
            self._show("Listen")
            self.typebox.focus_set()
            self.after(1, self.typebox.render)
            self._pop_up()
        elif state is None and was_open:
            log("TV keyboard closed")
            if self._focused() is self.typebox:
                self.focus_set()
            if self._kb_from:
                self._show(self._kb_from)
            self._kb_from = None

    def _pop_up(self):
        """Bring the window forward. If another app (say, a game) has the foreground, Windows refuses and
        flashes the taskbar button instead, so this never yanks you out of what you're doing."""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            if self.state() == "iconic":
                ctypes.windll.user32.FlashWindow(hwnd, True)
            else:
                ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception as e:
            log(f"pop up failed: {e!r}")

    def _field(self, parent, label, var, width, pad=(0, 0)):
        box = tk.Frame(parent, bg=BG)
        box.pack(side="left", padx=pad)
        tk.Label(box, text=label, bg=BG, fg=MUTED, font=(FONT, 9)).pack(anchor="w")
        e = tk.Entry(box, textvariable=var, width=width, bg=CARD, fg=FG, insertbackground=FG, relief="flat",
                     font=(FONT, 10), highlightthickness=1, highlightbackground=EDGE, highlightcolor=ACCENT,
                     disabledbackground=BG, disabledforeground=MUTED)
        e.pack(ipady=5, ipadx=4, pady=(3, 0))
        return e

    def _dark_titlebar(self):
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            val = ctypes.c_int(1)
            for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (Win10 build variants)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), 4)
            # nudge a repaint so the title bar picks it up
            self.withdraw()
            self.deiconify()
        except Exception:
            pass

    # ---- helpers ---------------------------------------------------------
    @staticmethod
    def _load():
        try:
            with open(CONFIG, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self):
        try:
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump({"host": self.host.get().strip(), "delay": self._delay(), "volume": self.pc_volume,
                           "autocast": self.autocast, "web_home": getattr(getattr(self, "web", None), "home", None)}, f)
        except OSError:
            pass

    def _delay(self):
        try:
            return max(0, int(self.delay.get()))
        except ValueError:
            return 500

    def _after_safe(self, fn):
        try:
            self.after(0, fn)          # from a worker thread: hop to the Tk thread
        except RuntimeError:           # window already closed
            pass

    def _on_volume(self, value):
        """The slider moved (not called when the code moves it): drive whichever volume it currently stands for."""
        if self.listener:
            self.pc_volume = int(value)
            self.listener.volume = self.pc_volume / 100
        else:
            self.tvvol.set_target(int(value))
        self._refresh_volume()

    def _tv_volume_changed(self):
        """The TV announced a volume change (from this app or from the remote): follow it."""
        self._refresh_volume()

    def _refresh_volume(self):
        """Make the control show the right thing for where the sound is going right now."""
        if self.listener:                                   # routed to this PC: the PC's own gain, 0-200%
            if self.scale.hi != 200:
                self.scale.set_range(0, 200, self.pc_volume, mark=100)
            self.vol_title.config(text="Volume  ·  this PC")
            self.vol_label.config(text=f"{self.pc_volume}%")
            self.mute_lbl.pack_forget()
            return
        tv = self.tvvol                                     # playing on the Roku: the TV's own volume, 0-100
        if self.scale.hi != 100:
            self.scale.set_range(0, 100, tv.level or 0, mark=None)
        elif tv.level is not None and not self.scale.dragging and tv.target is None:
            self.scale.set_range(0, 100, tv.level, mark=None)
        self.vol_title.config(text="Volume  ·  TV")
        self.vol_label.config(text="—" if tv.level is None else str(tv.level))
        if not self.mute_lbl.winfo_ismapped():
            self.mute_lbl.pack(side="right", padx=(0, 10))
        self.mute_lbl.config(text="Muted" if tv.muted else "Mute", fg=RED if tv.muted else MUTED)

    def _set_dot(self, text):
        color = GREEN if text.startswith("Connected") else AMBER if text.startswith("Connecting") \
            else GREY if text in ("Not connected", "Disconnected") else RED
        self.dot.itemconfig(self.dot_item, fill=color)

    def set_status(self, text, ended=False):
        # called from worker threads: hop back to the Tk thread
        log(f"status: {text}{' (ended)' if ended else ''}")
        self.after(0, self._apply_status, text, ended)

    def _apply_status(self, text, ended):
        self.status.set(text)
        self._set_dot(text)
        if ended and self.listener:
            self.disconnect(keep_status=True)

    # ---- actions ---------------------------------------------------------
    def toggle(self):
        if self.listener:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        listener = Listener(self.host.get().strip() or DEFAULT_HOST, self._delay(), self.set_status)
        listener.volume = self.pc_volume / 100
        self.btn.style("Connecting…", ACCENT, ACCENT_HI, enabled=False)
        self.update_idletasks()
        try:
            listener.start()
        except Exception as e:
            listener.stop()
            self.status.set(str(e))
            self._set_dot(str(e))
            self.btn.style("Connect", ACCENT, ACCENT_HI)
            return
        self.listener = listener
        self._save()
        self._set_dot("Connecting")
        for w in (self.host_entry, self.delay_entry):
            w.config(state="disabled")
        self.btn.style("Disconnect", "#26262b", "#34343a")
        self._refresh_volume()                              # the sound now goes to this PC: slider = PC volume

    def disconnect(self, keep_status=False):
        if self.listener:
            threading.Thread(target=self.listener.stop, daemon=True).start()
            self.listener = None
        if not keep_status:
            self.status.set("Not connected")
            self._set_dot("Not connected")
        for w in (self.host_entry, self.delay_entry):
            w.config(state="normal")
        self.btn.style("Connect", ACCENT, ACCENT_HI)
        self._refresh_volume()                              # the sound is back on the TV: slider = TV volume

    def close(self):
        self._save()
        self.kb_watch.stop()
        self.chrome.release()
        self.web.close()
        self.bridge.stop()
        if self.listener:
            self.listener.stop()
        self.destroy()


def already_running():
    """True (and the existing window is brought forward) if another CapoCast is already open."""
    try:
        kernel32, user32 = ctypes.windll.kernel32, ctypes.windll.user32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        globals()["_instance_mutex"] = kernel32.CreateMutexW(None, False, "Local\\RokuListen.SingleInstance")
        if kernel32.GetLastError() != 183:   # ERROR_ALREADY_EXISTS
            return False
        hwnd = user32.FindWindowW(None, "CapoCast")
        if hwnd:
            user32.ShowWindow(hwnd, 9)        # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    if already_running():
        sys.exit(0)
    try:
        ctypes.windll.user32.SetProcessDPIAware()   # real pixel coords, so the embedded Chrome lines up exactly
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RokuListen")   # own taskbar icon
    except Exception:
        pass
    App().mainloop()
