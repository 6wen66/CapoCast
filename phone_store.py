"""Bookmarks and browsing history for the phone's Browse tab, kept on the PC in one small JSON file.

They live here, not in the phone, so they survive reinstalling the home-screen app and are the same on every phone.
Only http(s) addresses are kept. History is capped and can be cleared; a bookmark is never dropped by the cap.
"""
import json
import os
import threading
import time
from urllib.parse import urlparse

MAX_BOOKMARKS, MAX_HISTORY = 200, 300


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
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.bookmarks = [self._row(r) for r in data.get("bookmarks", []) if clean_url(r.get("url"))][:MAX_BOOKMARKS]
            self.history = [self._row(r) for r in data.get("history", []) if clean_url(r.get("url"))][:MAX_HISTORY]
        except (OSError, ValueError, AttributeError, TypeError):
            pass

    @staticmethod
    def _row(r):
        return {"url": clean_url(r.get("url")), "title": str(r.get("title") or "")[:200], "at": float(r.get("at") or 0)}

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"bookmarks": self.bookmarks, "history": self.history}, f)
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
