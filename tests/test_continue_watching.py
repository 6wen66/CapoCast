"""Continue Watching: what's stored, how the next episode is found, and the Web tab's recording and resuming."""
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import phone_remote as pr  # noqa: E402
import web_tab  # noqa: E402
from next_episode import next_url  # noqa: E402
from phone_store import PhoneStore  # noqa: E402
from test_web_tab_cast import Base  # noqa: E402

EP = "https://anime.example/watch/naruto-episode-"


class Store(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "store.json")
        self.s = PhoneStore(self.path)

    def test_a_show_in_progress_is_offered_to_resume(self):
        self.s.save_progress(EP + "5", 600, 1400, "Naruto 5", "anime.example", next_url=EP + "6")
        rows = self.s.continue_list()
        self.assertEqual([(r["url"], r["pos"], r["finished"]) for r in rows], [(EP + "5", 600.0, False)])

    def test_moving_to_the_next_episode_replaces_the_row_instead_of_adding_one(self):
        self.s.save_progress(EP + "5", 1390, 1400, "Naruto 5")
        self.s.save_progress(EP + "6", 120, 1400, "Naruto 6")
        self.assertEqual([w["url"] for w in self.s.watching], [EP + "6"])

    def test_different_shows_each_keep_a_row(self):
        self.s.save_progress(EP + "5", 600, 1400, "A")
        self.s.save_progress("https://anime.example/watch/bleach-episode-3", 600, 1400, "B")
        self.assertEqual(len(self.s.watching), 2)

    def test_the_last_half_minute_counts_as_finished_and_offers_the_next_episode(self):
        self.s.save_progress(EP + "5", 1380, 1400, "Naruto 5", next_url=EP + "6")
        row = self.s.continue_list()[0]
        self.assertTrue(row["finished"])
        self.assertEqual(row["next_url"], EP + "6")

    def test_a_finished_show_with_no_known_next_episode_is_not_listed(self):
        self.s.save_progress(EP + "5", 1400, 1400, "Naruto 5", finished=True)
        self.assertEqual(self.s.continue_list(), [])

    def test_the_first_seconds_are_not_worth_resuming(self):
        self.s.save_progress(EP + "5", 4, 1400, "Naruto 5")
        self.assertEqual(self.s.continue_list(), [])

    def test_a_later_save_for_the_same_page_keeps_the_picture_and_next_link(self):
        self.s.save_progress(EP + "5", 100, 1400, "N", "site", "https://i.example/a.jpg", EP + "6")
        self.s.save_progress(EP + "5", 200, 1400, "N")
        row = self.s.progress_for(EP + "5")
        self.assertEqual((row["pos"], row["image"], row["next_url"]), (200.0, "https://i.example/a.jpg", EP + "6"))

    def test_it_survives_a_restart_and_an_old_file_without_the_list(self):
        self.s.save_progress(EP + "5", 600, 1400, "Naruto 5")
        self.assertEqual(PhoneStore(self.path).progress_for(EP + "5")["pos"], 600.0)
        old = os.path.join(tempfile.mkdtemp(), "old.json")
        with open(old, "w") as f:
            json.dump({"bookmarks": [], "history": []}, f)
        self.assertEqual(PhoneStore(old).watching, [])

    def test_junk_rows_in_the_file_are_dropped(self):
        with open(self.path, "w") as f:
            json.dump({"watching": [{"url": "javascript:alert(1)", "pos": 5, "dur": 100}, {"url": EP + "1", "pos": "x"},
                                    {"url": EP + "1", "pos": 5, "dur": 0}, 7, {"url": EP + "2", "pos": 50, "dur": 100}]}, f)
        self.assertEqual([w["url"] for w in PhoneStore(self.path).watching], [EP + "2"])

    def test_the_list_is_capped_and_can_be_edited(self):
        for i in range(60):
            self.s.save_progress(f"https://s{i}.example/watch/show-episode-1", 100, 1400, f"S{i}")
        self.assertEqual(len(self.s.watching), 50)
        self.s.remove_watching("https://s59.example/watch/show-episode-1")
        self.assertIsNone(self.s.progress_for("https://s59.example/watch/show-episode-1"))
        self.s.clear_watching()
        self.assertEqual(self.s.watching, [])

    def test_bookmarks_and_history_are_untouched_by_all_this(self):
        self.s.toggle_bookmark("https://a.example/", "A")
        self.s.visit("https://b.example/", "B")
        self.s.save_progress(EP + "5", 600, 1400, "Naruto 5")
        again = PhoneStore(self.path)
        self.assertEqual(len(again.bookmarks), 1)
        self.assertEqual(len(again.history), 1)


class NextUrl(unittest.TestCase):
    def test_the_pages_own_link_wins(self):
        links = ["/watch/naruto-episode-4", "/watch/naruto-episode-6", "/watch/bleach-episode-6"]
        self.assertEqual(next_url(EP + "5", links), EP + "6")

    def test_an_episode_spelled_out_in_the_address_gets_one_added(self):
        self.assertEqual(next_url(EP + "5", []), EP + "6")
        self.assertEqual(next_url("https://x.example/tv/123?s=1&e=5"), "https://x.example/tv/123?s=1&e=6")
        self.assertEqual(next_url("https://x.example/one-piece/ep-12#top"), "https://x.example/one-piece/ep-13")

    def test_a_title_number_is_not_an_episode(self):
        self.assertIsNone(next_url("https://x.example/movie/1507696"))
        self.assertIsNone(next_url("https://x.example/watch?v=abc", ["/watch?v=abd"]))

    def test_a_link_to_a_different_show_is_not_used(self):
        self.assertEqual(next_url("https://x.example/a/show-episode-9", ["/a/other-episode-10"]),
                         "https://x.example/a/show-episode-10")

    def test_nothing_known_means_no_guess(self):
        self.assertIsNone(next_url("https://x.example/watch/some-film", ["/about", "/watch/some-film"]))


class Recording(Base):
    def setUp(self):
        super().setUp()
        self.saved = []
        tab = self.tab
        tab.cast_page, tab.cast_site, tab.cast_image, tab.cast_next = EP + "5", "anime.example", "", EP + "6"
        tab._progress_at, tab.link_at = 0.0, None
        tab.app.phone_store = SimpleNamespace(save_progress=lambda *a: self.saved.append(a),
                                              progress_for=lambda url: None)
        tab.sync = SimpleNamespace(active=True, live=False, dur=1400.0, offset=0.0, settle_until=0.0, title="Naruto 5",
                                   tv_seen=0.0)

    def wait(self):
        for _ in range(100):
            if self.saved:
                return
            time.sleep(0.01)

    def test_where_the_tv_is_gets_written_while_following(self):
        self.tab._record_progress({"state": "play", "app": "RokuCast", "pos": 300.0, "dur": 1400})
        self.wait()
        self.assertEqual(self.saved[0][:4], (EP + "5", 300.0, 1400.0, "Naruto 5"))
        self.assertEqual(self.saved[0][6], EP + "6")

    def test_it_is_written_every_so_often_not_every_second(self):
        st = {"state": "play", "app": "RokuCast", "pos": 300.0, "dur": 1400}
        self.tab._record_progress(st)
        self.wait()
        self.tab._record_progress(dict(st, pos=301.0))
        time.sleep(0.1)
        self.assertEqual(len(self.saved), 1)

    def test_the_offset_of_a_restarted_stream_is_added(self):
        self.tab.sync.offset = 500.0
        self.tab._record_progress({"state": "play", "app": "RokuCast", "pos": 10.0, "dur": 900})
        self.wait()
        self.assertEqual(self.saved[0][1], 510.0)

    def test_nothing_is_written_for_live_streams_short_clips_buffering_or_another_app(self):
        good = {"state": "play", "app": "RokuCast", "pos": 300.0, "dur": 1400}
        for change in ({"state": "buffer"}, {"app": "Netflix"}):
            self.tab._record_progress(dict(good, **change))
        self.tab.sync.live = True
        self.tab._record_progress(good)
        self.tab.sync.live, self.tab.sync.dur = False, 60.0
        self.tab._record_progress(good)
        self.tab.sync.dur, self.tab.sync.settle_until = 1400.0, time.time() + 60
        self.tab._record_progress(good)
        self.tab._record_progress(None)
        time.sleep(0.1)
        self.assertEqual(self.saved, [])

    def test_finishing_marks_it_finished_and_leaving_keeps_the_furthest_point(self):
        self.tab.sync.tv_seen = 800.0
        self.tab._post = lambda fn: None
        self.tab._set_presence = lambda *a: None
        self.tab.relay = None
        self.tab._note = lambda *a: None
        self.tab._following_stopped(web_tab.WebSync.FINISHED)
        self.wait()
        self.assertEqual(self.saved[-1][1:3], (1400.0, 1400.0))
        self.assertTrue(self.saved[-1][7])
        self.saved.clear()
        self.tab._following_stopped(web_tab.WebSync.LEFT_PAGE)
        self.wait()
        self.assertEqual(self.saved[-1][1], 800.0)
        self.assertFalse(self.saved[-1][7])


class Resuming(Base):
    def setUp(self):
        super().setUp()
        tab = self.tab
        tab.link, tab.link_at, tab.home = None, None, "https://duckduckgo.com"
        tab.guard.navigate = mock.Mock()
        tab.guard.seek_page = mock.Mock()
        tab.guard.has_fresh_stream = lambda: True
        tab.guard.snapshot = lambda: {"running": True, "url": "x", "title": "", "unsupported": ""}
        tab.alive = lambda: True
        tab.cast = mock.Mock()
        self.after = []
        tab.app.after = lambda ms, fn: self.after.append((ms, fn))
        tab._post = lambda fn: fn()
        tab._note = lambda *a: None
        tab.LINK_SETTLE = 0.0

    def run_steps(self):
        for _ in range(6):
            if not self.tab.link:
                break
            self.tab._link_step()

    def test_a_resume_moves_the_page_before_casting_from_it(self):
        self.assertIn("2:05", self.tab.cast_link(EP + "5", 125))
        self.run_steps()
        self.tab.guard.navigate.assert_called_once_with(EP + "5")
        self.tab.guard.seek_page.assert_called_once_with(125, 0)
        self.tab.cast.assert_not_called()                      # it waits for the page's player to move
        ms, fn = self.after[-1]
        self.assertGreaterEqual(ms, 2000)
        fn()
        self.tab.cast.assert_called_once()
        self.assertIsNone(self.tab.link_at)

    def test_an_ordinary_link_casts_straight_away_without_seeking(self):
        self.tab.cast_link(EP + "5")
        self.run_steps()
        self.tab.guard.seek_page.assert_not_called()
        self.tab.cast.assert_called_once()

    def test_phone_continue_actions(self):
        row = {"url": EP + "5", "pos": 600.0, "dur": 1400.0, "next_url": EP + "6", "finished": False}
        store = SimpleNamespace(progress_for=lambda u: dict(row) if u == EP + "5" else None,
                                remove_watching=mock.Mock(), clear_watching=mock.Mock())
        self.tab.app.phone_store = store
        self.tab.cast_link = mock.Mock(return_value="ok")
        self.assertEqual(self.tab.phone_continue("resume", EP + "5"), "ok")
        self.tab.cast_link.assert_called_with(EP + "5", 600.0)
        self.tab.phone_continue("start", EP + "5")
        self.tab.cast_link.assert_called_with(EP + "5", None)
        self.tab.phone_continue("next", EP + "5")
        self.tab.cast_link.assert_called_with(EP + "6")
        self.tab.phone_continue("remove", EP + "5")
        store.remove_watching.assert_called_once_with(EP + "5")
        self.tab.phone_continue("clear")
        store.clear_watching.assert_called_once()
        with self.assertRaises(ValueError):
            self.tab.phone_continue("resume", "https://gone.example/")
        with self.assertRaises(ValueError):
            self.tab.phone_continue("explode", EP + "5")

    def test_a_finished_row_restarts_from_the_beginning_not_the_end(self):
        row = {"url": EP + "5", "pos": 1400.0, "dur": 1400.0, "next_url": "", "finished": True}
        self.tab.app.phone_store = SimpleNamespace(progress_for=lambda u: dict(row))
        self.tab.cast_link = mock.Mock()
        self.tab.phone_continue("resume", EP + "5")
        self.tab.cast_link.assert_called_with(EP + "5", None)
        with self.assertRaises(ValueError):
            self.tab.phone_continue("next", EP + "5")


class PhoneApi(unittest.TestCase):
    def setUp(self):
        self.calls = []
        calls = self.calls
        b = SimpleNamespace(web_continue=lambda action, url: calls.append((action, url)) or "Opening it",
                            web_pages=lambda: {"bookmarks": [], "history": [], "continue": [{"url": EP + "5"}],
                                               "current": {}})
        self.srv = pr.PhoneRemote(b, "tok", b"", port=0, bind="127.0.0.1")
        self.srv.start()
        self.addCleanup(self.srv.stop)

    def call(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.srv.port}{path}", data=data,
                                     headers={"X-Token": "tok", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, {}

    def test_the_list_comes_with_the_pages(self):
        self.assertEqual(self.call("/api/web/pages")[1]["continue"], [{"url": EP + "5"}])

    def test_actions_reach_the_backend_and_unknown_ones_do_not(self):
        code, body = self.call("/api/web/continue", {"action": "resume", "url": EP + "5"})
        self.assertEqual((code, body["message"]), (200, "Opening it"))
        self.assertEqual(self.call("/api/web/continue", {"action": "explode", "url": "x"})[0], 400)
        self.assertEqual(self.calls, [("resume", EP + "5")])

    def test_the_page_has_a_continue_tab(self):
        with open(pr.__file__, encoding="utf-8") as f:
            self.assertIn('data-pg="continue"', f.read())


if __name__ == "__main__":
    unittest.main()
