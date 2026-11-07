"""Decides when the page has moved on to the next episode, so the TV can follow without anyone pressing Cast.

A wrong guess takes over the TV, so this is cautious: the address must look like the same show, a new stream must have
arrived, a full-length video must be playing steadily, and there's a countdown you can cancel. Pure logic, no Tk.
"""
import re
import time
from urllib.parse import parse_qsl, urldefrag, urljoin, urlparse

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


def _numbers(url):
    """Every number in the path and query, in order."""
    p = urlparse(url)
    return [int(n) for n in re.findall(r"\d+", p.path)] + [int(n) for _, v in parse_qsl(p.query) for n in re.findall(r"\d+", v)]


def _is_next(current, other):
    """`other` is the same show with exactly one number one higher (the episode), and nothing else different."""
    a, b = _numbers(current), _numbers(other)
    if len(a) != len(b) or not same_show(current, other):
        return False
    changed = [(x, y) for x, y in zip(a, b) if x != y]
    return len(changed) == 1 and changed[0][1] == changed[0][0] + 1


EXPLICIT = re.compile(r"(?i)((?:episode|ep|e)(?:[-_/=]|%20)?)(\d+)")


def next_url(current, hrefs=()):
    """The address of the episode after the one at `current`, or None if it can't be told.

    The page's own links come first: one that is the same show with the episode number one higher. Failing that, an
    address that spells the episode out ('episode-5', 'ep=5', '?e=5') gets 1 added. Anything vaguer is left alone,
    because a guess that is wrong takes the TV somewhere unwanted."""
    current = urldefrag(current)[0]
    for h in hrefs or ():
        try:
            full = urldefrag(urljoin(current, str(h)))[0]
        except ValueError:
            continue
        if full != current and full.startswith(("http://", "https://")) and _is_next(current, full):
            return full
    p = urlparse(current)
    for part in ("path", "query"):
        text = getattr(p, part)
        matches = list(EXPLICIT.finditer(text))
        if matches:
            m = matches[-1]
            guess = text[:m.start(2)] + str(int(m.group(2)) + 1) + text[m.end(2):]
            out = urlparse(current)._replace(**{part: guess}).geturl()
            return out if same_show(current, out) else None
    return None
