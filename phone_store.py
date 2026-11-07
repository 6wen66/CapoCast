"""Bookmarks and browsing history for the phone's Browse tab, kept on the PC in one small JSON file.

They live here, not in the phone, so they survive reinstalling the home-screen app and are the same on every phone.
Only http(s) addresses are kept. History is capped and can be cleared; a bookmark is never dropped by the cap.
"""
import json
import os
import threading
import time
from urllib.parse import urlparse

from stream_finder import same_show

MAX_BOOKMARKS, MAX_HISTORY, MAX_WATCHING = 200, 300, 50
FINISHED_WITHIN = 30.0      # seconds from the end that count as having watched it all


def clean_url(url):
    """The address as stored, or '' if it isn't a real web page (blank tab, data: URL, internal page...)."""
    url = (url or "").strip()
    try:
        p = urlparse(url)
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.hostname or len(url) > 2000:
        return ""
    return url


class PhoneStore:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self.bookmarks, self.history = [], []            # [{url, title, at}], newest first
        self.watching = []                               # Continue Watching: one row per show, newest first
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.bookmarks = [self._row(r) for r in data.get("bookmarks", []) if clean_url(r.get("url"))][:MAX_BOOKMARKS]
            self.history = [self._row(r) for r in data.get("history", []) if clean_url(r.get("url"))][:MAX_HISTORY]
            self.watching = [w for w in (self._watch_row(r) for r in data.get("watching", [])
                                         if isinstance(r, dict)) if w][:MAX_WATCHING]
        except (OSError, ValueError, AttributeError, TypeError):
            pass

    @staticmethod
    def _row(r):
        return {"url": clean_url(r.get("url")), "title": str(r.get("title") or "")[:200], "at": float(r.get("at") or 0)}

    @staticmethod
    def _watch_row(r):
        """A stored Continue Watching row, or None if it isn't usable."""
        url = clean_url(r.get("url"))
        try:
            pos, dur, at = float(r.get("pos") or 0), float(r.get("dur") or 0), float(r.get("at") or 0)
        except (TypeError, ValueError):
            return None
        if not url or dur <= 0 or pos < 0:
            return None
        return {"url": url, "title": str(r.get("title") or "")[:200], "site": str(r.get("site") or "")[:80],
                "image": clean_url(r.get("image")), "pos": min(pos, dur), "dur": dur, "at": at,
                "finished": bool(r.get("finished")), "next_url": clean_url(r.get("next_url"))}

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"bookmarks": self.bookmarks, "history": self.history, "watching": self.watching}, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # ---- history ------------------------------------------------------------------------------------------------
    def visit(self, url, title=""):
        """The Web tab is on this page. The same page again only refreshes its title (titles arrive after the address)."""
        url = clean_url(url)
        if not url:
            return
        title = str(title or "")[:200]
        with self._lock:
            top = self.history[0] if self.history else None
            if top and top["url"] == url:
                if title and top["title"] != title:
                    top["title"] = title
                    self._save()
                return
            self.history = [r for r in self.history if r["url"] != url]       # one row per page: moved to the top
            self.history.insert(0, {"url": url, "title": title, "at": time.time()})
            del self.history[MAX_HISTORY:]
            self._save()

    def recent(self, limit=60):
        with self._lock:
            return [dict(r) for r in self.history[:limit]]

    def clear_history(self):
        with self._lock:
            self.history = []
            self._save()

    # ---- bookmarks ----------------------------------------------------------------------------------------------
    def is_bookmarked(self, url):
        url = clean_url(url)
        with self._lock:
            return any(b["url"] == url for b in self.bookmarks)

    def toggle_bookmark(self, url, title=""):
        """Add the page, or remove it if it's already there. Returns True if it is bookmarked afterwards."""
        url = clean_url(url)
        if not url:
            raise ValueError("Open a page first.")
        with self._lock:
            if any(b["url"] == url for b in self.bookmarks):
                self.bookmarks = [b for b in self.bookmarks if b["url"] != url]
                self._save()
                return False
            if len(self.bookmarks) >= MAX_BOOKMARKS:
                raise ValueError("That's a lot of bookmarks. Remove some first.")
            self.bookmarks.insert(0, {"url": url, "title": str(title or "")[:200], "at": time.time()})
            self._save()
            return True

    def remove_bookmark(self, url):
        url = clean_url(url)
        with self._lock:
            self.bookmarks = [b for b in self.bookmarks if b["url"] != url]
            self._save()

    def all_bookmarks(self):
        with self._lock:
            return [dict(b) for b in self.bookmarks]

    # ---- continue watching --------------------------------------------------------------------------------------
    def save_progress(self, url, pos, dur, title="", site="", image="", next_url="", finished=False):
        """Where the TV is in the show cast from `url`. The same show (this page, or a neighbouring episode of it) keeps
        one row, so moving on to the next episode replaces the last one. Anything within FINISHED_WITHIN of the end, or
        `finished`, is marked finished. Returns the stored row, or None if it wasn't worth keeping."""
        url = clean_url(url)
        try:
            pos, dur = float(pos), float(dur)
        except (TypeError, ValueError):
            return None
        if not url or dur <= 0 or pos < 0:
            return None
        done = bool(finished) or pos >= dur - FINISHED_WITHIN
        with self._lock:
            old = next((w for w in self.watching if w["url"] == url), None) or                 next((w for w in self.watching if same_show(w["url"], url)), None)
            same_page = bool(old) and old["url"] == url        # a neighbouring episode keeps nothing of the last one's
            keep = old if same_page else {}
            row = {"url": url, "title": str(title or keep.get("title") or "")[:200],
                   "site": str(site or keep.get("site") or old and old["site"] or "")[:80],
                   "image": clean_url(image) or keep.get("image", ""),
                   "pos": min(pos, dur), "dur": dur, "at": time.time(), "finished": done,
                   "next_url": clean_url(next_url) or keep.get("next_url", "")}
            self.watching = [row] + [w for w in self.watching if w is not old]
            del self.watching[MAX_WATCHING:]
            self._save()
            return dict(row)

    def continue_list(self, limit=30):
        """Rows to offer, newest first: a started show to resume, or a finished one with a known next episode."""
        with self._lock:
            return [dict(w) for w in self.watching if (not w["finished"] and w["pos"] > 10) or
                    (w["finished"] and w["next_url"])][:limit]

    def progress_for(self, url):
        """The saved row for exactly this page (not a neighbouring episode), or None."""
        url = clean_url(url)
        with self._lock:
            return next((dict(w) for w in self.watching if w["url"] == url), None)

    def remove_watching(self, url):
        url = clean_url(url)
        with self._lock:
            self.watching = [w for w in self.watching if w["url"] != url]
            self._save()

    def clear_watching(self):
        with self._lock:
            self.watching = []
            self._save()
