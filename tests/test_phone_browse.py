"""Browsing from the phone: the Browse tab's routes, the guard's phone-sized view, and the Web tab's phone methods."""
import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import phone_remote as pr  # noqa: E402
from test_web_tab_cast import Base  # noqa: E402
from web_guard import WebGuard  # noqa: E402


class FakeWeb:
    """The backend as the Browse tab sees it; records what it was asked."""

    def __init__(self):
        self.log = []

    def web_frame(self, after, w, h):
        self.log.append(("frame", after, w, h))
        return None if after == 9 else (after + 1, b"\xff\xd8jpeg", (w, h))

    def web_info(self):
        return {"alive": True, "url": "https://x.example/"}

    def web_tap(self, x, y):
        self.log.append(("tap", x, y))

    def web_scroll(self, x, y, dx, dy):
        self.log.append(("scroll", x, y, dx, dy))

    def web_text(self, text):
        self.log.append(("text", text))

    def web_key(self, name):
        self.log.append(("key", name))

    def web_nav(self, action, url):
        self.log.append(("nav", action, url))

    def web_cast(self):
        return "Looking for the video"

    def web_transport(self, action, value):
        self.log.append(("transport", action, value))

    def web_youtube(self, on, go):
        self.log.append(("youtube", on, go))


class Routes(unittest.TestCase):
    def setUp(self):
        self.web = FakeWeb()
        self.srv = pr.PhoneRemote(self.web, "secret-token", b"", port=0, bind="127.0.0.1")
        self.srv.start()
        self.base = f"http://127.0.0.1:{self.srv.port}"
        patcher = mock.patch("phone_remote.time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.srv.stop)

    def call(self, path, body=None, token="secret-token"):
        headers = {"X-Token": token} if token else {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, {}, e.read()

    def test_a_frame_comes_with_its_sequence_and_size(self):
        code, headers, body = self.call("/api/web/frame?after=3&w=390&h=700")
        self.assertEqual((code, body), (200, b"\xff\xd8jpeg"))
        self.assertEqual((headers["X-Seq"], headers["X-View-W"], headers["X-View-H"]), ("4", "390", "700"))
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        self.assertEqual(self.web.log, [("frame", 3, 390, 700)])

    def test_no_new_frame_is_204(self):
        self.assertEqual(self.call("/api/web/frame?after=9&w=390&h=700")[0], 204)

    def test_frame_sizes_are_checked(self):
        for q in ("w=10&h=700", "w=390&h=99999", "w=abc&h=700", "after=-1"):
            self.assertEqual(self.call("/api/web/frame?" + q)[0], 400, q)

    def test_taps_scrolls_text_and_keys(self):
        self.call("/api/web/input", {"type": "tap", "x": 100.5, "y": 200})
        self.call("/api/web/input", {"type": "scroll", "x": 50, "y": 60, "dx": 0, "dy": 33})
        self.call("/api/web/input", {"type": "text", "text": "hello"})
        self.call("/api/web/input", {"type": "key", "name": "Enter"})
        self.assertEqual(self.web.log, [("tap", 100.5, 200.0), ("scroll", 50.0, 60.0, 0.0, 33.0), ("text", "hello"),
                                        ("key", "Enter")])

    def test_live_typing_deletes_then_types_in_one_request(self):
        self.call("/api/web/input", {"type": "edit", "back": 2, "text": "ab"})
        self.call("/api/web/input", {"type": "edit", "back": 0, "text": "c"})
        self.call("/api/web/input", {"type": "edit", "back": 1, "text": ""})
        self.assertEqual(self.web.log, [("key", "Backspace"), ("key", "Backspace"), ("text", "ab"), ("text", "c"),
                                        ("key", "Backspace")])

    def test_empty_or_silly_edits_are_refused(self):
        for body in ({"type": "edit", "back": 0, "text": ""}, {"type": "edit", "back": -1, "text": "a"},
                     {"type": "edit", "back": 9999, "text": "a"}, {"type": "edit", "back": "x", "text": "a"}):
            self.assertEqual(self.call("/api/web/input", body)[0], 400, body)
        self.assertEqual(self.web.log, [])

    def test_bad_input_is_refused(self):
        for body in ({"type": "tap", "x": "a", "y": 1}, {"type": "tap", "x": 99999, "y": 1}, {"type": "key", "name": "F12"},
                     {"type": "text", "text": ""}, {"type": "scroll", "x": 1, "y": 1, "dx": 0, "dy": 10 ** 6},
                     {"type": "zap"}):
            self.assertEqual(self.call("/api/web/input", body)[0], 400, body)
        self.assertEqual(self.web.log, [])

    def test_navigation_and_cast(self):
        self.call("/api/web/nav", {"action": "back"})
        self.call("/api/web/nav", {"action": "go", "url": "cats"})
        self.assertEqual(self.call("/api/web/nav", {"action": "go", "url": ""})[0], 400)
        self.assertEqual(self.call("/api/web/nav", {"action": "format"})[0], 400)
        self.assertEqual(json.loads(self.call("/api/web/cast", {})[2]), {"message": "Looking for the video"})
        self.assertEqual(self.web.log, [("nav", "back", ""), ("nav", "go", "cats")])

    def test_the_youtube_switch(self):
        self.call("/api/web/youtube", {"on": True, "go": "https://www.youtube.com/"})
        self.call("/api/web/youtube", {"on": True})
        self.call("/api/web/youtube", {"on": False})
        self.assertEqual(self.web.log, [("youtube", True, "https://www.youtube.com/"), ("youtube", True, ""),
                                        ("youtube", False, "")])
        self.assertEqual(self.call("/api/web/youtube", {"on": True}, token=None)[0], 401)

    def test_play_pause_and_skip(self):
        self.call("/api/web/transport", {"action": "toggle", "value": 0})
        self.call("/api/web/transport", {"action": "skip", "value": -10})
        self.assertEqual(self.call("/api/web/transport", {"action": "skip", "value": 99999})[0], 400)
        self.assertEqual(self.call("/api/web/transport", {"action": "explode"})[0], 400)
        self.assertEqual(self.web.log, [("transport", "toggle", 0.0), ("transport", "skip", -10.0)])

    def test_it_all_needs_the_token(self):
        for path in ("/api/web/frame", "/api/web/info", "/api/web/cast"):
            self.assertEqual(self.call(path, token=None)[0], 401, path)
        self.assertEqual(self.web.log, [])

    def test_a_message_from_the_page_side_comes_back_as_text(self):
        def refuse(action, url):
            raise ValueError("netflix.com protects its video with DRM.")
        self.web.web_nav = refuse
        code, _, body = self.call("/api/web/nav", {"action": "go", "url": "netflix.com"})
        self.assertEqual((code, json.loads(body)["error"]), (400, "netflix.com protects its video with DRM."))


class PhoneView(unittest.TestCase):
    """The browser renders at the phone's size while a phone is watching, and goes back afterwards."""

    def setUp(self):
        self.g = WebGuard()
        self.sent = []
        self.g._main = "S"
        self.g._send = lambda method, params=None, session=None, cb=None: self.sent.append((method, params))
        self.g._view = (1280, 700, False)                 # the app's tab is hidden: no streaming

    def test_the_phone_gets_its_own_size_streams_and_hands_back(self):
        g = self.g
        g.phone_watch(390, 700)
        g._apply_view()
        metrics = dict(self.sent)["Emulation.setDeviceMetricsOverride"]
        self.assertEqual((metrics["width"], metrics["height"], metrics["deviceScaleFactor"]), (390, 700, 1))
        cast = dict(self.sent)["Page.startScreencast"]
        self.assertEqual((cast["maxWidth"], cast["maxHeight"]), (390, 700))
        self.assertGreater(cast["quality"], 72)           # shown enlarged on the phone, so a better picture than the PC's
        self.assertEqual(g.view_size(), (390, 700))
        self.sent.clear()
        g._phone = (390, 700, time.time() - 30)           # the phone went quiet
        g.phone_expire()
        self.assertFalse(g.phone_active)
        g._apply_view()
        self.assertEqual(dict(self.sent)["Emulation.setDeviceMetricsOverride"]["width"], 1280)
        self.assertNotIn("Page.startScreencast", dict(self.sent))     # the hidden tab streams nothing again
        self.assertEqual(g.view_size(), (1280, 700))

    def test_sizes_are_clamped_and_a_resize_re_applies(self):
        g = self.g
        g.phone_watch(5000, 5000)
        self.assertEqual(g._phone[:2], (900, 1600))
        while not g._cmds.empty():
            g._cmds.get()
        g.phone_watch(900, 1600)
        self.assertTrue(g._cmds.empty())                  # same size: nothing to redo
        g.phone_watch(390, 700)
        self.assertFalse(g._cmds.empty())                 # rotated: render again

    def test_a_short_quiet_spell_does_not_let_go(self):
        self.g.phone_watch(390, 700)
        self.g.phone_expire()
        self.assertTrue(self.g.phone_active)

    def test_works_even_before_the_app_has_set_a_size(self):
        self.g._view = None
        self.g.phone_watch(390, 700)
        self.g._apply_view()
        self.assertIn("Page.startScreencast", dict(self.sent))


class PhoneTab(Base):
    def setUp(self):
        super().setUp()
        t = self.tab
        self.mouse, self.keys_sent, self.navs, self.posted = [], [], [], []
        t.home, t.link, t.launched_at = "https://duckduckgo.com", None, 0.0
        t._post = self.posted.append
        t.guard = SimpleNamespace(
            running=True, mouse=lambda *a, **k: self.mouse.append((a, k)), key=self.keys_sent.append,
            text=lambda x: self.keys_sent.append(("text", x)), navigate=self.navs.append,
            back=lambda: self.navs.append("back"), forward=lambda: self.navs.append("forward"),
            reload=lambda: self.navs.append("reload"), phone_watch=lambda w, h: self.navs.append(("watch", w, h)),
            frame=lambda: (5, b"jpeg"), view_size=lambda: (390, 700),
            snapshot=lambda: {"running": True, "url": "https://site.example/ep", "title": "Ep", "streams": 2,
                              "unsupported": ""})

    def test_a_tap_is_move_press_release_at_the_same_spot(self):
        self.tab.phone_tap(10, 20)
        self.assertEqual([a[0] for a, k in self.mouse], ["mouseMoved", "mousePressed", "mouseReleased"])
        self.assertTrue(all(a[1:3] == (10, 20) for a, k in self.mouse))

    def test_scroll_goes_to_the_wheel(self):
        self.tab.phone_scroll(5, 6, 0, 40)
        (a, k), = self.mouse
        self.assertEqual((a[0], k["dy"], k["dx"]), ("mouseWheel", 40, 0))

    def test_enter_and_backspace_are_real_key_events(self):
        self.tab.phone_key("Enter")
        self.tab.phone_key("Backspace")
        self.assertEqual([e["key"] for e in self.keys_sent], ["Enter", "Enter", "Backspace", "Backspace"])
        with self.assertRaises(ValueError):
            self.tab.phone_key("F5")

    def test_address_box_rules_match_the_pc(self):
        t = self.tab
        t.phone_nav("go", "example.com/x")
        t.phone_nav("go", "funny cats")
        t.phone_nav("home")
        t.phone_nav("back")
        self.assertEqual(self.navs, ["https://example.com/x", "https://duckduckgo.com/?q=funny+cats",
                                     "https://duckduckgo.com", "back"])
        with self.assertRaises(ValueError):
            t.phone_nav("go", "https://www.netflix.com/title/1")
        with self.assertRaises(ValueError):
            t.phone_nav("explode")

    def test_frames_come_at_the_phones_size(self):
        self.assertEqual(self.tab.phone_frame(4, 390, 700, timeout=1), (5, b"jpeg", (390, 700)))
        self.assertIn(("watch", 390, 700), self.navs)

    def test_a_frame_from_before_the_switch_to_the_phones_size_is_skipped(self):
        t = self.tab
        t.guard.frame_meta = {"deviceWidth": 390, "deviceHeight": 213}          # drawn at the old shape, scaled down
        self.assertIsNone(t.phone_frame(4, 390, 700, timeout=0.15))
        t.guard.frame_meta = {"deviceWidth": 390, "deviceHeight": 700}
        self.assertEqual(t.phone_frame(4, 390, 700, timeout=1)[0], 5)
        t.guard.frame_meta = {}                                                # no metadata: take the frame
        self.assertEqual(t.phone_frame(4, 390, 700, timeout=1)[0], 5)

    def test_a_frame_the_phone_already_has_is_not_resent(self):
        self.assertIsNone(self.tab.phone_frame(5, 390, 700, timeout=0.15))

    def test_cast_page_refuses_drm_and_toggles_cancel(self):
        t = self.tab
        t.cast = lambda: self.posted.append("cast")
        self.assertEqual(t.phone_cast(), "Looking for the video…")
        t.busy = True
        self.assertEqual(t.phone_cast(), "Cancelled")
        t.guard.snapshot = lambda: {"running": True, "url": "https://www.disneyplus.com/x", "title": "", "streams": 0,
                                    "unsupported": ""}
        with self.assertRaises(ValueError):
            t.phone_cast()
        t.guard.snapshot = lambda: {"running": False, "url": "", "title": "", "streams": 0, "unsupported": ""}
        with self.assertRaises(ValueError):
            t.phone_cast()

    def test_transport_acts_on_the_pages_video_at_the_casts_length(self):
        t = self.tab
        calls = []
        t.guard.toggle_page = lambda dur: calls.append(("toggle", dur))
        t.guard.skip_page = lambda dt, dur: calls.append(("skip", dt, dur))
        t.sync = None
        t.phone_transport("toggle")
        t.sync = SimpleNamespace(active=True, dur=5400.0, title="Ghost in the Shell", started_at=1234.5)
        t.phone_transport("skip", -10)
        self.assertEqual(calls, [("toggle", 0), ("skip", -10, 5400.0)])
        with self.assertRaises(ValueError):
            t.phone_transport("rewind")

    def test_watching_names_the_cast_and_changes_id_with_each_new_one(self):
        t = self.tab
        t.cast_page = "https://site.example/ghost-in-the-shell"
        t.sync = None
        self.assertIsNone(t.phone_watching())
        t.sync = SimpleNamespace(active=True, dur=5400.0, title="Ghost in the Shell", started_at=1234.5)
        first = t.phone_watching()
        self.assertEqual((first["title"], first["url"]), ("Ghost in the Shell", "https://site.example/ghost-in-the-shell"))
        t.sync = SimpleNamespace(active=True, dur=1400.0, title="Next episode", started_at=1300.1)
        self.assertNotEqual(t.phone_watching()["id"], first["id"])
        t.sync = SimpleNamespace(active=False, dur=0, title="", started_at=1300.1)
        self.assertIsNone(t.phone_watching())

    def test_info_reports_what_the_phone_needs(self):
        t = self.tab
        t.sync, t.link = None, None
        info = t.phone_info()
        self.assertEqual((info["url"], info["streams"], info["busy"], info["ready"], info["note"]),
                         ("https://site.example/ep", 2, False, True, None))


if __name__ == "__main__":
    unittest.main()
