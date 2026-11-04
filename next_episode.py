"""Decides when the page has moved on to the next episode, so the TV can follow without anyone pressing Cast.

A wrong guess takes over the TV, so this is cautious: the address must look like the same show, a new stream must have
arrived, a full-length video must be playing steadily, and there's a countdown you can cancel. Pure logic, no Tk.
"""
import time
from urllib.parse import urldefrag

from stream_finder import MIN_SECONDS, same_show


class NextEpisode:
    WAIT = 3.0          # a new video must play this long first: players stall and swap sources as they start
    COUNTDOWN = 5.0     # the "Up next" warning, with a Cancel
    GRACE = 600.0       # once the TV has stopped, how long the page may take to reach the next episode

    def __init__(self, clock=time.time):
        self.clock = clock
        self.disarm()

    @property
    def counting(self):
        return self.fires_at is not None

    def arm(self, page_url):
        """Watch for the episode after the one cast from `page_url`."""
        self.disarm()
        self.url = urldefrag(page_url)[0]

    def disarm(self):
        self.url = self.skipped = self.expires = None
        self._reset()

    def _reset(self):
        self.playing_since = self.fires_at = None

    def grace(self):
        """The TV isn't following any more, but it wasn't you stopping it: keep watching for a while."""
        if self.url:
            self.expires = self.clock() + self.GRACE

    def skip(self, page_url):
        """Cancel pressed: leave this episode alone, but still follow the one after it."""
        self.skipped = urldefrag(page_url)[0]
        self._reset()

    def tick(self, page_url, videos, fresh_stream):
        """Seconds until the TV should switch, 0 for now, or None if there's nothing to do."""
        now = self.clock()
        if not self.url:
            return None
        if self.expires and now > self.expires:
            self.disarm()
            return None
        key = urldefrag(page_url)[0]
        playing = any(v.get("playing") and (v.get("dur") or 0) >= MIN_SECONDS for v in videos)
        if key in (self.url, self.skipped) or not fresh_stream or not playing or not same_show(self.url, key):
            self._reset()
            return None
        if self.playing_since is None:
            self.playing_since = now
        if now - self.playing_since < self.WAIT:
            return None
        if self.fires_at is None:
            self.fires_at = now + self.COUNTDOWN
        return max(0.0, self.fires_at - now)
