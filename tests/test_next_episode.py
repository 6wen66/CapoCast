import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from next_episode import NextEpisode  # noqa: E402
from stream_finder import same_show  # noqa: E402

EP4 = "https://site.example/watch/tv/289517?s=1&e=4"
EP5 = "https://site.example/watch/tv/289517?s=1&e=5"
EP6 = "https://site.example/watch/tv/289517?s=1&e=6"
LONG = [{"playing": True, "dur": 2400.0}]


class SameShow(unittest.TestCase):
    def test_neighbouring_episodes(self):
        for a, b in ((EP4, EP5),
                     ("https://s.example/watch/tv/1?s=1&e=8", "https://s.example/watch/tv/1?s=2&e=1"),
                     ("https://s.example/watch/naruto-episode-4", "https://s.example/watch/naruto-episode-5"),
                     ("https://s.example/anime/naruto/episode/4", "https://s.example/anime/naruto/episode/5"),
                     ("https://s.example/watch?id=7&ep=4", "https://s.example/watch?id=7&ep=5"),
                     ("https://www.s.example/w/x-4", "https://cdn.s.example/w/x-5")):
            self.assertTrue(same_show(a, b), (a, b))

    def test_different_titles_and_pages(self):
        for a, b in ((EP4, EP4),
                     (EP4, "https://site.example/title/tv/289517/"),
                     (EP4, "https://site.example/"),
                     (EP4, "https://site.example/watch/tv/999?s=1&e=1"),
                     (EP4, "https://site.example/watch/movie/1507696"),
                     ("https://s.example/watch/movie/1507696", "https://s.example/watch/movie/1507697"),
                     ("https://s.example/watch?v=abc", "https://s.example/watch?v=xyz"),
                     ("https://s.example/watch?id=7&ep=4", "https://s.example/watch?id=8&ep=4"),
                     ("https://s.example/watch/naruto-4", "https://s.example/watch/bleach-4"),
                     ("https://s.example/watch?e=4", "https://s.example/watch?e=4&t=30"),
                     (EP4, "https://other.example/watch/tv/289517?s=1&e=5"),
                     ("about:blank", "about:blank")):
            self.assertFalse(same_show(a, b), (a, b))


class Watching(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.watch = NextEpisode(clock=lambda: self.now)
        self.watch.arm(EP4)

    def run_for(self, seconds, url=EP5, videos=LONG, fresh=True):
        """Tick at the UI's pace and return the last answer."""
        left = None
        for _ in range(int(seconds * 4)):
            self.now += 0.25
            left = self.watch.tick(url, videos, fresh)
        return left

    def test_warns_then_goes(self):
        self.assertIsNone(self.run_for(2.5))
        self.assertFalse(self.watch.counting)
        left = self.run_for(1)
        self.assertGreater(left, 0)
        self.assertTrue(self.watch.counting)
        self.assertEqual(self.run_for(6), 0)

    def test_nothing_happens_on_the_page_that_was_cast(self):
        self.assertIsNone(self.run_for(20, url=EP4))

    def test_a_trailer_on_the_title_page_is_left_alone(self):
        self.assertIsNone(self.run_for(20, url="https://site.example/title/tv/289517/"))

    def test_a_different_title_is_left_alone(self):
        self.assertIsNone(self.run_for(20, url="https://site.example/watch/movie/1507696"))

    def test_it_waits_for_a_new_stream(self):
        self.assertIsNone(self.run_for(20, fresh=False))

    def test_a_short_ad_on_the_next_episode_does_not_count(self):
        self.assertIsNone(self.run_for(20, videos=[{"playing": True, "dur": 30.0}]))

    def test_a_video_that_is_not_playing_does_not_count(self):
        self.assertIsNone(self.run_for(20, videos=[{"playing": False, "dur": 2400.0}]))

    def test_pausing_during_the_warning_stops_it(self):
        self.run_for(4)
        self.assertTrue(self.watch.counting)
        self.assertIsNone(self.run_for(1, videos=[{"playing": False, "dur": 2400.0}]))
        self.assertFalse(self.watch.counting)

    def test_cancel_leaves_that_episode_but_follows_the_next(self):
        self.run_for(4)
        self.watch.skip(EP5)
        self.assertFalse(self.watch.counting)
        self.assertIsNone(self.run_for(20))
        self.run_for(4, url=EP6)
        self.assertTrue(self.watch.counting)

    def test_disarmed_it_does_nothing(self):
        self.watch.disarm()
        self.assertIsNone(self.run_for(20))

    def test_after_the_tv_stops_it_keeps_watching_for_a_while(self):
        self.watch.grace()
        self.now += NextEpisode.GRACE - 30
        self.assertEqual(self.run_for(10), 0)

    def test_it_gives_up_when_the_page_never_gets_there(self):
        self.watch.grace()
        self.now += NextEpisode.GRACE + 1
        self.assertIsNone(self.run_for(20))
        self.assertIsNone(self.watch.url)

    def test_grace_does_not_start_watching_after_a_stop(self):
        self.watch.disarm()
        self.watch.grace()
        self.assertIsNone(self.watch.expires)


if __name__ == "__main__":
    unittest.main()
