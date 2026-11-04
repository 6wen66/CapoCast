import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brave_profile  # noqa: E402
from web_guard import WebGuard, _total_size, normalize_address  # noqa: E402


class Address(unittest.TestCase):
    def test_links_and_hosts_and_searches(self):
        self.assertEqual(normalize_address("https://a.example/x"), "https://a.example/x")
        self.assertEqual(normalize_address("miruro.cx"), "https://miruro.cx")
        self.assertEqual(normalize_address("site.com/watch?x=1"), "https://site.com/watch?x=1")
        self.assertTrue(normalize_address("one piece ep 1").startswith("https://duckduckgo.com/?q=one+piece"))

    def test_empty_goes_home(self):
        self.assertEqual(normalize_address("  ", home="https://h.example"), "https://h.example")


class Sizes(unittest.TestCase):
    def test_content_range_beats_content_length(self):
        self.assertEqual(_total_size({"Content-Range": "bytes 0-1/12345", "Content-Length": "2"}), 12345)

    def test_plain_length_and_missing(self):
        self.assertEqual(_total_size({"content-length": "777"}), 777)
        self.assertEqual(_total_size({}), 0)


class FilterSeeding(unittest.TestCase):
    def make_source(self, root, comp, version, with_list=True):
        d = os.path.join(root, comp, version)
        os.makedirs(d)
        if with_list:
            open(os.path.join(d, "list.txt"), "w").write("||ads.example^\n")
        open(os.path.join(d, "manifest.json"), "w").write("{}")

    def test_copies_newest_version_and_replaces_old(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            comp = "a" * 32
            self.make_source(src, comp, "1.0.9")
            self.make_source(src, comp, "1.0.10")                  # numerically newer than 1.0.9
            os.makedirs(os.path.join(dst, comp, "0.0.1"))            # stale copy from an earlier launch
            self.assertTrue(brave_profile.seed_filters(dst, source=src))
            self.assertEqual(os.listdir(os.path.join(dst, comp)), ["1.0.10"])

    def test_skips_new_tab_photo_bundles_and_reports_missing_lists(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            skipped = sorted(brave_profile.SKIP)[0]
            self.make_source(src, skipped, "1.0.0")
            self.assertFalse(brave_profile.seed_filters(dst, source=src))
            self.assertFalse(os.path.exists(os.path.join(dst, skipped)))

    def test_missing_source_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as dst:
            self.assertFalse(brave_profile.seed_filters(dst, source=os.path.join(dst, "nope")))


class AddressChangeWithoutReload(unittest.TestCase):
    def setUp(self):
        self.guard = WebGuard(lambda m: None, home="about:blank")
        self.guard._main, self.guard._main_frame, self.guard._page_key = "page", "f1", "https://site/watch?e=4"
        self.guard._add_candidate("https://cdn/ep4.m3u8", "hls", "f1", {}, 0)

    def move(self, url, session="page", frame="f1"):
        self.guard._on_Page_navigatedWithinDocument({"frameId": frame, "url": url}, session)

    def urls(self):
        return [c.url for c in self.guard.candidates()]

    def test_next_episode_leaves_only_the_new_stream(self):
        self.move("https://site/watch?e=5")
        self.guard._add_candidate("https://cdn/ep5.m3u8", "hls", "f1", {}, 0)
        self.assertEqual(self.urls(), ["https://cdn/ep5.m3u8"])

    def test_a_fresh_stream_is_only_reported_once_one_arrives(self):
        self.assertTrue(self.guard.has_fresh_stream())
        self.move("https://site/watch?e=5")
        self.assertFalse(self.guard.has_fresh_stream())
        self.guard._add_candidate("https://cdn/ep5.m3u8", "hls", "f1", {}, 0)
        self.assertTrue(self.guard.has_fresh_stream())

    def test_the_old_stream_stays_usable_when_the_video_did_not_change(self):
        self.move("https://site/watch?e=4&t=30")
        self.assertEqual(self.urls(), ["https://cdn/ep4.m3u8"])

    def test_a_stream_requested_again_counts_as_current(self):
        self.move("https://site/watch?e=5")
        self.guard._add_candidate("https://cdn/ep5.m3u8", "hls", "f1", {}, 0)
        self.guard._add_candidate("https://cdn/ep4.m3u8", "hls", "f1", {}, 0)
        self.assertCountEqual(self.urls(), ["https://cdn/ep4.m3u8", "https://cdn/ep5.m3u8"])

    def test_a_hash_change_or_an_iframe_moving_is_not_a_new_page(self):
        self.move("https://site/watch?e=4#comments")
        self.move("https://ads/x", session="iframe")
        self.move("https://site/other", frame="f2")
        self.guard._add_candidate("https://cdn/extra.m3u8", "hls", "f1", {}, 0)
        self.assertCountEqual(self.urls(), ["https://cdn/ep4.m3u8", "https://cdn/extra.m3u8"])


if __name__ == "__main__":
    unittest.main()
