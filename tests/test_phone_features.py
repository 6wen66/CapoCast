"""Phone features: the now-playing card and scrub bar, bookmarks and history, and casting from the share sheet."""
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import phone_remote as pr  # noqa: E402
import phone_store  # noqa: E402
from test_web_tab_cast import Base  # noqa: E402


class ShareText(unittest.TestCase):
    def test_a_bare_link_passes_through(self):
        self.assertEqual(pr.shared_link("https://a.example/x?y=1"), "https://a.example/x?y=1")
        self.assertEqual(pr.shared_link("  https://a.example/  "), "https://a.example/")

    def test_a_link_inside_shared_text_is_found(self):
        self.assertEqual(pr.shared_link("Watch this!\nhttps://a.example/ep?x=1&y=2."), "https://a.example/ep?x=1&y=2")
        self.assertEqual(pr.shared_link("(see https://a.example/p)"), "https://a.example/p")

    def test_nothing_usable_is_refused(self):
        for bad in ("", "   ", "two words", "x" * 2100, "y " * 3000):
            with self.assertRaises(ValueError, msg=bad[:20]):
                pr.shared_link(bad)


class Backend:
    def __init__(self):
        self.calls = []
        self.sent = []

    def cast(self, url):
        self.calls.append(url)
        if "boom" in url:
            raise RuntimeError("The Roku refused the command.")
        return "Playing on the TV"

    def seek_to(self, t):
        self.calls.append(("seek_to", t))

    def web_pages(self):
        return {"bookmarks": [{"url": "https://a.example/", "title": "A", "at": 1}], "history": [], "current": {}}

    def web_bookmark(self, action, url):
        self.calls.append(("bookmark", action, url))
        return action == "toggle"


class Server(unittest.TestCase):
    def setUp(self):
        self.b = Backend()
        self.srv = pr.PhoneRemote(self.b, "secret-token", b"", port=0, bind="127.0.0.1")
        self.srv.start()
        self.base = f"http://127.0.0.1:{self.srv.port}"
        patcher = mock.patch("phone_remote.time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.srv.stop)

    def call(self, path, body=None, token="secret-token", host=None):
        headers = {"X-Token": token} if token else {}
        if host:
            headers["Host"] = host
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    # ---- scrubbing
    def test_seek_to_a_time(self):
        self.assertEqual(self.call("/api/seekto", {"t": 754.5})[0], 200)
        self.assertEqual(self.b.calls, [("seek_to", 754.5)])

    def test_seek_to_checks_the_time(self):
        for bad in ({"t": -1}, {"t": 10 ** 7}, {"t": "soon"}, {}):
            self.assertEqual(self.call("/api/seekto", bad)[0], 400, bad)
        self.assertEqual(self.b.calls, [])

    # ---- casting from the share sheet
    def test_cast_accepts_shared_text_with_a_link_in_it(self):
        code, body = self.call("/api/cast", {"url": "Look at this https://a.example/ep1 it's great"})
        self.assertEqual(code, 200)
        self.assertEqual(self.b.calls, ["https://a.example/ep1"])

    def test_cast_with_no_link_in_the_text_is_refused(self):
        self.assertEqual(self.call("/api/cast", {"url": "just some words"})[0], 400)
        self.assertEqual(self.b.calls, [])

    def test_the_send_link_casts_and_shows_a_confirmation_page(self):
        code, page = self.call("/send?k=secret-token&u=https%3A%2F%2Fa.example%2Fep1", token=None)
        self.assertEqual(code, 200)
        self.assertIn("Sent to the TV", page)
        self.assertEqual(self.b.calls, ["https://a.example/ep1"])
        self.assertIn("/?k=secret-token", page)                       # a way back to the remote

    def test_the_send_link_reports_failures_on_the_page(self):
        code, page = self.call("/send?k=secret-token&u=https%3A%2F%2Fboom.example%2F", token=None)
        self.assertEqual(code, 200)
        self.assertIn("Couldn&#x27;t send that", page)
        self.assertIn("The Roku refused the command.", page)

    def test_the_send_link_without_the_token_does_nothing(self):
        for q in ("/send?u=https%3A%2F%2Fa.example%2F", "/send?k=wrong&u=https%3A%2F%2Fa.example%2F"):
            code, page = self.call(q, token=None)
            self.assertEqual(code, 401, q)
            self.assertNotIn("secret-token", page)
        self.assertEqual(self.b.calls, [])

    def test_messages_in_the_page_cannot_inject_html(self):
        code, page = self.call("/send?k=secret-token&u=https%3A%2F%2Fboom.example%2F%3Cscript%3E", token=None)
        self.assertNotIn("<script>", page)

    def test_the_setup_page_shows_the_address_the_phone_used_and_the_token(self):
        code, page = self.call("/setup?k=secret-token", token=None, host="192.168.1.20:8765")
        self.assertEqual(code, 200)
        self.assertIn("http://192.168.1.20:8765/api/cast", page)
        self.assertIn("secret-token", page)
        self.assertIn("X-Token", page)
        self.assertEqual(self.call("/setup", token=None)[0], 401)
        self.assertNotIn("secret-token", self.call("/setup?k=nope", token=None)[1])

    # ---- bookmarks
    def test_pages_and_bookmark_routes(self):
        code, body = self.call("/api/web/pages")
        self.assertEqual((code, json.loads(body)["bookmarks"][0]["title"]), (200, "A"))
        self.assertEqual(json.loads(self.call("/api/web/bookmark", {"action": "toggle"})[1]), {"bookmarked": True})
        self.call("/api/web/bookmark", {"action": "remove", "url": "https://a.example/"})
        self.call("/api/web/bookmark", {"action": "clear_history"})
        self.assertEqual(self.b.calls, [("bookmark", "toggle", ""), ("bookmark", "remove", "https://a.example/"),
                                        ("bookmark", "clear_history", "")])
        self.assertEqual(self.call("/api/web/bookmark", {"action": "wipe"})[0], 400)
        self.assertEqual(self.call("/api/web/pages", token=None)[0], 401)


class TabPages(Base):
    """The Web tab feeding history and answering the Pages sheet."""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = phone_store.PhoneStore(os.path.join(self.dir.name, "p.json"))
        t = self.tab
        t.app.phone_store = self.store
        t._visited = ("", "")
        self.snap = {"running": True, "url": "https://site.example/ep1", "title": "", "streams": 0, "unsupported": ""}
        t.guard = SimpleNamespace(snapshot=lambda: dict(self.snap))

    def test_each_new_page_is_recorded_once_and_the_late_title_fills_in(self):
        t = self.tab
        for _ in range(3):
            t._record_visit(self.snap)
        self.snap["title"] = "Episode 1"
        t._record_visit(self.snap)
        t._record_visit(self.snap)
        self.snap.update(url="https://site.example/ep2", title="Episode 2")
        t._record_visit(self.snap)
        self.assertEqual([(r["url"], r["title"]) for r in self.store.recent()],
                         [("https://site.example/ep2", "Episode 2"), ("https://site.example/ep1", "Episode 1")])

    def test_blank_and_internal_pages_are_not_recorded(self):
        for url in ("", "about:blank", "chrome-error://chromewebdata/"):
            self.snap["url"] = url
            self.tab._record_visit(self.snap)
        self.assertEqual(self.store.recent(), [])

    def test_bookmark_the_current_page_and_see_it_in_the_sheet(self):
        t = self.tab
        self.snap["title"] = "Episode 1"
        self.assertTrue(t.phone_bookmark("toggle"))
        pages = t.phone_pages()
        self.assertEqual(pages["current"], {"url": "https://site.example/ep1", "title": "Episode 1", "bookmarked": True})
        self.assertEqual([b["title"] for b in pages["bookmarks"]], ["Episode 1"])
        self.assertTrue(t.phone_info()["bookmarked"])
        self.assertFalse(t.phone_bookmark("toggle"))
        self.assertFalse(t.phone_info()["bookmarked"])

    def test_remove_and_clear(self):
        t = self.tab
        self.store.toggle_bookmark("https://other.example/", "Other")
        self.store.visit("https://site.example/ep1", "E")
        t.phone_bookmark("remove", "https://other.example/")
        t.phone_bookmark("clear_history")
        self.assertEqual((self.store.all_bookmarks(), self.store.recent()), ([], []))
        with self.assertRaises(ValueError):
            t.phone_bookmark("bake")

    def test_bookmarking_a_blank_page_says_so(self):
        self.snap["url"] = "about:blank"
        with self.assertRaises(ValueError):
            self.tab.phone_bookmark("toggle")

    def test_without_a_store_the_sheet_is_just_empty(self):
        del self.tab.app.phone_store
        self.assertEqual(self.tab.phone_pages()["bookmarks"], [])
        with self.assertRaises(ValueError):
            self.tab.phone_bookmark("toggle")


class NowPlaying(unittest.TestCase):
    def setUp(self):
        import roku_listen_gui as g
        self.g = g
        self.calls = []
        self.watching = None
        web = SimpleNamespace(
            phone_watching=lambda: self.watching, sync=None,
            guard=SimpleNamespace(snapshot=lambda: {"title": "Page title"}),
            phone_transport=lambda action, value=0: self.calls.append(("page", action, value)))
        self.app = SimpleNamespace(web=web, _host="10.0.0.5", last_tv=None)
        self.b = g.PhoneBackend(self.app)

    def tv(self, **kw):
        return {"state": "play", "app": "Netflix", "pos": 100.0, "dur": 3000.0, **kw}

    def test_nothing_playing_means_no_card(self):
        self.assertIsNone(self.b.now(None))
        self.assertIsNone(self.b.now(self.tv(state="none")))

    def test_another_app_shows_its_name_and_cannot_be_scrubbed(self):
        n = self.b.now(self.tv())
        self.assertEqual((n["source"], n["title"], n["pos"], n["dur"], n["seekable"]), ("tv", "Netflix", 100.0, 3000.0, False))

    def test_a_web_cast_is_in_the_videos_own_time(self):
        self.watching = {"id": 1.0, "title": "Ghost in the Shell", "url": "u"}
        self.app.web.sync = SimpleNamespace(offset=600.0, dur=5400.0, live=False)
        n = self.b.now(self.tv(app="RokuCast", pos=30.0, dur=4800.0))
        self.assertEqual((n["source"], n["title"], n["pos"], n["dur"], n["seekable"]),
                         ("web", "Ghost in the Shell", 630.0, 5400.0, True))

    def test_a_web_cast_without_a_known_length_or_live_cannot_be_scrubbed(self):
        self.watching = {"id": 1.0, "title": "", "url": "u"}
        self.app.web.sync = SimpleNamespace(offset=0.0, dur=0.0, live=True)
        n = self.b.now(self.tv(app="RokuCast", dur=0.0))
        self.assertEqual((n["title"], n["seekable"]), ("Page title", False))

    def test_youtube_uses_the_title_from_playlet_and_can_be_scrubbed(self):
        with mock.patch.object(self.g.Remote, "playlet_now", return_value={"videoid": "abc", "title": "A great video"}):
            n = self.b.now(self.tv(app="Playlet", dur=600.0))
        self.assertEqual((n["source"], n["app"], n["title"], n["seekable"]), ("youtube", "YouTube", "A great video", True))

    def test_youtube_falls_back_to_looking_the_title_up_once(self):
        with mock.patch.object(self.g.Remote, "playlet_now", return_value={"videoid": "abc"}), \
                mock.patch.object(self.g.discord_presence, "youtube_info", return_value=("Chan", "Looked up")) as info:
            first = self.b.now(self.tv(app="Playlet"))
            self.b._yt_cache = None                                     # past the short cache
            second = self.b.now(self.tv(app="Playlet"))
        self.assertEqual((first["title"], second["title"]), ("Looked up", "Looked up"))
        info.assert_called_once_with("abc")

    def test_scrubbing_a_web_cast_moves_the_page_by_the_difference(self):
        self.watching = {"id": 1.0, "title": "T", "url": "u"}
        self.app.web.sync = SimpleNamespace(offset=600.0, dur=5400.0, live=False)
        self.app.last_tv = self.tv(app="RokuCast", pos=30.0)           # the video is at 630 s
        self.b.seek_to(1000.0)
        self.assertEqual(self.calls, [("page", "skip", 370.0)])

    def test_scrubbing_youtube_asks_playlet_for_that_time(self):
        with mock.patch.object(self.g.Remote, "seek_to") as seek:
            self.b.seek_to(754.0)
        seek.assert_called_once_with("10.0.0.5", 754.0)


class PlayletSeek(unittest.TestCase):
    def setUp(self):
        import roku_listen_gui as g
        self.g = g

    def test_jump_posts_the_time_to_playlet(self):
        st = {"state": "play", "app": "Playlet", "pos": 10.0, "dur": 600.0}
        with mock.patch.object(self.g.Remote, "state", return_value=st), \
                mock.patch.object(self.g.Remote, "playlet_now", return_value={"videoid": "vid"}), \
                mock.patch.object(self.g.Remote, "_open") as op:
            self.g.Remote.seek_to("10.0.0.5", 321.7)
        url, = op.call_args[0]
        self.assertTrue(url.endswith(":8888/api/queue/play"))
        self.assertEqual(json.loads(op.call_args[1]["data"]), {"videoId": "vid", "timestamp": 321})

    def test_jump_stays_inside_the_video(self):
        st = {"state": "play", "app": "Playlet", "pos": 10.0, "dur": 600.0}
        with mock.patch.object(self.g.Remote, "state", return_value=st), \
                mock.patch.object(self.g.Remote, "playlet_now", return_value={"videoid": "vid"}), \
                mock.patch.object(self.g.Remote, "_open") as op:
            self.g.Remote.seek_to("h", 99999)
        self.assertEqual(json.loads(op.call_args[1]["data"])["timestamp"], 598)

    def test_other_apps_cannot_jump_to_a_time(self):
        with mock.patch.object(self.g.Remote, "state", return_value={"state": "play", "app": "Netflix", "pos": 1.0, "dur": 9.0}):
            with self.assertRaises(ValueError):
                self.g.Remote.seek_to("h", 5)

    def test_relative_seek_still_uses_arrow_keys_outside_playlet(self):
        with mock.patch.object(self.g.Remote, "state", return_value={"state": "play", "app": "Netflix", "pos": 1.0, "dur": 9.0}), \
                mock.patch.object(self.g.Remote, "key") as key:
            self.g.Remote.seek("h", -10)
            self.g.Remote.seek("h", 10)
        self.assertEqual([c[0][1] for c in key.call_args_list], ["Left", "Right"])


if __name__ == "__main__":
    unittest.main()
