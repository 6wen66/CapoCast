import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import web_tab  # noqa: E402
from stream_finder import Candidate  # noqa: E402


class FakeRelay:
    def __init__(self, sess):
        self.sess, self.added = sess, []

    def add(self, url, headers, kind, start):
        self.added.append(url)
        return "http://relay/s/1/index.m3u8"

    def session_for(self, url):
        return self.sess


class Base(unittest.TestCase):
    def setUp(self):
        self.ui_calls, self.keys, self.casts, self.painted = [], [], [], []
        self.sess = SimpleNamespace(hits=0, error="", actual_start=0.0, failures=[])
        remote = SimpleNamespace(active_app=lambda host: "", cast_url=lambda host, url: self.casts.append(url))
        tab = self.tab = web_tab.WebTab.__new__(web_tab.WebTab)
        # like Tk: nothing queued from a worker runs until the main loop gets to it, after the worker has moved on
        tab.app = SimpleNamespace(after=lambda ms, fn: self.ui_calls.append(fn), _host="10.0.0.5",
                                  autocast_off=lambda: None)
        tab.ui = SimpleNamespace(log=lambda m: None, Remote=remote, RED="r", MUTED="m", AMBER="a", GREEN="g",
                                 ACCENT="acc", ACCENT_HI="hi")
        tab.tv = SimpleNamespace(key=self.keys.append)
        tab.busy, tab.attempt, tab.cast_at, tab.sync = False, 0, 0.0, None
        tab.note, tab.note_until = ("", "m"), 0.0
        tab.relay = FakeRelay(self.sess)
        tab.guard = SimpleNamespace(video_time=lambda d: None, candidates=lambda: [], page_videos=lambda: [],
                                    active_frames=set, snapshot=lambda: {"unsupported": "", "url": "https://s/w",
                                                                         "title": "Ep", "running": True})
        tab.alive = lambda: True
        tab.cast_btn = SimpleNamespace(style=lambda text, *a, **k: self.painted.append((text, k.get("enabled", True))))
        tab._ensure_relay = lambda host: tab.relay

    def begin(self):
        """A cast in progress, as cast() leaves it, without starting its worker."""
        self.tab.attempt += 1
        self.tab.busy, self.tab.cast_at = True, 0.0
        return self.tab.attempt

    def run_worker(self, attempt, fn, *args):
        t = threading.Thread(target=self.tab._guarded, args=(attempt, fn, *args))
        t.start()
        t.join(10)
        self.assertFalse(t.is_alive())

    def run_ui(self):
        calls, self.ui_calls[:] = list(self.ui_calls), []
        for fn in calls:
            fn()


class AFailedCast(Base):
    def fail_with(self, error):
        def fn(attempt):
            raise error
        self.run_worker(self.begin(), fn)
        self.run_ui()

    def test_frees_the_button_and_says_why(self):
        self.fail_with(web_tab.CastError("No video found yet. Press play on the video, then Cast."))
        self.assertFalse(self.tab.busy)
        self.assertIn("No video found yet", self.tab.note[0])

    def test_an_unexpected_crash_frees_the_button_too(self):
        self.fail_with(ValueError("boom"))
        self.assertFalse(self.tab.busy)
        self.assertIn("boom", self.tab.note[0])

    def test_finding_nothing_on_the_page_ends_in_a_message(self):
        with mock.patch.object(web_tab, "ytdlp_candidate", return_value=None):
            self.run_worker(self.begin(), self.tab._find, "https://s/w", "Ep")
        self.run_ui()
        self.run_ui()
        self.assertFalse(self.tab.busy)
        self.assertIn("No video found", self.tab.note[0])


class CancelButton(Base):
    def test_the_button_is_a_live_cancel_while_casting(self):
        self.tab.busy = True
        self.tab._paint_cast_button()
        self.assertEqual(self.painted[-1], ("Cancel", True))
        self.tab.busy = False
        self.tab._paint_cast_button()
        self.assertEqual(self.painted[-1], ("Cast to TV", True))

    def test_pressing_it_frees_the_button_at_once(self):
        self.begin()
        self.tab.cast()
        self.assertFalse(self.tab.busy)
        self.assertEqual(self.tab.note[0], "Cast cancelled")

    def test_a_double_click_on_cast_does_not_cancel_it(self):
        self.begin()
        self.tab.cast_at = time.time()
        self.tab.cast()
        self.assertTrue(self.tab.busy)

    def test_a_cancelled_cast_never_reaches_the_tv(self):
        attempt = self.begin()
        self.tab.cast()
        cand = Candidate(url="https://cdn/ep.m3u8", kind="hls")
        self.run_worker(attempt, self.tab._send, cand, "Ep")
        self.assertEqual(self.casts, [])
        self.assertEqual(self.tab.relay.added, [])

    def test_a_late_failure_from_a_cancelled_cast_stays_quiet(self):
        attempt = self.begin()
        self.tab.cast()

        def fn(a):
            raise web_tab.CastError("too late")
        self.run_worker(attempt, fn)
        self.run_ui()
        self.assertEqual(self.tab.note[0], "Cast cancelled")

    def test_a_late_failure_cannot_touch_the_next_cast(self):
        old = self.begin()
        self.tab.cast()
        self.begin()

        def fn(a):
            raise web_tab.CastError("old one failed")
        self.run_worker(old, fn)
        self.run_ui()
        self.assertTrue(self.tab.busy)

    def test_cancelling_after_the_tv_was_told_backs_it_out(self):
        attempt = self.begin()
        self.tab.sync = SimpleNamespace(start=mock.Mock(), active=False, lead=3.0)
        cand = Candidate(url="https://cdn/ep.m3u8", kind="hls")
        worker = threading.Thread(target=self.tab._guarded, args=(attempt, self.tab._send, cand, "Ep"))
        worker.start()
        end = time.time() + 5
        while not self.casts and time.time() < end:
            time.sleep(0.02)
        self.tab.cast()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.keys, ["Back"])
        self.tab.sync.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
