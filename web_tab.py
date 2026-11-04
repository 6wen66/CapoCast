"""The Web tab: any site in a protected, headless Brave, with a Cast to TV button.

Flow: you browse and press play. web_guard (talking to Brave over DevTools) blocks pop-ups, redirects and click
traps, records the streams the page loads, and streams the page's picture to web_view, which draws it here. Cast to
TV picks the real stream (stream_finder), serves it to the Roku through media_relay (which replays the site's
headers) and starts it in the RokuCast channel on the TV.

Brave has no window of its own, so nothing ever opens on the desktop or takes focus from a game.

The UI widgets and colors are handed in (`ui`) instead of imported: this file is loaded by roku_listen_gui.py, and
importing that file back would load it a second time.
"""
import json
import os
import shutil
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox
from urllib.parse import urlparse

import brave_profile
import drm
from drm import DrmError
from media_relay import MediaRelay, probe_dash, probe_hls
from roku_listen import local_ip_for
from stream_finder import Candidate, apply_page_videos, choose, score
from web_guard import WebGuard, normalize_address
from web_sync import WebSync
from web_view import PageView

HOME = "https://duckduckgo.com"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class CastError(Exception):
    """A problem to tell the user about, in plain words."""


def ytdlp_candidate(page_url):
    """Last resort when the browser saw no stream: ask yt-dlp (if installed) what the page's video is."""
    exe = shutil.which("yt-dlp")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-J", "--no-playlist", "--no-warnings", "-f", "best[protocol^=m3u8]/best[ext=mp4]/best",
                              page_url], capture_output=True, text=True, timeout=45, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise CastError("yt-dlp took too long looking at that page.")
    if out.returncode != 0 or not out.stdout.strip():
        return None
    info = json.loads(out.stdout)
    if info.get("_type") == "playlist":
        info = next((e for e in info.get("entries") or [] if e), {})
    if info.get("has_drm"):
        raise CastError("That video is DRM-protected, which CapoCast won't cast.")
    url = info.get("url")
    if not url:
        return None
    kind = "hls" if "m3u8" in (info.get("protocol") or "") else "mp4" if info.get("ext") == "mp4" else None
    if not kind:
        raise CastError(f"The video is {info.get('ext') or 'in a format'} that CapoCast can't relay yet (it handles "
                        "HLS, DASH and MP4).")
    return Candidate(url=url, kind=kind, headers=info.get("http_headers") or {},
                     duration=float(info.get("duration") or 0), height=int(info.get("height") or 0),
                     live=bool(info.get("is_live")))


class Tv:
    """What WebSync needs from the Roku, bound to whatever IP is in the box right now."""

    def __init__(self, remote, host_fn):
        self.remote, self.host_fn = remote, host_fn

    def state(self):
        return self.remote.state(self.host_fn())

    def key(self, name):
        self.remote.key(self.host_fn(), name)

    def cast(self, url):
        self.remote.cast_url(self.host_fn(), url)


class WebTab:
    def __init__(self, app, page, ui, home=None):
        self.app, self.page, self.ui = app, page, ui
        self.home = home or HOME
        self.guard = WebGuard(ui.log, home=self.home)
        self.proc = None
        self.relay = None
        self.sync = None                                  # follows the page's video on the TV after a cast
        self.tv = Tv(ui.Remote, lambda: app._host)
        self.profile = brave_profile.profile_dir()
        self.visible = self.busy = False
        self.launched_at = 0.0
        self.filters_ok = True
        self.note, self.note_until = ("", ui.MUTED), 0.0
        self._build()
        app.after(250, self._tick)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    # ---- layout ------------------------------------------------------------------
    def _build(self):
        ui = self.ui
        bar = tk.Frame(self.page, bg=ui.BG)
        bar.pack(fill="x", padx=16, pady=(10, 4))
        for kind, cmd in (("back", lambda: self.guard.back()), ("fwd", lambda: self.guard.forward()),
                          ("reload", lambda: self.guard.reload())):
            ui.NavButton(bar, kind, cmd).pack(side="left", padx=(0, 2))
        self.url = tk.StringVar()
        self.entry = tk.Entry(bar, textvariable=self.url, bg=ui.CARD, fg=ui.FG, insertbackground=ui.FG, relief="flat",
                              font=(ui.FONT, 11), highlightthickness=1, highlightbackground=ui.EDGE,
                              highlightcolor=ui.ACCENT)
        self.entry.pack(side="left", fill="x", expand=True, padx=(10, 12), ipady=7, ipadx=8)
        self.entry.bind("<Return>", self._go)
        self.entry.bind("<FocusIn>", lambda e: self.entry.after(1, lambda: self.entry.select_range(0, "end")))
        self.cast_btn = ui.RoundButton(bar, "Cast to TV", self.cast, width=130, height=38)
        self.cast_btn.pack(side="left")

        strip = tk.Frame(self.page, bg=ui.BG)
        strip.pack(fill="x", padx=18, pady=(0, 6))
        self.status = tk.Label(strip, text="", bg=ui.BG, fg=ui.MUTED, font=(ui.FONT, 9), anchor="w")
        self.status.pack(side="left")
        self.allow = tk.Label(strip, text="", bg=ui.BG, fg=ui.ACCENT, font=(ui.FONT, 9, "bold"), cursor="hand2")
        self.allow.bind("<Button-1>", lambda e: self.guard.allow_last_block())
        self.stop_lbl = tk.Label(strip, text="Stop casting", bg=ui.BG, fg=ui.ACCENT, font=(ui.FONT, 9, "bold"),
                                 cursor="hand2")
        self.stop_lbl.bind("<Button-1>", lambda e: self.stop_casting())

        self.host = tk.Frame(self.page, bg=ui.BG)
        self.host.pack(fill="both", expand=True)
        self.view = PageView(self.host, lambda: self.guard, ui.BG, self._resized)
        self.view.pack(fill="both", expand=True)
        self.msg = tk.Label(self.host, text="", bg=ui.BG, fg=ui.MUTED, font=(ui.FONT, 11), justify="center")
        self.msg.place(relx=0.5, rely=0.45, anchor="center")
        self.msg.bind("<Button-1>", lambda e: self.launch())

    def _message(self, text, clickable=False):
        self.msg.config(text=text, fg=self.ui.FG if clickable else self.ui.MUTED, cursor="hand2" if clickable else "")

    def _note(self, text, color=None, seconds=8):
        self.note, self.note_until = (text, color or self.ui.MUTED), time.time() + seconds

    def _resized(self, w, h):
        self.guard.set_view(w, h, self.visible)

    # ---- tab lifecycle ---------------------------------------------------------------
    def on_show(self):
        self.visible = True
        w, h = self.view.winfo_width(), self.view.winfo_height()
        if w > 50 and h > 50:
            self.guard.set_view(w, h, True)
        if not self.alive():
            self.launch()

    def on_hide(self):
        self.visible = False
        w, h = self.view.winfo_width(), self.view.winfo_height()
        if w > 50 and h > 50 and self.guard._view:
            self.guard.set_view(w, h, False)             # stop streaming frames nobody is looking at

    def launch(self):
        if self.alive():
            return
        exe = brave_profile.find_brave()
        if not exe:
            self._message("The Web tab needs the Brave browser (it blocks the ads).\n\n"
                          "Install it with:  winget install Brave.Brave\nthen click here.", clickable=True)
            return
        self._message("Opening the Web tab…")
        try:
            self.filters_ok = brave_profile.seed_filters(self.profile, self.ui.log)
            try:
                os.remove(brave_profile.port_file(self.profile))
            except OSError:
                pass
            self.guard.stop()
            self.guard = WebGuard(self.ui.log, home=self.home)         # fresh state for the new browser
            self.view.reset()
            w, h = self.view.winfo_width(), self.view.winfo_height()
            self.guard.set_view(w if w > 50 else 1280, h if h > 50 else 700, self.visible)
            self.proc = subprocess.Popen([exe, *brave_profile.launch_args(self.profile, self.home)],
                                         creationflags=NO_WINDOW)
            self.launched_at = time.time()
        except Exception as e:
            self.ui.log(f"web launch failed: {e!r}")
            self._message(f"{e}  ·  click to retry", clickable=True)

    def _tick(self):
        try:
            if self.proc is not None and self.proc.poll() is not None:
                self.proc = None
                self.guard.stop()
                self._message("The browser stopped  ·  click to reopen", clickable=True)
            if self.alive():
                if not self.guard.running and not self.guard.error:
                    pf = brave_profile.port_file(self.profile)
                    if os.path.exists(pf):
                        try:
                            self.guard.attach(pf)
                        except Exception as e:
                            self.ui.log(f"web guard attach failed: {e!r}")
                            self.guard.error = "Protection couldn't start"
                    elif time.time() - self.launched_at > 40:
                        self._message("Couldn't open the Web tab  ·  click to retry", clickable=True)
                        self.proc.terminate()
                if self.view.has_frame and self.msg.cget("text"):
                    self._message("")
            self._refresh()
        except Exception as e:
            self.ui.log(f"web tab error: {e!r}")
        self.app.after(250, self._tick)

    # ---- status line ---------------------------------------------------------------------
    def _refresh(self):
        ui = self.ui
        snap = self.guard.snapshot()
        if snap["url"] and self.app._focused() is not self.entry and self.url.get() != snap["url"]:
            self.url.set(snap["url"])
        if time.time() < self.note_until:
            text, color = self.note
        elif not self.alive():
            text, color = "", ui.MUTED
        elif snap["error"] or self.guard.error:
            text, color = (snap["error"] or self.guard.error) + " - pop-up and redirect protection is off", ui.RED
        elif not snap["running"]:
            text, color = "Starting protection…", ui.AMBER
        else:
            s, parts = snap["stats"], []
            if s["popups"]:
                parts.append(f"{s['popups']} pop-up{'s' * (s['popups'] > 1)} blocked")
            if s["redirects"]:
                parts.append(f"{s['redirects']} redirect{'s' * (s['redirects'] > 1)} stopped")
            if s["overlays"]:
                parts.append(f"{s['overlays']} click trap{'s' * (s['overlays'] > 1)} removed")
            if s["ads"]:
                parts.append(f"{s['ads']} ad requests blocked")
            if snap["streams"]:
                parts.append(f"{snap['streams']} video stream{'s' * (snap['streams'] > 1)} found")
            text = "  ·  ".join(parts) or "Protection on"
            color = ui.GREEN
            if not self.filters_ok:
                text = "Ad filters missing: open Brave once so it can download them  ·  " + text
                color = ui.AMBER
        self.status.config(text=text, fg=color)
        lb = snap["last_block"]
        if lb and lb["what"] in ("redirect", "pop-up") and lb.get("url") and time.time() >= self.note_until:
            self.allow.config(text=f"   Blocked {urlparse(lb['url']).hostname or lb['url']}  ·  Allow")
            if not self.allow.winfo_ismapped():
                self.allow.pack(side="left")
        else:
            self.allow.pack_forget()
        following = bool(self.sync and self.sync.active)
        if following:
            if not self.stop_lbl.winfo_ismapped():
                self.stop_lbl.pack(side="right")
        else:
            self.stop_lbl.pack_forget()
        if self.busy:
            self.cast_btn.style("Casting…", ui.ACCENT, ui.ACCENT_HI, enabled=False)
        else:
            self.cast_btn.style("Cast to TV", ui.ACCENT, ui.ACCENT_HI)

    def stop_casting(self):
        """Stop following the page, give the page its sound back, and leave the RokuCast channel on the TV."""
        if self.sync:
            self.sync.stop("Stopped")
        threading.Thread(target=self._tv_back, daemon=True).start()

    def _tv_back(self):
        try:
            self.tv.key("Back")
        except Exception as e:
            self.ui.log(f"stop casting: {e!r}")

    def _following_stopped(self, reason):
        """Called from WebSync's thread when it gives up (page left, TV stopped, or you pressed Stop)."""
        if reason and reason != "Stopped":
            self._post(lambda: self._note(reason, self.ui.AMBER, 12))
        elif reason == "Stopped":
            self._post(lambda: self._note("Stopped casting", self.ui.MUTED, 6))

    def _go(self, _e=None):
        self.guard.navigate(normalize_address(self.url.get(), self.home))
        self.view.focus_set()

    # ---- casting -------------------------------------------------------------------------------
    def cast(self):
        if self.busy:
            return
        snap = self.guard.snapshot()
        if not self.alive() or not snap["running"]:
            return self._note("Wait for the page to finish opening, then try again.", self.ui.AMBER)
        refusal = drm.refusal_for(snap["url"])
        if refusal:
            return messagebox.showinfo("Can't cast this", refusal, parent=self.app)
        self.busy = True
        if self.sync:
            self.sync.stop("", quiet=True)                # a new cast replaces the old one
        self._note("Looking for the video…", self.ui.AMBER, 60)
        threading.Thread(target=self._guarded, args=(self._find, snap["url"], snap["title"]), daemon=True).start()

    def _post(self, fn):
        try:
            self.app.after(0, fn)
        except RuntimeError:        # window already closed
            pass

    def _guarded(self, fn, *args):
        try:
            fn(*args)
        except CastError as e:
            self._post(lambda: self._fail(str(e)))
        except Exception as e:
            self.ui.log(f"cast failed: {e!r}")
            self._post(lambda: self._fail(f"Something went wrong: {e}"))

    def _fail(self, message):
        self.busy = False
        self._note(message.splitlines()[0], self.ui.RED, 12)
        if len(message) > 90 or "\n" in message:
            messagebox.showinfo("Can't cast this", message, parent=self.app)

    def _find(self, page_url, title):
        """Worker thread: decide which stream is the video."""
        cands = self.guard.candidates()
        apply_page_videos(cands, self.guard.page_videos())
        active = self.guard.active_frames()
        covered, pool, drm_seen = set(), [], False
        for c in sorted(cands, key=lambda c: score(c, active), reverse=True)[:5]:
            if c.kind in ("hls", "dash"):
                try:
                    covered.update(probe_hls(c) if c.kind == "hls" else probe_dash(c))
                except DrmError:
                    drm_seen = True
                    continue
                except Exception as e:
                    self.ui.log(f"probe failed for {c.url[:100]}: {e!r}")
                    try:                    # some CDNs refuse a first request; one more try before giving up on measuring
                        time.sleep(0.6)
                        covered.update(probe_hls(c) if c.kind == "hls" else probe_dash(c))
                    except DrmError:
                        drm_seen = True
                        continue
                    except Exception:
                        c.measured = False  # keep it (it may still play) but rank it below streams we could read
            pool.append(c)
        norm = lambda u: urlparse(u)._replace(query="", fragment="").geturl()     # tokens in the query differ per request
        covered = {norm(u) for u in covered}
        for c in pool:
            c.covered = norm(c.url) in covered
        decision = choose(pool, active, unsupported=bool(self.guard.snapshot()["unsupported"]))
        if not decision.best:
            if drm_seen:
                raise CastError("That video is DRM-protected, which CapoCast won't cast.\n\n"
                                "Use the service's own Roku channel instead.")
            self._post(lambda: self._note("No stream seen; asking yt-dlp…", self.ui.AMBER, 60))
            fallback = ytdlp_candidate(page_url)
            if not fallback:
                if decision.unsupported:
                    raise CastError("This site streams in a format CapoCast can't relay yet (it handles HLS, DASH "
                                    "and MP4, not WebM or MKV).")
                raise CastError("No video found yet. Press play on the video, then Cast.")
            decision.best = fallback
        if decision.close and not decision.playing:
            # Several streams fit and none is being played, so there's nothing to tell them apart by. Asking you to
            # pick from a list you can't judge is worse than a second of play: the player's own downloads decide it.
            raise CastError("I can't tell which stream is the video yet.\n\nPress play on the video, let it run for a "
                            "few seconds, then press Cast.")
        if decision.close:
            options = [decision.best] + decision.close
            self._post(lambda: self._pick(options, title))
            return
        self._send(decision.best, title)

    def _pick(self, options, title):
        """Two or more streams look equally like the video: let the user choose."""
        ui = self.ui
        win = tk.Toplevel(self.app)
        win.title("Which one is the video?")
        win.configure(bg=ui.BG)
        win.transient(self.app)
        win.resizable(False, False)
        tk.Label(win, text="More than one stream could be the video.\nPick the one that matches (length is the best clue).",
                 bg=ui.BG, fg=ui.FG, font=(ui.FONT, 10), justify="left").pack(padx=20, pady=(16, 8), anchor="w")
        lb = tk.Listbox(win, width=70, height=len(options), bg=ui.CARD, fg=ui.FG, selectbackground=ui.ACCENT,
                        relief="flat", font=(ui.FONT, 10), activestyle="none", highlightthickness=1,
                        highlightbackground=ui.EDGE)
        for c in options:
            lb.insert("end", c.label())
        lb.selection_set(0)
        lb.pack(padx=20)

        def done(choice):
            win.destroy()
            if choice is None:
                self.busy = False
                return self._note("")
            threading.Thread(target=self._guarded, args=(self._send, choice, title), daemon=True).start()

        row = tk.Frame(win, bg=ui.BG)
        row.pack(pady=16)
        ui.RoundButton(row, "Cast", lambda: done(options[(lb.curselection() or (0,))[0]]), width=110, height=36).pack(
            side="left", padx=6)
        cancel = ui.RoundButton(row, "Cancel", lambda: done(None), width=110, height=36)
        cancel.style("Cancel", "#26262b", "#34343a")
        cancel.pack(side="left", padx=6)
        win.protocol("WM_DELETE_WINDOW", lambda: done(None))
        win.grab_set()

    def _ensure_relay(self, host):
        try:
            ip = local_ip_for(host)
        except OSError as e:
            raise CastError(f"Can't find this PC's network address to reach the Roku ({e}).")
        if self.relay and self.relay.server and self.relay.ip == ip:
            return self.relay
        if self.relay:
            self.relay.stop()
        self.relay = MediaRelay(ip, allow=lambda: self.app._host, log=self.ui.log)
        try:
            self.relay.start()
        except OSError as e:
            self.relay = None
            raise CastError(f"Couldn't start the stream server: {e}")
        return self.relay

    def _send(self, cand, title):
        """Worker thread: hand the stream to the relay, tell the Roku to play it, and confirm it arrived."""
        ui, host = self.ui, self.app._host
        relay = self._ensure_relay(host)
        # Start where the page already is, not at 0:00. The TV takes a few seconds to start, so aim a little ahead.
        page_t = self.guard.video_time(cand.duration)
        start = (page_t + (self.sync.lead if self.sync else 3.0)) if (cand.kind == "hls" and not cand.live
                                                                    and page_t and page_t > 5) else 0.0
        self._fresh_channel(host)
        url = relay.add(cand.url, cand.headers, cand.kind, start)
        sess = relay.session_for(url)
        ui.log(f"cast {cand.kind} {cand.url[:140]} -> {url} (from {start:.0f}s)")
        self._post(lambda: self._note("Sending to the TV…", ui.AMBER, 60))
        try:
            ui.Remote.cast_url(host, url)
        except Exception as e:
            raise CastError(str(e))
        self._post(self.app.autocast_off)             # don't let YouTube's auto-cast take the TV back
        deadline = time.time() + 20
        while time.time() < deadline and not sess.hits and not sess.error:
            time.sleep(0.4)
        if sess.error:
            raise CastError(sess.error)
        if not sess.hits:
            raise CastError(
                "The TV never asked this PC for the video.\n\nWindows Firewall is the usual cause: when Windows asks, "
                "allow CapoCast (or Python) on private networks. Also check the Roku IP in the Listen tab.")
        label = title or urlparse(cand.url).hostname or "video"
        # From here the page's video is a muted remote for the TV: pause, play and scrubbing follow.
        end = time.time() + 8
        while sess.actual_start is None and time.time() < end:       # a trimmed HLS stream says where it begins
            time.sleep(0.2)
        if self.sync is None:
            self.sync = WebSync(self.guard, self.tv, self._relay_add, self.ui.log, self._following_stopped)
        self.sync.start(cand, offset=sess.actual_start or 0.0, page_time=page_t, title=label)
        self._post(lambda: self._cast_done(label))
        self._watch_health(sess)

    def _fresh_channel(self, host):
        """Recasting while the RokuCast channel is still up from the last cast hands the new stream to a player that's
        mid-way through the old one, and the TV can come back with no sound. Back out of the channel first so every
        cast starts on a clean player."""
        if self.ui.Remote.active_app(host) != "dev":
            return
        try:
            self.tv.key("Back")
        except Exception as e:
            self.ui.log(f"fresh start: Back failed: {e!r}")
            return
        end = time.time() + 4
        while time.time() < end and self.ui.Remote.active_app(host) == "dev":
            time.sleep(0.25)
        time.sleep(0.6)

    def _watch_health(self, sess):
        """For half a minute after a cast, notice if the stream is quietly failing (segments refused or expired),
        which is how a cast ends up silent or frozen part-way."""
        end = time.time() + 30
        while time.time() < end and self.sync is not None and self.sync.active:
            time.sleep(2)
            if len(sess.failures) >= 3:
                codes = sorted({c for c, _ in sess.failures})
                self.ui.log(f"stream failing: {sess.failures[:6]}")
                self._post(lambda: self._note(
                    "Parts of this stream are failing to load" + (f" (HTTP {codes[0]})" if codes[0] else "")
                    + ". Reload the page and Cast again.", self.ui.AMBER, 20))
                return

    def _relay_add(self, url, headers, kind, start):
        relay = self._ensure_relay(self.app._host)
        public = relay.add(url, headers, kind, start)
        return public, relay.session_for(public)

    def _cast_done(self, label):
        self.busy = False
        self._note("On TV  ·  " + (label if len(label) <= 52 else label[:51] + "…") + "  ·  following this page",
                   self.ui.GREEN, 20)

    def close(self):
        """Shut the browser down cleanly so it saves the profile (sign-ins), then make sure it's gone."""
        if self.sync:
            self.sync.stop("", quiet=True)
        try:
            self.guard.close_browser()
            time.sleep(0.6)
        except Exception:
            pass
        self.guard.stop()
        if self.relay:
            self.relay.stop()
        if self.proc is not None:
            try:
                self.proc.wait(3)
            except Exception:
                self.proc.terminate()
            self.proc = None
