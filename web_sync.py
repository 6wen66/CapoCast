"""Keeps the TV following the page's video after a Web cast, like the YouTube tab does for Playlet.

While a cast is active the page's video is a muted remote control: pausing, playing or scrubbing it moves the TV.

* pause / play  -> the Roku's Play key (it only toggles, so it's pressed only when the TV is in the other state)
* scrub, HLS    -> the TV is restarted on the same stream at the new position (the relay serves a playlist that begins
                   there). The RokuCast channel has no seek command, so a seek is a restart.
* scrub, others -> the Roku's Left / Right keys, 10 s each (MP4 and DASH can't be cut at an arbitrary time, unless
                   the cast is upscaled: that's HLS on the TV whatever the source was, so it restarts like HLS)
* drift         -> the muted PC copy is nudged onto the TV's timeline now and then, so the page's slider is honest

Nothing here touches Tk: the owner passes in the guard (page events, mirror and seek commands), a `tv` object with
state() / key(name) / cast(url), and `relay_add(url, headers, kind, start)` returning (public_url, session).
"""
import queue
import threading
import time


class WebSync:
    SETTLE = 3.0      # after a (re)start the TV still reports its previous state for a moment
    DRIFT = 0.8       # seconds of page-vs-TV difference worth correcting
    RECHECK = 15.0    # seconds between drift checks while both are playing
    SEEK_MIN = 2.0    # a jump smaller than this is playback, not a scrub
    MAX_STEPS = 90    # at most 15 minutes of 10 s key presses
    ENDING = 30.0     # the TV stopping this close to the end is the video finishing, not someone leaving it
    LEFT_PAGE = "You left the page, so the TV is no longer following it"
    FINISHED = "That one finished on the TV"
    TV_STOPPED = "The TV stopped playing this"
    BROWSER_GONE = "The browser closed, so the TV is no longer following the page"

    def __init__(self, guard, tv, relay_add, log=lambda m: None, on_stop=lambda reason: None):
        self.guard, self.tv, self.relay_add, self.log, self.on_stop = guard, tv, relay_add, log, on_stop
        self.active = False
        self.lead = 3.0                 # learned: seconds from "start at t" until the TV is really playing
        self.offset = 0.0               # TV position + offset = position in the video
        self.settle_until = 0.0
        self.title = ""
        self._thread = None

    # ---- lifecycle -----------------------------------------------------------
    def start(self, cand, offset, page_time=None, title="", restartable=False):
        """Begin following. `offset` is where the TV's stream started (HLS cut) and page_time where the page was
        when the cast began (used to line up the TV when the stream couldn't be cut). `restartable` says a seek can
        restart the TV at a new position even though the source isn't HLS."""
        self.stop("", quiet=True)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)             # or it sees active go True again and runs beside the new thread
        self.cand, self.kind, self.dur = cand, cand.kind, float(cand.duration or 0)
        self.restartable = restartable or cand.kind == "hls"
        self.live = bool(cand.live)
        self.offset, self.title = float(offset or 0), title
        self.epoch0 = self.guard.epoch
        self.settle_until = time.time() + self.SETTLE
        self.tv_seen = 0.0              # the furthest into the video the TV has been seen
        self.last_check = self.last_mirror = self.last_alive = time.time()
        self._seek_at = None            # (target, when) of the last seek we acted on
        while True:                     # forget events from before the cast
            try:
                self.guard.page_events.get_nowait()
            except queue.Empty:
                break
        self.active = True
        self.guard.mirror(True)
        self._thread = threading.Thread(target=self._run, args=(page_time,), name="web-sync", daemon=True)
        self._thread.start()

    def stop(self, reason="", quiet=False):
        was = self.active
        self.active = False
        if was:
            try:
                self.guard.mirror(False)             # the page gets its sound back
            except Exception:
                pass
            if not quiet:
                self.on_stop(reason)

    # ---- the loop --------------------------------------------------------------
    def _run(self, page_time):
        try:
            if page_time and page_time > self.SEEK_MIN + 8 and not self.restartable and not self.live:
                self._catch_up(page_time)
            while self.active:
                try:
                    ev = self._next()
                except queue.Empty:
                    self._idle()
                    continue
                self._safe(ev.get("kind"), lambda: self._handle(ev))
        except Exception as e:
            self.log(f"web sync crashed: {e!r}")
            self.stop("The link to the TV broke")

    def _safe(self, what, fn):
        try:
            fn()
        except Exception as e:
            self.log(f"web sync error on {what}: {e!r}")

    def _next(self):
        ev = self.guard.page_events.get(timeout=1.0)
        while ev.get("kind") == "seeked":           # scrubbing fires a burst: only the latest matters
            try:
                nxt = self.guard.page_events.get_nowait()
            except queue.Empty:
                break
            if nxt.get("kind") == "seeked":
                ev = nxt
            else:
                self.guard.page_events.queue.appendleft(nxt)
                break
        return ev

    def _idle(self):
        now = time.time()
        if self.guard.epoch != self.epoch0:
            return self.stop(self.LEFT_PAGE)
        if now - self.last_mirror > 3:              # frames that reloaded since need telling again
            self.last_mirror = now
            self.guard.mirror(True)
        if now - self.last_alive > 5 and now > self.settle_until:
            self.last_alive = now
            st = self.tv.state()
            if st is not None and (st["state"] == "none" or st.get("app") not in ("RokuCast", "")):
                ending = self.dur and self.tv_seen >= self.dur - self.ENDING
                return self.stop(self.FINISHED if ending else self.TV_STOPPED)
            if st is not None:
                self.tv_seen = st["pos"] + self.offset
        if now - self.last_check > self.RECHECK:
            self._safe("align", self._align)

    def _relevant(self, ev):
        """Events from the video we cast, not an ad, a preview thumbnail, or a different video the page swapped in
        (e.g. a "next episode" click) that hasn't reported its own duration yet."""
        if not self.dur:
            return True
        return bool(ev.get("dur")) and abs(ev["dur"] - self.dur) <= 3

    def _handle(self, ev):
        if not self._relevant(ev):
            return
        kind = ev.get("kind")
        self.log(f"web sync {kind} t={ev.get('t'):.1f} paused={ev.get('paused')}" if ev.get("t") is not None else kind)
        if kind == "pause":
            self._ensure("pause")
        elif kind == "play":
            self._ensure("play")
            self._align()
        elif kind == "seeked":
            self._seek(ev["t"], ev.get("paused", False))

    # ---- TV helpers --------------------------------------------------------------
    def _tv_time(self):
        st = self.tv.state()
        if st and st["state"] in ("play", "pause", "buffer"):
            return st["pos"] + self.offset
        return None

    def _ensure(self, want):
        """The Roku Play key toggles, so only press it when the TV is in the other state."""
        time.sleep(max(0.0, self.settle_until - time.time()))
        deadline = time.time() + 8
        while time.time() < deadline and self.active:
            st = self.tv.state()
            state = st and st["state"]
            if state in ("play", "pause"):
                if state != want:
                    self.tv.key("Play")
                return
            time.sleep(0.4)

    def _started(self, t0):
        """Wait until the TV is really playing and its position is moving; learn how long that took."""
        last = None
        while time.time() < t0 + 12 and self.active:
            st = self.tv.state()
            if st and st["state"] == "play" and st.get("app") in ("RokuCast", ""):
                if last is not None and st["pos"] > last:
                    took = time.time() - t0 - 0.1
                    self.lead = min(8.0, max(0.5, 0.6 * self.lead + 0.4 * took))
                    self.log(f"TV playing after {took:.2f}s, lead now {self.lead:.2f}s")
                    return True
                last = st["pos"]
            time.sleep(0.25)
        return False

    def _seek(self, target, paused):
        tv_now = self._tv_time()
        if self._seek_at and abs(target - self._seek_at[0]) < 1.0 and time.time() - self._seek_at[1] < 6:
            return                                   # the page re-reporting our own earlier seek
        if tv_now is not None and abs(target - tv_now) < self.SEEK_MIN + (0 if paused else self.lead / 2):
            return                                   # ordinary playback, not a scrub
        self._seek_at = (target, time.time())
        if self.restartable and not self.live:
            aim = max(0.0, target + (0 if paused else self.lead))
            url, sess = self.relay_add(self.cand.url, self.cand.headers, self.kind, aim)
            t0 = time.time()
            self.tv.cast(url)
            self.settle_until = t0 + self.SETTLE
            end = t0 + 8
            while sess.actual_start is None and time.time() < end and self.active:
                time.sleep(0.2)
            self.offset = sess.actual_start or 0.0   # where in the video the TV's new stream begins
            if paused:
                self._ensure("pause")
            elif self._started(t0):
                self._align()
        elif tv_now is not None and not self.live:
            self._step(target - tv_now + (0 if paused else self.lead))

    def _step(self, delta):
        """Move the TV by about `delta` seconds with its ±10 s keys."""
        steps = max(-self.MAX_STEPS, min(self.MAX_STEPS, round(delta / 10)))
        self.log(f"web sync: {steps:+d} x 10 s")
        for _ in range(abs(steps)):
            if not self.active:
                return
            self.tv.key("Right" if steps > 0 else "Left")
            time.sleep(0.12)
        self.settle_until = time.time() + 1.0

    def _catch_up(self, page_time):
        """A cast of a stream that can't be cut starts at 0:00: step forward to where the page already was."""
        t0 = time.time()
        end = t0 + 15
        while time.time() < end and self.active:
            st = self.tv.state()
            if st and st["state"] in ("play", "pause") and st.get("app") in ("RokuCast", ""):
                break
            time.sleep(0.4)
        now = self.guard.video_time(self.dur) or page_time
        tv_now = self._tv_time()
        if tv_now is not None and now - tv_now > self.SEEK_MIN + 8:
            self._step(now - tv_now + self.lead)

    def _align(self):
        """Snap the muted PC copy onto the TV's timeline if they've drifted apart."""
        self.last_check = time.time()
        if not self.guard.video_playing(self.dur):
            return
        tv, page = self._tv_time(), self.guard.video_time(self.dur)
        if tv is None or page is None or time.time() < self.settle_until:
            return
        if abs(tv - page) > self.DRIFT:
            self.guard.seek_page(tv + 0.3, self.dur)
            self.log(f"aligned the page to the TV (was {tv - page:+.2f}s off)")
