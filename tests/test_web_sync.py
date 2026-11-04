import os
import queue
import sys
import threading
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream_finder import Candidate  # noqa: E402
from web_sync import WebSync  # noqa: E402


class FakeGuard:
    def __init__(self):
        self.page_events, self.epoch = queue.Queue(), 0
        self.mirrors, self.seeks, self.time_now, self.playing = [], [], 100.0, True

    def mirror(self, on):
        self.mirrors.append(on)

    def seek_page(self, t, dur):
        self.seeks.append((round(t, 1), dur))

    def video_time(self, dur=0):
        return self.time_now

    def video_playing(self, dur=0):
        return self.playing


class FakeTV:
    """A TV whose position moves a little each time it's asked, like a playing Roku."""

    def __init__(self):
        self.st, self.pos, self.keys, self.casts, self.app = "play", 100.0, [], [], "RokuCast"

    def state(self):
        if self.st == "play":
            self.pos += 0.5
        return {"state": self.st, "app": self.app, "pos": self.pos, "dur": 1400}

    def key(self, name):
        self.keys.append(name)
        if name == "Play":
            self.st = "pause" if self.st == "play" else "play"
        elif name in ("Right", "Left"):
            self.pos += 10 if name == "Right" else -10

    def cast(self, url):
        self.casts.append(url)
        self.pos, self.st = 0.0, "play"


def wait_for(fn, seconds=4.0):
    end = time.time() + seconds
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return fn()


class Base(unittest.TestCase):
    kind = "mp4"

    def setUp(self):
        self.guard, self.tv, self.stopped, self.adds = FakeGuard(), FakeTV(), [], []
        self.sess = SimpleNamespace(actual_start=60.0, hits=1)

        def relay_add(url, headers, kind, start):
            self.adds.append((url, kind, start))
            self.sess.actual_start = float(int(start // 6) * 6)         # cut at a 6 s segment boundary
            return f"http://relay/s/{len(self.adds)}/index.m3u8", self.sess

        self.sync = WebSync(self.guard, self.tv, relay_add, on_stop=self.stopped.append)
        self.sync.SETTLE, self.sync.lead = 0.0, 3.0
        self.cand = Candidate(url="https://site/x.m3u8", kind=self.kind, headers={"Referer": "r"}, duration=1400)
        self.sync.start(self.cand, offset=0.0, page_time=None, title="Ep")

    def tearDown(self):
        self.sync.stop("", quiet=True)

    def event(self, kind, t=100.0, paused=False, dur=1400):
        self.guard.page_events.put({"kind": kind, "t": t, "paused": paused, "dur": dur})


class Mp4Mirroring(Base):
    def test_start_mutes_the_page(self):
        self.assertEqual(self.guard.mirrors[0], True)

    def test_pause_presses_play_once_and_only_when_the_tv_is_playing(self):
        self.event("pause", paused=True)
        self.assertTrue(wait_for(lambda: self.tv.keys == ["Play"]))
        self.event("pause", paused=True)                      # the TV is already paused: no second toggle
        time.sleep(0.6)
        self.assertEqual(self.tv.keys, ["Play"])

    def test_play_resumes_a_paused_tv(self):
        self.tv.st = "pause"
        self.event("play")
        self.assertTrue(wait_for(lambda: self.tv.st == "play" and self.tv.keys[:1] == ["Play"]))

    def test_a_scrub_forward_steps_the_tv_with_its_keys(self):
        self.tv.pos = 100.0
        self.event("seeked", t=160.0)                          # about +60 s
        self.assertTrue(wait_for(lambda: self.tv.keys.count("Right") >= 6))
        self.assertNotIn("Left", self.tv.keys)
        self.assertEqual(self.adds, [])                        # MP4 can't be restarted at a time

    def test_a_scrub_back_steps_left(self):
        self.tv.pos = 300.0
        self.event("seeked", t=250.0)
        self.assertTrue(wait_for(lambda: self.tv.keys.count("Left") >= 4))
        self.assertNotIn("Right", self.tv.keys)

    def test_a_tiny_jump_is_just_playback(self):
        self.tv.pos = 100.0
        self.event("seeked", t=101.0)
        time.sleep(0.6)
        self.assertEqual(self.tv.keys, [])

    def test_other_videos_on_the_page_are_ignored(self):
        self.event("pause", paused=True, dur=15)               # a 15 s ad on the same page
        time.sleep(0.6)
        self.assertEqual(self.tv.keys, [])

    def test_a_video_with_no_duration_yet_is_ignored(self):
        # e.g. a "next episode" click swaps in a new <video> whose metadata (and duration) hasn't loaded yet;
        # its early events shouldn't be mistaken for the video we're actually casting.
        self.event("seeked", t=0.0, dur=0)
        time.sleep(0.6)
        self.assertEqual(self.tv.keys, [])
        self.assertEqual(self.adds, [])

    def test_recasting_stops_the_old_thread_before_the_new_one_runs(self):
        # e.g. clicking "next episode": start() is called again on the same WebSync while the first thread may
        # still be mid-loop. If it isn't fully stopped first, both threads read self.guard.page_events and poke
        # the TV, and a pause can get toggled straight back to play by the stale one.
        old_thread = self.sync._thread
        cand2 = Candidate(url="https://site/y.m3u8", kind=self.kind, headers={}, duration=2000)
        self.sync.start(cand2, offset=0.0, page_time=None, title="Ep2")
        self.assertFalse(old_thread.is_alive())
        self.event("pause", paused=True, dur=2000)
        self.assertTrue(wait_for(lambda: self.tv.st == "pause"))
        time.sleep(0.6)
        self.assertEqual(self.tv.keys, ["Play"])

    def test_leaving_the_page_stops_following_and_gives_the_sound_back(self):
        self.guard.epoch += 1
        self.assertTrue(wait_for(lambda: bool(self.stopped), 4))
        self.assertIn("left the page", self.stopped[0])
        self.assertEqual(self.guard.mirrors[-1], False)
        self.assertFalse(self.sync.active)

    def test_the_tv_going_elsewhere_stops_following(self):
        self.sync.last_alive = 0                               # make the periodic TV check due
        self.tv.app = "Netflix"
        self.assertTrue(wait_for(lambda: bool(self.stopped), 4))
        self.assertIn("TV stopped", self.stopped[0])
        self.assertEqual(self.guard.mirrors[-1], False)

    def test_drift_nudges_the_muted_page_onto_the_tv(self):
        self.guard.time_now = 90.0                             # page is 10 s behind the TV
        self.tv.pos = 100.0
        self.event("play")
        self.assertTrue(wait_for(lambda: bool(self.guard.seeks), 4))
        self.assertAlmostEqual(self.guard.seeks[0][0], 100.8, delta=1.5)


class HlsMirroring(Base):
    kind = "hls"

    def test_a_scrub_restarts_the_stream_at_the_new_position(self):
        self.tv.pos = 100.0
        self.event("seeked", t=600.0)
        self.assertTrue(wait_for(lambda: len(self.adds) == 1))
        url, kind, start = self.adds[0]
        self.assertEqual(kind, "hls")
        self.assertAlmostEqual(start, 603.0, delta=0.1)        # the scrub target plus the learned lead
        self.assertEqual(self.tv.casts, ["http://relay/s/1/index.m3u8"])
        self.assertEqual(self.tv.keys.count("Right"), 0)
        self.assertEqual(self.sync.offset, 600.0)              # where the new stream really begins

    def test_the_offset_turns_the_tvs_position_into_the_videos_time(self):
        self.tv.pos = 100.0
        self.event("seeked", t=600.0)
        self.assertTrue(wait_for(lambda: len(self.adds) == 1))
        wait_for(lambda: self.sync.offset == 600.0)
        self.tv.pos = 12.0
        self.assertAlmostEqual(self.sync._tv_time(), 612.5, delta=1.0)

    def test_scrubbing_while_paused_restarts_paused(self):
        self.tv.pos = 100.0
        self.event("seeked", t=900.0, paused=True)
        self.assertTrue(wait_for(lambda: len(self.adds) == 1))
        self.assertAlmostEqual(self.adds[0][2], 900.0, delta=0.1)   # no lead needed when nothing is playing
        self.assertTrue(wait_for(lambda: self.tv.st == "pause"))

    def test_a_burst_of_scrub_events_restarts_once(self):
        self.tv.pos = 100.0
        for t in (200.0, 300.0, 400.0, 500.0):
            self.event("seeked", t=t)
        self.assertTrue(wait_for(lambda: len(self.adds) >= 1))
        time.sleep(0.8)
        self.assertEqual(len(self.adds), 1)
        self.assertAlmostEqual(self.adds[0][2], 503.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
