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
import math
import os
import shutil
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog
from urllib.parse import urlparse

import brave_profile
import discord_presence
import drm
import upscaler
from media_relay import MediaRelay, probe_all
from next_episode import NextEpisode
from roku_listen import local_ip_for
from stream_finder import Candidate, apply_page_videos, choose, score
from web_guard import WebGuard, normalize_address
from web_sync import WebSync
from web_view import PageView

HOME = "https://duckduckgo.com"
UPSCALE_NAMES = {"off": "Off", "fast": "Anime4K", "hq": "Anime4K HQ", "movie": "Movie"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class CastError(Exception):
    """A problem to tell the user about, in plain words."""


class Cancelled(Exception):
    """You pressed Cancel: this cast's worker stops where it is."""


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
    discord, cast_since = None, 0.0                       # defaults for anything built without __init__
    upscale_owner, tv_gone, cast_image, cast_session = None, 0, None, None
    cast_site = ""                                           # the site being cast from, for the Discord status

    SOURCE = "web"                                        # this tab's name in the shared Discord status

    def __init__(self, app, page, ui, home=None, upscale=None, discord=None):
        self.app, self.page, self.ui = app, page, ui
        self.discord = discord                            # discord_presence.Status, shared with the YouTube tab
        self.cast_since = 0.0                             # when the current cast began, for Discord's timer
        self.home = home or HOME
        self.upscale = upscale if upscale in UPSCALE_NAMES else "off"
        self.upscaled = None                              # what the TV's upscaled cast was made with, while there is one
        self.guard = WebGuard(ui.log, home=self.home)
        self.proc = None
        self.relay = None
        self.sync = None                                  # follows the page's video on the TV after a cast
        self.tv = Tv(ui.Remote, lambda: app._host)
        self.profile = brave_profile.profile_dir()
        self.visible = self.busy = False
        self.attempt, self.cast_at = 0, 0.0               # which cast is current; a worker from an older one stands down
        self.next, self.cast_page = NextEpisode(), ""     # follows the page on to the next episode
        self.launched_at = 0.0
        self.filters_ok = True
        self.note, self.note_until = ("", ui.MUTED), 0.0
        self._build()
        app.after(250, self._tick)

    @property
    def discord_on(self):
        return bool(self.discord and self.discord.is_on(self.SOURCE))

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
        # Start page: "Home" goes to it, "Set as home" makes the page you're on the one the Web tab opens with.
        self.sethome_lbl = tk.Label(strip, text="Set as home", bg=ui.BG, fg=ui.MUTED, font=(ui.FONT, 9), cursor="hand2")
        self.sethome_lbl.pack(side="right")
        self.sethome_lbl.bind("<Button-1>", lambda e: self.set_home())
        self.home_lbl = tk.Label(strip, text="Home", bg=ui.BG, fg=ui.MUTED, font=(ui.FONT, 9), cursor="hand2")
        self.home_lbl.pack(side="right", padx=(0, 12))
        self.home_lbl.bind("<Button-1>", lambda e: self.go_home())
        self.upscale_lbl = tk.Label(strip, text="", bg=ui.BG, font=(ui.FONT, 9), cursor="hand2")
        self.upscale_lbl.pack(side="right", padx=(0, 12))
        self.upscale_lbl.bind("<Button-1>", lambda e: self.next_upscale())
        self._paint_upscale()
        self.discord_lbl = tk.Label(strip, text="", bg=ui.BG, font=(ui.FONT, 9), cursor="hand2")
        self.discord_lbl.pack(side="right", padx=(0, 12))
        self.discord_lbl.bind("<Button-1>", lambda e: self.toggle_discord())
        self._paint_discord()
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
            if self.sync and self.sync.active:
                self.sync.stop(WebSync.BROWSER_GONE)
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
                if self.sync and self.sync.active:
                    self.sync.stop(WebSync.BROWSER_GONE)       # nothing can report the page's video any more
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
                self._watch_next()
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
        self._paint_cast_button()

    def _paint_cast_button(self):
        if self.busy or self.next.counting:
            self.cast_btn.style("Cancel", "#26262b", "#34343a")
        else:
            self.cast_btn.style("Cast to TV", self.ui.ACCENT, self.ui.ACCENT_HI)

    def _paint_upscale(self):
        on = self.upscale != "off"
        self.upscale_lbl.config(text="Upscale: " + UPSCALE_NAMES[self.upscale], fg=self.ui.ACCENT if on else self.ui.MUTED)

    def _paint_discord(self):
        self.discord_lbl.config(text="Discord: " + ("On" if self.discord_on else "Off"),
                                fg=self.ui.ACCENT if self.discord_on else self.ui.MUTED)

    def toggle_discord(self):
        """Show what's on the TV as your Discord status. Off until you turn it on, and asks for the application ID
        the first time: Discord only shows a status for an application you've registered."""
        if self.discord is None:
            return
        if self.discord.enabled.get(self.SOURCE):
            self.discord.enable(self.SOURCE, False)
        else:
            if not self.discord.client_id:
                found, problem = discord_presence.ask_application_id(self.app)
                if problem:
                    return self._note(problem, self.ui.AMBER, 10)
                if not found:
                    return
                self.discord.set_id(found)
            self.discord.enable(self.SOURCE, True)
            if not discord_presence.discord_running():
                self._note("Discord isn't open. The status will appear once it is.", self.ui.AMBER, 8)
            if self.sync is not None and self.sync.active:       # already casting: show it now, Discord open or not
                self._set_presence(self.sync.title or "Watching", self.cast_image, self.cast_site)
        self._paint_discord()
        save = getattr(self.app, "_save", None)
        if save:
            save()

    def _set_presence(self, title, image=None, site=""):
        """Show `title` as the Discord status, laid out like the YouTube one: the name of what's playing, the site it's
        on (and the upscaling, if any) underneath, and its thumbnail beside them. None takes the status down. Does
        nothing when it's off."""
        if self.discord is None:
            return
        if title is None:
            self.discord.clear(self.SOURCE)
            return
        mode = (self.upscaled or {}).get("mode")
        state = f"on {site}" if site else "Casting to the TV"
        if mode:
            state += f" · 4K upscale ({UPSCALE_NAMES[mode]})"
        self.discord.show(self.SOURCE, title, state, self.cast_since or None, image,
                          image_text=f"{title} · {site}" if site else title)

    def next_upscale(self):
        """Clicking the label steps through Off, Anime4K, Anime4K HQ and Movie. It applies from the next cast."""
        names = list(UPSCALE_NAMES)
        self.upscale = names[(names.index(self.upscale) + 1) % len(names)]
        if self.upscale != "off":
            why = upscaler.missing()
            if why:
                self.upscale = "off"
                self._note(why, self.ui.AMBER, 12)
        self._paint_upscale()
        save = getattr(self.app, "_save", None)
        if save:
            save()
        if self.upscale != "off":
            self._note(f"Next cast: upscaled to 4K ({UPSCALE_NAMES[self.upscale]}) on this PC's GPU", self.ui.MUTED, 6)

    def stop_casting(self):
        """Stop following the page, give the page its sound back, and leave the RokuCast channel on the TV."""
        self.next.disarm()
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
        self._post(self.next.grace if reason in (WebSync.LEFT_PAGE, WebSync.FINISHED) else self.next.disarm)
        if reason in ("Stopped", WebSync.FINISHED, WebSync.TV_STOPPED):
            self._set_presence(None)                      # leaving the page doesn't count: the TV is still playing it
        if self.relay and reason in ("Stopped", WebSync.FINISHED, WebSync.TV_STOPPED):
            self.upscaled = None
            self.relay.stop_upscaling()                   # leaving the page doesn't count: the TV is still playing it
        if reason and reason != "Stopped":
            self._post(lambda: self._note(reason, self.ui.AMBER, 12))
        elif reason == "Stopped":
            self._post(lambda: self._note("Stopped casting", self.ui.MUTED, 6))

    def tv_state(self, st):
        """Fed by the app's once-a-second look at the TV (None if it can't be reached). WebSync notices the TV leaving
        while the page is being followed; this covers the rest: you left the page, or a recast failed, and the TV has
        since gone somewhere else. Then the Discord status comes down and the encoder stops, rather than both carrying
        on for a cast nobody is watching."""
        left_over = bool(self.upscaled or (self.discord and self.discord.showing == self.SOURCE))
        following = self.sync is not None and self.sync.active
        if st is None or following or self.busy or not left_over:
            self.tv_gone = 0
            return
        on_cast = st["state"] != "none" and st.get("app") in ("RokuCast", "")
        self.tv_gone = 0 if on_cast else self.tv_gone + 1
        if self.tv_gone < 2:                              # one odd reading while the TV switches isn't enough
            return
        self.tv_gone, self.upscaled = 0, None
        self._set_presence(None)
        if self.relay:
            threading.Thread(target=self.relay.stop_upscaling, daemon=True).start()

    def go_home(self):
        self.guard.navigate(self.home)

    def set_home(self):
        """Make the page you're on the Web tab's start page (saved, and used every time the tab opens)."""
        url = self.guard.snapshot()["url"]
        if not url.startswith(("http://", "https://")):
            return self._note("Open a page first, then set it as your home page.", self.ui.AMBER)
        self.home = self.guard.home = url
        save = getattr(self.app, "_save", None)
        if save:
            save()
        self._note("Home page set to " + (urlparse(url).hostname or url), self.ui.GREEN, 6)

    def _go(self, _e=None):
        self.guard.navigate(normalize_address(self.url.get(), self.home))
        self.view.focus_set()

    # ---- casting -------------------------------------------------------------------------------
    def _watch_next(self):
        """The page has moved on to the next episode: warn, then take the TV there too."""
        if self.busy:
            return
        snap = self.guard.snapshot()
        left = self.next.tick(snap["url"], self.guard.page_videos(), self.guard.has_fresh_stream())
        if left is None:
            return
        if left > 0:
            title = snap["title"] if len(snap["title"]) <= 52 else snap["title"][:51] + "…"
            return self._note(f"Up next{': ' + title if title else ''}  ·  on the TV in {math.ceil(left)}s",
                              self.ui.GREEN, 1)
        self.ui.log(f"next episode: following the page to {snap['url'][:100]}")
        self.next.disarm()
        self.cast()

    def cast(self):
        if self.busy:
            return self.cancel_cast()
        snap = self.guard.snapshot()
        if self.next.counting:
            self.next.skip(snap["url"])
            return self._note("Staying on what's playing", self.ui.MUTED, 6)
        if not self.alive() or not snap["running"]:
            return self._note("Wait for the page to finish opening, then try again.", self.ui.AMBER)
        refusal = drm.refusal_for(snap["url"])
        if refusal:
            return messagebox.showinfo("Can't cast this", refusal, parent=self.app)
        self.busy, self.cast_at, self.cast_page = True, time.time(), snap["url"]
        self.attempt += 1
        self.next.disarm()
        if self.sync:
            self.sync.stop("", quiet=True)                # a new cast replaces the old one
        self._note("Looking for the video…", self.ui.AMBER, 60)
        threading.Thread(target=self._guarded, args=(self.attempt, self._find, snap["url"], snap["title"]),
                         daemon=True).start()

    def cancel_cast(self):
        if time.time() - self.cast_at < 0.6:              # the second half of a double-click on Cast
            return
        self.attempt += 1
        self.busy = False
        self._note("Cast cancelled", self.ui.MUTED, 6)

    def _check(self, attempt):
        if attempt != self.attempt:
            raise Cancelled()

    def _post(self, fn):
        try:
            self.app.after(0, fn)
        except RuntimeError:        # window already closed
            pass

    def _guarded(self, attempt, fn, *args):
        try:
            fn(attempt, *args)
        except Cancelled:
            pass
        except CastError as e:
            message = str(e)                              # Python deletes `e` before the UI gets to run the lambda
            self._post(lambda: self._fail(attempt, message))
        except Exception as e:
            self.ui.log(f"cast failed: {e!r}")
            message = f"Something went wrong: {e}"
            self._post(lambda: self._fail(attempt, message))

    def _fail(self, attempt, message):
        if attempt != self.attempt:
            return
        self.ui.log(f"cast failed: {message.splitlines()[0]}")
        self.busy = False
        self._note(message.splitlines()[0], self.ui.RED, 12)
        if len(message) > 90 or "\n" in message:
            messagebox.showinfo("Can't cast this", message, parent=self.app)

    def _find(self, attempt, page_url, title):
        """Worker thread: decide which stream is the video."""
        cands = self.guard.candidates()
        apply_page_videos(cands, self.guard.page_videos())
        active = self.guard.active_frames()
        top = sorted(cands, key=lambda c: score(c, active), reverse=True)[:5]
        t0 = time.time()
        covered, drm = probe_all([c for c in top if c.kind in ("hls", "dash")], self.ui.log)
        self.ui.log(f"find: {len(cands)} streams, measured {len(top)} in {time.time() - t0:.1f}s")
        self._check(attempt)
        drm_seen = bool(drm)
        pool = [c for c in top if not any(c is d for d in drm)]
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
            self._check(attempt)
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
            self._post(lambda: self._pick(attempt, options, title))
            return
        self._send(attempt, decision.best, title)

    def _pick(self, attempt, options, title):
        """Two or more streams look equally like the video: let the user choose."""
        if attempt != self.attempt:
            return
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
            threading.Thread(target=self._guarded, args=(attempt, self._send, choice, title), daemon=True).start()

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

    def _send(self, attempt, cand, title):
        """Worker thread: hand the stream to the relay, tell the Roku to play it, and confirm it arrived. If the cast
        fails or is cancelled after the encoder started, the encoder stops too: left alone it would keep the GPU busy
        until it was minutes ahead of a TV that never got the stream."""
        try:
            self._send_cast(attempt, cand, title)
        except BaseException:
            if self.relay and getattr(self, "upscale_owner", None) == attempt:
                self.upscale_owner, self.upscaled = None, None
                self.relay.stop_upscaling()
            raise

    def _send_cast(self, attempt, cand, title):
        self._check(attempt)
        ui, host = self.ui, self.app._host
        relay = self._ensure_relay(host)
        relay.stop_upscaling()                            # whatever was being made for the last cast
        self.upscaled = None
        plan = self._upscale_plan(cand)
        self._check(attempt)
        # Start where the page already is, not at 0:00. The TV takes a few seconds to start, so aim a little ahead.
        page_t = self.guard.video_time(cand.duration)
        start = (page_t + (self.sync.lead if self.sync else 3.0)) if ((cand.kind == "hls" or plan) and not cand.live
                                                                    and page_t and page_t > 5) else 0.0
        self._fresh_channel(host)
        self._check(attempt)
        url = self._start_upscale(attempt, relay, cand, start, plan) if plan else None
        if not url:
            url = relay.add(cand.url, cand.headers, cand.kind, start)
        sess = relay.session_for(url)
        ui.log(f"cast {cand.kind} {cand.url[:140]} -> {url} (from {start:.0f}s)")
        self._post(lambda: self._note("Sending to the TV…", ui.AMBER, 60))
        try:
            ui.Remote.cast_url(host, url)
        except Exception as e:
            raise CastError(str(e))
        self._post(self.app.autocast_off)             # don't let YouTube's auto-cast take the TV back
        try:
            self._await_tv(attempt, sess)
        except Cancelled:
            self._tv_back()                           # the TV had already been told to play this
            raise
        label, self.cast_site, self.cast_image = self._describe(title, cand)
        # From here the page's video is a muted remote for the TV: pause, play and scrubbing follow.
        if self.sync is None:
            self.sync = WebSync(self.guard, self.tv, self._relay_add, self.ui.log, self._following_stopped)
        self.sync.guard = self.guard                  # Brave may have been restarted since the last cast
        self.sync.start(cand, offset=sess.actual_start or 0.0, page_time=page_t, title=label,
                        restartable=bool(self.upscaled))
        self._post(lambda: self._cast_done(attempt, label))
        self._watch_health(attempt, sess)

    def _describe(self, title, cand):
        """(name, site, thumbnail) for what's now on the TV. The page's live title is used if it is still the page that
        was cast from (a single-page site changes it as you move between episodes); the title seen when Cast was
        pressed otherwise."""
        info = self.guard.page_info(2.0)
        defrag = lambda u: urlparse(u or "")._replace(fragment="").geturl()
        same = not info or defrag(info.get("url")) == defrag(self.cast_page)
        live = (info.get("title") or info.get("og_title") or "").strip() if info and same else ""
        site = discord_presence.site_label(self.cast_page, info.get("site", "") if info and same else "")
        name = discord_presence.clean_title(live or title, site, self.cast_page)
        image = self.guard.page_image(2.0, cand.duration) if same else ""
        return name or urlparse(cand.url).hostname or "video", site, image

    def _upscale_plan(self, cand):
        """Whether this cast can be upscaled: {'mode', 'duration', 'audio'}, or None to cast it as it is (saying why,
        so the switch being on and the TV looking the same is never a mystery)."""
        if self.upscale == "off":
            return None
        ui, why = self.ui, upscaler.missing()
        if not why and cand.live:
            why = "Live streams aren't upscaled"
        info = None
        if not why and (not cand.duration or not cand.height or cand.has_audio is None):
            self._post(lambda: self._note("Reading the video for upscaling…", ui.AMBER, 30))
            info = upscaler.probe(cand.url, cand.headers, cand.kind, ui.log)
        duration = cand.duration or (info or {}).get("duration") or 0
        height = cand.height or (info or {}).get("height") or 0
        audio = cand.has_audio if cand.has_audio is not None else (info or {}).get("audio", True)
        if not why and not duration:
            why = "Can't upscale this one: its length isn't known"
        if not why and cand.has_video is False:
            why = "This stream has no picture to upscale"
        if not why and height >= upscaler.TARGET_H:
            why = "This video is already 4K"
        if why:
            ui.log(f"upscale skipped: {why}")
            self._post(lambda: self._note(why + ", casting it as it is", ui.AMBER, 8))
            return None
        return {"mode": self.upscale, "duration": duration, "audio": bool(audio)}

    def _start_upscale(self, attempt, relay, cand, start, plan):
        """Start the encoder and wait for its first piece. Returns the URL for the TV, or None to cast plainly."""
        ui = self.ui
        self._post(lambda: self._note(f"Upscaling ({UPSCALE_NAMES[plan['mode']]})…", ui.AMBER, 60))
        self.upscale_owner = attempt                      # so a failure of this attempt (and only this one) stops it
        url = relay.add_upscaled(cand.url, cand.headers, cand.kind, start, plan["duration"], plan["mode"], plan["audio"])
        sess = relay.session_for(url)
        try:
            ready = sess.wait_ready(lambda: self._check(attempt))
        except Cancelled:
            relay.stop_upscaling()
            raise
        if ready:
            self.upscaled = {**plan, "session": sess}
            return url
        ui.log(f"upscale failed, casting plainly: {sess.error}")
        relay.stop_upscaling()
        self._post(lambda: self._note("Upscaling failed, casting it as it is", ui.AMBER, 8))
        return None

    def _await_tv(self, attempt, sess):
        """Wait for the TV to fetch the stream and, for a trimmed HLS stream, to learn where it begins."""
        deadline = time.time() + 20
        while time.time() < deadline and not sess.hits and not sess.error:
            self._check(attempt)
            time.sleep(0.4)
        if sess.error:
            raise CastError(sess.error)
        if not sess.hits:
            raise CastError(
                "The TV never asked this PC for the video.\n\nWindows Firewall is the usual cause: when Windows asks, "
                "allow CapoCast (or Python) on private networks. Also check the Roku IP in the Listen tab.")
        end = time.time() + 8
        while sess.actual_start is None and time.time() < end:
            self._check(attempt)
            time.sleep(0.2)
        self._check(attempt)

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

    FAIL_WINDOW, FAIL_LIMIT = 30.0, 3        # this many failed requests within this many seconds is a stream going bad

    def _watch_health(self, attempt, sess, tick=2.0):
        """For as long as this cast is followed, notice if the stream is quietly failing (segments refused or
        expired, which is how a cast ends up silent or frozen part-way), if the upscaler dies, or if it can't keep up.
        A seek moves the cast to a new session, which is followed too."""
        self.cast_session = sess
        current, seen, fails, warned_at, slow = sess, 0, [], 0.0, False
        while self.sync is not None and self.sync.active and attempt == self.attempt:
            time.sleep(tick)
            sess = self.cast_session or sess
            if sess is not current:                           # a scrub restarted the stream
                current, seen, fails, slow = sess, 0, [], False
            now = time.time()
            fresh = list(sess.failures[seen:])
            seen += len(fresh)
            fails = [(t, c) for t, c in fails if now - t < self.FAIL_WINDOW] + [(now, c) for c, _ in fresh]
            if len(fails) >= self.FAIL_LIMIT and now - warned_at > 60:
                warned_at = now
                codes = sorted({c for _, c in fails})
                self.ui.log(f"stream failing: {sess.failures[-6:]}")
                self._post(lambda: self._note(
                    "Parts of this stream are failing to load" + (f" (HTTP {codes[0]})" if codes[0] else "")
                    + ". Reload the page and Cast again.", self.ui.AMBER, 20))
            elif sess.error and now - warned_at > 60 and sess.error != getattr(self, "_reported_error", ""):
                warned_at, self._reported_error = now, sess.error
                reason = sess.error
                self._post(lambda: self._note("The stream stopped: " + reason, self.ui.AMBER, 20))
            if not slow and getattr(sess, "stalls", 0) >= 3 and self.upscaled:
                slow = True                                   # a seek swaps the session in the plan, so this follows it
                self._post(lambda: self._note("Upscaling is running slower than the video, so the TV may buffer. The "
                                              "site may be slow, or the GPU busy: try a lighter Upscale setting.",
                                              self.ui.AMBER, 20))

    def _relay_add(self, url, headers, kind, start):
        relay = self._ensure_relay(self.app._host)
        if self.upscaled:
            plan = self.upscaled
            public = relay.add_upscaled(url, headers, kind, start, plan["duration"], plan["mode"], plan["audio"])
            plan["session"] = relay.session_for(public)
        else:
            public = relay.add(url, headers, kind, start)
        self.cast_session = relay.session_for(public)
        return public, self.cast_session

    def _cast_done(self, attempt, label):
        if attempt != self.attempt:
            return
        self.busy = False
        self.cast_since = time.time()
        self._set_presence(label, self.cast_image, self.cast_site)
        self.next.arm(self.cast_page)
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
