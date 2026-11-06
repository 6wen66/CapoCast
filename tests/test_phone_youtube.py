"""The phone's YouTube tab: the real youtube.com, drawn black, where tapping a video plays it on the TV."""
import json
import os
import sys
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import web_guard  # noqa: E402
from test_web_tab_cast import Base  # noqa: E402
from web_guard import WebGuard, dark_pref  # noqa: E402


class DarkTheme(unittest.TestCase):
    def test_a_fresh_browser_gets_just_the_theme(self):
        self.assertEqual(dark_pref([]), "f6=400")

    def test_other_preferences_are_kept(self):
        got = dark_pref([{"name": "PREF", "value": "tz=America.New_York&f4=4000000"}])
        self.assertEqual(sorted(got.split("&")), ["f4=4000000", "f6=400", "tz=America.New_York"])

    def test_existing_flags_are_kept_and_light_is_switched_off(self):
        self.assertEqual(dark_pref([{"name": "PREF", "value": "f6=80008"}]), "f6=408")
        self.assertEqual(dark_pref([{"name": "PREF", "value": "f6=8&hl=en"}]), "hl=en&f6=408")

    def test_a_garbled_flag_does_not_break_it(self):
        self.assertEqual(dark_pref([{"name": "PREF", "value": "f6=zz"}]), "f6=400")

    def test_other_cookies_are_ignored(self):
        self.assertEqual(dark_pref([{"name": "SID", "value": "f6=1"}]), "f6=400")


class Recorder:
    """Stands in for the browser connection: records what the guard sends, and answers getCookies."""

    def __init__(self, cookies=()):
        self.sent, self.cookies = [], list(cookies)

    def __call__(self, method, params=None, session=None, cb=None):
        self.sent.append((method, params or {}))
        if method == "Network.getCookies" and cb:
            cb({"cookies": self.cookies})


class TheSwitch(unittest.TestCase):
    def guard(self, cookies=(), url=""):
        g = WebGuard(lambda m: None)
        g._send = Recorder(cookies)
        g._main, g._url = "S", url
        return g

    def run_cmds(self, g):
        g._drain()
        return g._send.sent

    def cookie(self, sent, name):
        return next(p for m, p in sent if m == "Network.setCookie" and p["name"] == name)

    def test_turning_it_on_sets_the_theme_then_opens_youtube(self):
        g = self.guard()
        g.youtube_mode(True, "https://www.youtube.com/")
        sent = self.run_cmds(g)
        self.assertEqual([m for m, _ in sent], ["Network.getCookies", "Network.setCookie", "Network.setCookie", "Page.navigate"])
        self.assertEqual(self.cookie(sent, "PREF")["value"], "f6=400")
        self.assertEqual(sent[-1][1]["url"], "https://www.youtube.com/")
        self.assertGreater(self.cookie(sent, "rl_yt")["expires"], time.time() + 5)

    def test_the_switch_lapses_by_itself_and_can_be_cleared(self):
        g = self.guard()
        g.youtube_mode(True)
        lease = self.cookie(self.run_cmds(g), "rl_yt")["expires"]
        self.assertLess(lease, time.time() + WebGuard.YT_LEASE + 1)
        g = self.guard()
        g._yt_dark = True
        g.youtube_mode(False)
        off = self.cookie(self.run_cmds(g), "rl_yt")
        self.assertLess(off["expires"], 100)                         # already in the past: the browser drops it

    def test_renewing_does_not_touch_the_theme_or_the_page(self):
        g = self.guard()
        g.youtube_mode(True)
        self.run_cmds(g)
        g._send.sent.clear()
        g.youtube_mode(True)
        sent = self.run_cmds(g)
        self.assertEqual([m for m, _ in sent], ["Network.setCookie"])

    def test_a_page_already_open_on_youtube_is_redrawn_in_the_new_theme(self):
        g = self.guard(url="https://www.youtube.com/feed/subscriptions")
        g.youtube_mode(True)
        self.assertIn("Page.reload", [m for m, _ in self.run_cmds(g)])
        g = self.guard(url="https://example.com/")
        g.youtube_mode(True)
        self.assertNotIn("Page.reload", [m for m, _ in self.run_cmds(g)])

    def test_nothing_happens_while_there_is_no_page_to_talk_to(self):
        g = self.guard()
        g._main = None
        g.youtube_mode(True, "https://www.youtube.com/")
        self.assertEqual(self.run_cmds(g), [])


class TappedVideos(unittest.TestCase):
    def binding(self, g, url):
        g._on_Runtime_bindingCalled({"name": "rlYoutube", "payload": url}, "S")

    def test_a_tapped_video_is_handed_on(self):
        g, got = WebGuard(lambda m: None), []
        g.on_youtube = got.append
        self.binding(g, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(got, ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"])

    def test_only_web_addresses_are_handed_on(self):
        g, got = WebGuard(lambda m: None), []
        g.on_youtube = got.append
        for bad in ("javascript:alert(1)", "http://youtube.com/watch?v=x", "file:///c:/x", ""):
            self.binding(g, bad)
        self.assertEqual(got, [])

    def test_no_listener_is_fine(self):
        self.binding(WebGuard(lambda m: None), "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_the_page_script_has_the_interceptor_and_the_black_css(self):
        self.assertIn("rlYoutube", web_guard.PAGE_JS)
        self.assertIn("rl_yt=1", web_guard.PAGE_JS)
        self.assertNotIn("__YT_BLACK__", web_guard.PAGE_JS)
        self.assertIn("--yt-spec-base-background", web_guard.PAGE_JS)


class TheTab(Base):
    def setUp(self):
        super().setUp()
        self.sent, self.opened = [], []
        self.tab.guard = SimpleNamespace(youtube_mode=lambda on, go=None: self.sent.append((on, go)))
        self.tab.phone_open = lambda: self.opened.append(1)
        self.tab.app.phone = SimpleNamespace(backend=SimpleNamespace(cast=self.cast))
        self.cast_result = "Playing on the TV"

    def cast(self, url):
        self.cast_url = url
        if isinstance(self.cast_result, Exception):
            raise self.cast_result
        return self.cast_result

    def test_it_only_opens_youtube(self):
        self.tab.phone_youtube(True, "https://www.youtube.com/")
        self.tab.phone_youtube(True, "https://m.youtube.com/feed/subscriptions")
        self.tab.phone_youtube(False)
        self.assertEqual(self.sent, [(True, "https://www.youtube.com/"), (True, "https://m.youtube.com/feed/subscriptions"),
                                     (False, None)])
        for bad in ("https://youtube.com.evil.com/", "http://www.youtube.com/", "https://example.com/", "javascript:1"):
            with self.assertRaises(ValueError, msg=bad):
                self.tab.phone_youtube(True, bad)
        self.assertEqual(len(self.sent), 3)

    def run_tap(self, url):
        import threading
        before = set(threading.enumerate())
        self.tab._youtube_tapped(url)
        for t in set(threading.enumerate()) - before:
            t.join(5)
        self.run_ui()

    def test_a_tapped_video_plays_on_the_tv_and_the_phone_is_told(self):
        self.run_tap("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(self.cast_url, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(self.tab.phone_note(), None if time.time() >= self.tab.note_until else ("Playing on the TV", True))
        self.assertEqual(self.tab.note[0], "Playing on the TV")

    def test_a_tv_that_does_not_answer_is_reported(self):
        self.cast_result = OSError("The TV didn't answer.")
        self.run_tap("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertIn("didn't answer", self.tab.note[0])
        self.assertEqual(self.tab.note[1], "a")


if __name__ == "__main__":
    unittest.main()
