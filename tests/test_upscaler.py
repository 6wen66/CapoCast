import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import upscaler  # noqa: E402
import web_tab  # noqa: E402
from media_relay import MediaRelay, trim_hls  # noqa: E402
from stream_finder import Candidate  # noqa: E402
from upscaler import UpscaleSession, command, master_playlist, media_playlist  # noqa: E402
from web_sync import WebSync  # noqa: E402


class Playlists(unittest.TestCase):
    def test_the_whole_video_is_listed_and_the_last_piece_is_short(self):
        text = media_playlist("v", 10.5)
        self.assertEqual(text.count("#EXTINF"), 3)
        self.assertIn("#EXTINF:2.500,\nv00002.m4s", text)
        self.assertIn('#EXT-X-MAP:URI="v_init.mp4"', text)
        self.assertTrue(text.rstrip().endswith("#EXT-X-ENDLIST"))

    def test_a_seek_trims_it_like_any_other_playlist_and_keeps_the_init_segment(self):
        text, skipped = trim_hls(media_playlist("v", 40), 13)
        self.assertEqual(skipped, 12.0)
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:3", text)
        self.assertIn('#EXT-X-MAP:URI="v_init.mp4"', text)
        self.assertNotIn("v00002.m4s", text)
        self.assertIn("v00003.m4s", text)

    def test_audio_is_a_separate_track(self):
        text = master_playlist(True)
        self.assertIn('TYPE=AUDIO,GROUP-ID="a"', text)
        self.assertIn('URI="audio.m3u8"', text)
        self.assertIn('AUDIO="a"', text)

    def test_a_silent_video_names_no_audio(self):
        text = master_playlist(False)
        self.assertNotIn("AUDIO", text)
        self.assertNotIn("mp4a", text)


class Command(unittest.TestCase):
    def build(self, kind="hls", first=3, audio=True):
        return command("ffmpeg", "https://cdn/x", {"User-Agent": "UA", "Referer": "https://site/", "Cookie": "a=1"},
                       kind, first, audio)

    def value(self, cmd, flag):
        return cmd[cmd.index(flag) + 1]

    def test_a_restart_lands_on_the_timeline_of_the_playlist(self):
        cmd = self.build(first=3)
        self.assertEqual(self.value(cmd, "-ss"), "12")
        self.assertEqual(self.value(cmd, "-start_number"), "3")
        self.assertEqual(cmd.count("-output_ts_offset"), 2)
        self.assertEqual(self.value(cmd, "-output_ts_offset"), "12")

    def test_the_site_gets_the_browsers_headers(self):
        cmd = self.build()
        self.assertEqual(self.value(cmd, "-user_agent"), "UA")
        self.assertEqual(self.value(cmd, "-headers"), "Referer: https://site/\r\nCookie: a=1\r\n")

    def test_only_network_protocols_can_be_opened(self):
        allowed = self.value(self.build(), "-protocol_whitelist").split(",")
        self.assertNotIn("file", allowed)

    def test_hls_options_are_not_given_to_other_inputs(self):
        self.assertIn("-allowed_extensions", self.build("hls"))
        self.assertNotIn("-allowed_extensions", self.build("mp4"))    # FFmpeg refuses an option the demuxer lacks

    def test_no_audio_output_for_a_silent_video(self):
        cmd = self.build(audio=False)
        self.assertNotIn("a.m3u8", cmd)
        self.assertIn("v.m3u8", cmd)


class FakeEncoder:
    """Stands in for FFmpeg: the test decides what it has 'made' by writing files into its folder."""
    made = []

    def __init__(self, cmd, cwd=None, **kw):
        self.cmd, self.cwd, self.rc = cmd, cwd, None
        FakeEncoder.made.append(self)

    def poll(self):
        return self.rc

    def terminate(self):
        self.rc = 1

    def wait(self, timeout=None):
        return self.rc

    def kill(self):
        self.rc = 1

    def piece(self, *numbers):
        for n in numbers:
            for name in (f"v{n:05d}.m4s", f"a{n:05d}.m4s"):
                with open(os.path.join(self.cwd, name), "wb") as f:
                    f.write(b"x")

    @property
    def first(self):
        return int(self.cmd[self.cmd.index("-start_number") + 1])


class Fixture(unittest.TestCase):
    def setUp(self):
        FakeEncoder.made = []
        for p in (mock.patch.object(upscaler.subprocess, "Popen", FakeEncoder),
                  mock.patch.object(upscaler.shutil, "which", lambda name: name)):
            p.start()
            self.addCleanup(p.stop)
        self.sessions = []

    def session(self, start=0.0, duration=4000, audio=True):
        s = UpscaleSession("https://cdn/x", {}, "hls", "tok", start, duration, "fast", audio)
        self.addCleanup(s.close)
        self.sessions.append(s)
        return s

    def enc(self):
        return FakeEncoder.made[-1]

    def wait_for(self, fn, timeout=3):
        end = time.time() + timeout
        while time.time() < end:
            if fn():
                return True
            time.sleep(0.02)
        return False


class Steering(Fixture):
    def test_starts_encoding_at_the_cut_straight_away(self):
        self.session(start=13)
        self.assertEqual(self.enc().first, 3)

    def test_a_piece_on_its_way_is_waited_for_without_restarting(self):
        s = self.session()
        got = []
        t = threading.Thread(target=lambda: got.append(s.file_for("v00001.m4s", timeout=3)))
        t.start()
        time.sleep(0.1)
        self.enc().piece(0, 1)
        t.join(4)
        self.assertTrue(got[0].endswith("v00001.m4s"))
        self.assertEqual(len(FakeEncoder.made), 1)

    def test_a_jump_restarts_the_encoder_there(self):
        s = self.session()
        old = self.enc()
        old.piece(0, 1)
        self.assertIsNone(s.file_for("v00100.m4s", timeout=0.05))
        self.assertEqual(self.enc().first, 100)
        self.assertEqual(old.rc, 1)

    def test_a_jump_back_to_a_piece_still_on_disk_needs_no_restart(self):
        s = self.session()
        self.enc().piece(0, 1, 2)
        self.assertTrue(s.file_for("v00001.m4s"))
        self.assertEqual(len(FakeEncoder.made), 1)

    def test_the_encoder_rests_when_far_ahead_of_the_tv(self):
        s = self.session()
        enc = self.enc()
        enc.piece(*range(upscaler.AHEAD + 2))
        self.assertTrue(self.wait_for(lambda: s.proc is None))
        self.assertEqual(enc.rc, 1)
        self.assertEqual(s.error, "")

    def test_it_wakes_again_before_the_tv_runs_out(self):
        s = self.session()
        self.enc().piece(*range(upscaler.AHEAD + 2))
        self.assertTrue(self.wait_for(lambda: s.proc is None))
        self.assertTrue(s.file_for("v00030.m4s"))                     # plenty left: stays asleep
        self.assertEqual(len(FakeEncoder.made), 1)
        self.assertTrue(s.file_for(f"v{upscaler.AHEAD - 3:05d}.m4s"))
        self.assertEqual(len(FakeEncoder.made), 2)
        self.assertEqual(self.enc().first, upscaler.AHEAD + 2)

    def test_pieces_behind_the_tv_are_deleted(self):
        s = self.session()
        self.enc().piece(*range(30))
        s.file_for("v00020.m4s")
        gone = os.path.join(self.enc().cwd, "v00002.m4s")
        kept = os.path.join(self.enc().cwd, "v00015.m4s")
        self.assertTrue(self.wait_for(lambda: not os.path.exists(gone)))
        self.assertTrue(os.path.exists(kept))

    def test_only_its_own_kinds_of_file_can_be_asked_for(self):
        s = self.session()
        with open(os.path.join(self.enc().cwd, "ffmpeg.log"), "ab"):
            pass
        self.assertIsNone(s.file_for("ffmpeg.log", timeout=0.05))
        self.assertIsNone(s.file_for("../shader.glsl", timeout=0.05))

    def test_a_piece_past_the_end_is_refused_at_once(self):
        s = self.session(duration=20)
        began = time.time()
        self.assertIsNone(s.file_for("v00900.m4s"))
        self.assertLess(time.time() - began, 1)


class Ending(Fixture):
    def test_an_encoder_that_makes_nothing_is_an_error_not_a_loop(self):
        s = self.session()
        with open(os.path.join(self.enc().cwd, "ffmpeg.log"), "w") as f:
            f.write("something\nvulkan: no device\n")
        self.enc().rc = 1
        self.assertFalse(s.wait_ready(timeout=3))
        self.assertIn("no device", s.error)
        self.assertIsNone(s.file_for("v00000.m4s", timeout=0.05))
        self.assertEqual(len(FakeEncoder.made), 1)

    def test_the_real_end_of_the_video_stops_further_asks(self):
        s = self.session(duration=40)
        self.enc().piece(*range(9))
        self.enc().rc = 0
        self.assertTrue(self.wait_for(lambda: s.eof == 9))
        self.assertIsNone(s.file_for("v00009.m4s"))                   # the listed length was a little generous
        self.assertEqual(len(FakeEncoder.made), 1)

    def test_a_clean_exit_midway_is_a_dropped_source_and_restarts(self):
        s = self.session()
        self.enc().piece(0, 1, 2)
        self.enc().rc = 0
        self.assertTrue(self.wait_for(lambda: s.proc is None))
        self.assertIsNone(s.eof)
        self.assertIsNone(s.file_for("v00003.m4s", timeout=0.05))
        self.assertEqual(self.enc().first, 3)

    def test_closing_stops_the_encoder_and_deletes_the_pieces(self):
        s = self.session()
        enc = self.enc()
        enc.piece(0)
        s.close()
        self.assertEqual(enc.rc, 1)
        self.assertFalse(os.path.exists(s.dir))

    def test_nothing_restarts_it_after_closing(self):
        s = self.session()
        s.close()
        self.assertIsNone(s.file_for("v00500.m4s", timeout=0.05))
        self.assertEqual(len(FakeEncoder.made), 1)


class ReadAhead(Fixture):
    """What the real TV did: audio comes out of FFmpeg ahead of the picture, and the TV reads it as fast as it can."""

    def audio(self, enc, *numbers):
        for n in numbers:
            with open(os.path.join(enc.cwd, f"a{n:05d}.m4s"), "wb") as f:
                f.write(b"x")

    def test_audio_running_ahead_of_the_picture_doesnt_restart_the_encoder(self):
        s = self.session()
        self.enc().piece(0, 1)
        self.audio(self.enc(), *range(2, 12))
        for n in range(2, 12):
            self.assertTrue(s.file_for(f"a{n:05d}.m4s", timeout=0.05))
        self.assertEqual(len(FakeEncoder.made), 1)

    def test_audio_reads_dont_count_as_where_the_tv_is(self):
        s = self.session()
        self.enc().piece(*range(4))
        self.audio(self.enc(), *range(4, 40))
        self.assertTrue(s.file_for("a00039.m4s", timeout=0.05))
        self.assertEqual(s.last, 0)
        time.sleep(0.4)                                               # or the picture the TV still needs gets tidied away
        self.assertTrue(os.path.exists(os.path.join(self.enc().cwd, "v00001.m4s")))

    def test_reading_a_little_past_the_encoder_is_waited_for(self):
        s = self.session()
        self.enc().piece(0, 1)
        self.assertIsNone(s.file_for(f"v{1 + upscaler.JUMP:05d}.m4s", timeout=0.05))
        self.assertEqual(len(FakeEncoder.made), 1)

    def test_audio_for_a_far_seek_still_moves_the_encoder(self):
        s = self.session()
        self.enc().piece(0, 1)
        self.assertIsNone(s.file_for("a00200.m4s", timeout=0.05))
        self.assertEqual(self.enc().first, 200)


class LongWatch(Fixture):
    """Watching past the point where the first pieces have been cleaned up, as any real cast does within a minute."""

    def watch(self, s, upto, ahead=4):
        for n in range(upto):
            self.enc().piece(n, n + ahead)
            for k in range(n, n + ahead):
                self.enc().piece(k)
            self.assertTrue(s.file_for(f"v{n:05d}.m4s", timeout=1), n)
            self.assertTrue(s.file_for(f"a{n:05d}.m4s", timeout=1), n)
            with s._cond:
                s._tidy()

    def test_cleaning_up_behind_the_tv_doesnt_restart_the_encoder(self):
        s = self.session()
        self.watch(s, 40)
        self.assertFalse(os.path.exists(os.path.join(self.enc().cwd, "v00000.m4s")))
        self.assertEqual(len(FakeEncoder.made), 1)

    def test_a_read_just_past_the_encoder_still_waits_after_cleaning_up(self):
        s = self.session()
        self.watch(s, 40)
        self.assertIsNone(s.file_for("v00046.m4s", timeout=0.05))
        self.assertEqual(len(FakeEncoder.made), 1)

    def test_a_real_seek_still_restarts_after_cleaning_up(self):
        s = self.session()
        self.watch(s, 40)
        self.assertIsNone(s.file_for("v00300.m4s", timeout=0.05))
        self.assertEqual(self.enc().first, 300)

    def test_reaching_the_end_after_cleaning_up_is_the_end_not_a_failure(self):
        s = self.session(duration=200)                                # 50 pieces
        self.watch(s, 45)
        self.enc().piece(*range(45, 49))
        self.enc().rc = 0
        self.assertTrue(self.wait_for(lambda: s.proc is None))
        self.assertEqual(s.error, "")
        self.assertEqual(s.eof, 49)

    def test_a_dropped_source_after_cleaning_up_isnt_a_failure_either(self):
        s = self.session()
        self.watch(s, 40)
        self.enc().rc = 1
        self.assertTrue(self.wait_for(lambda: s.proc is None))
        self.assertEqual(s.error, "")


class Pace(Fixture):
    def tick(self, s, head):
        with s._cond:
            s._time_pieces(head)

    def test_pieces_slower_than_the_video_are_counted_and_a_good_one_clears_them(self):
        s = self.session()
        clock = [1000.0]
        with mock.patch.object(upscaler.time, "time", lambda: clock[0]):
            self.tick(s, 1)
            for head, expect in ((2, 1), (3, 2), (4, 3)):
                clock[0] += upscaler.SEGMENT + 1
                self.tick(s, head)
                self.assertEqual(s.stalls, expect)
            clock[0] += 1
            self.tick(s, 5)
        self.assertEqual(s.stalls, 0)

    def test_a_tv_that_is_merely_waiting_isnt_a_slow_pc(self):
        s = self.session()
        clock = [1000.0]
        with mock.patch.object(upscaler.time, "time", lambda: clock[0]):
            self.tick(s, 1)
            for head in range(2, 12):
                clock[0] += upscaler.SEGMENT / upscaler.READ_RATE    # the pace the encoder is held to on purpose
                self.tick(s, head)
        self.assertEqual(s.stalls, 0)

    def test_slow_pieces_far_ahead_of_the_tv_dont_matter(self):
        s = self.session()
        s.last, clock = 0, [1000.0]
        with mock.patch.object(upscaler.time, "time", lambda: clock[0]):
            self.tick(s, upscaler.REFILL + 1)
            clock[0] += upscaler.SEGMENT * 3
            self.tick(s, upscaler.REFILL + 2)
        self.assertEqual(s.stalls, 0)

    def test_starting_up_isnt_counted(self):
        s = self.session()
        clock = [1000.0]
        with mock.patch.object(upscaler.time, "time", lambda: clock[0]):
            self.tick(s, 0)
            clock[0] += 30
            self.tick(s, 1)
        self.assertEqual(s.stalls, 0)

    def test_the_log_says_how_fast_the_tv_takes_the_stream(self):
        lines = []
        s = self.session()
        s.log = lines.append
        for _ in range(upscaler.SENT_EVERY):
            s.sent(8_000_000, 2.0)
        self.assertEqual(lines, ["upscale: stream needs 16.0 Mbit/s, TV took it at 32.0 Mbit/s"])


class Relay(Fixture):
    def setUp(self):
        super().setUp()
        self.relay = MediaRelay("127.0.0.1", allow=lambda: "10.9.9.9", allow_loopback=True)
        self.relay.start()
        self.addCleanup(self.relay.stop)

    def add(self, start=0.0):
        url = self.relay.add_upscaled("https://cdn/x", {"Referer": "https://site/"}, "hls", start, 40, "fast", True)
        return url, self.relay.session_for(url)

    def get(self, url):
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read()

    def test_the_tv_is_given_the_master_then_a_trimmed_playlist(self):
        url, sess = self.add(start=13)
        self.assertIn(b"video.m3u8", self.get(url)[1])
        self.assertIsNone(sess.actual_start)                          # the master doesn't cut anything
        body = self.get(url.replace("index.m3u8", "video.m3u8"))[1].decode()
        self.assertIn("v00003.m4s", body)
        self.assertNotIn("v00002.m4s", body)
        self.assertEqual(sess.actual_start, 12.0)

    def test_pieces_are_served_from_the_encoders_folder(self):
        url, sess = self.add()
        self.enc().piece(0)
        self.assertEqual(self.get(url.replace("index.m3u8", "v00000.m4s")), (200, b"x"))
        self.assertGreater(sess.hits, 0)

    def test_a_piece_that_cant_be_had_is_a_503(self):
        url, sess = self.add()
        with mock.patch.object(upscaler, "WAIT", 0.05), mock.patch.object(UpscaleSession.file_for, "__defaults__", (0.05,)):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.get(url.replace("index.m3u8", "v00000.m4s"))
        self.assertEqual(e.exception.code, 503)
        self.assertEqual(len(sess.failures), 1)

    def test_the_browsers_headers_reach_the_encoder(self):
        self.add()
        self.assertIn("Referer: https://site/", "".join(a for a in self.enc().cmd if "Referer" in a))

    def test_a_new_upscale_replaces_the_old_one(self):
        first_url, first = self.add()
        old = self.enc()
        self.add(start=20)
        self.assertEqual(old.rc, 1)
        self.assertTrue(first.closed)
        self.assertIsNone(self.relay.session_for(first_url))

    def test_stopping_the_relay_stops_the_encoder(self):
        self.add()
        enc = self.enc()
        self.relay.stop_upscaling()
        self.assertEqual(enc.rc, 1)


class Tab(unittest.TestCase):
    def setUp(self):
        self.notes = []
        tab = self.tab = web_tab.WebTab.__new__(web_tab.WebTab)
        tab.app = SimpleNamespace(after=lambda ms, fn: fn())
        tab.ui = SimpleNamespace(log=lambda m: None, AMBER="a", MUTED="m", GREEN="g", ACCENT="acc")
        tab._note = lambda text, *a: self.notes.append(text)
        tab.upscale, tab.upscaled, tab.attempt = "fast", None, 1
        for p in (mock.patch.object(upscaler, "missing", lambda: ""),):
            p.start()
            self.addCleanup(p.stop)

    def cand(self, **kw):
        base = dict(url="https://c/x.m3u8", kind="hls", duration=1400.0, height=1080, has_audio=True, has_video=True)
        return Candidate(**{**base, **kw})

    def test_off_means_off(self):
        self.tab.upscale = "off"
        self.assertIsNone(self.tab._upscale_plan(self.cand()))
        self.assertEqual(self.notes, [])

    def test_a_normal_video_gets_a_plan_with_no_extra_probe(self):
        with mock.patch.object(upscaler, "probe", side_effect=AssertionError("no need to probe")):
            plan = self.tab._upscale_plan(self.cand())
        self.assertEqual(plan, {"mode": "fast", "duration": 1400.0, "audio": True})

    def test_what_the_page_didnt_say_is_probed_for(self):
        info = {"duration": 90.0, "height": 720, "audio": False, "video": True}
        with mock.patch.object(upscaler, "probe", return_value=info):
            plan = self.tab._upscale_plan(self.cand(kind="mp4", duration=0.0, height=0, has_audio=None))
        self.assertEqual(plan, {"mode": "fast", "duration": 90.0, "audio": False})

    def test_skipping_always_says_why(self):
        for cand, words in ((self.cand(live=True), "Live"), (self.cand(height=2160), "already 4K"),
                            (self.cand(has_video=False), "no picture")):
            self.notes.clear()
            self.assertIsNone(self.tab._upscale_plan(cand))
            self.assertIn(words, self.notes[-1])

    def test_an_unknown_length_cant_be_laid_out_as_a_playlist(self):
        with mock.patch.object(upscaler, "probe", return_value=None):
            self.assertIsNone(self.tab._upscale_plan(self.cand(duration=0.0)))
        self.assertIn("length", self.notes[-1])

    def test_a_pc_that_cant_do_it_says_so(self):
        with mock.patch.object(upscaler, "missing", lambda: "needs FFmpeg"):
            self.assertIsNone(self.tab._upscale_plan(self.cand()))
        self.assertIn("needs FFmpeg", self.notes[-1])

    def relay_with(self, sess):
        return SimpleNamespace(add_upscaled=lambda *a: "http://r/s/1/index.m3u8", session_for=lambda u: sess,
                               stop_upscaling=mock.Mock())

    def test_a_failed_encoder_falls_back_to_a_plain_cast(self):
        sess = SimpleNamespace(wait_ready=lambda check: False, error="no vulkan")
        relay = self.relay_with(sess)
        url = self.tab._start_upscale(1, relay, self.cand(), 0.0, {"mode": "fast", "duration": 9, "audio": True})
        self.assertIsNone(url)
        self.assertIsNone(self.tab.upscaled)
        relay.stop_upscaling.assert_called()
        self.assertIn("as it is", self.notes[-1])

    def test_cancelling_during_the_warm_up_stops_the_encoder(self):
        def cancelled(check):
            self.tab.attempt = 2
            check()
        relay = self.relay_with(SimpleNamespace(wait_ready=cancelled, error=""))
        with self.assertRaises(web_tab.Cancelled):
            self.tab._start_upscale(1, relay, self.cand(), 0.0, {"mode": "fast", "duration": 9, "audio": True})
        relay.stop_upscaling.assert_called()

    def test_a_page_seek_keeps_the_cast_upscaled(self):
        plan = {"mode": "hq", "duration": 100.0, "audio": True}
        self.tab.upscaled = plan
        calls = []
        relay = SimpleNamespace(add_upscaled=lambda *a: calls.append(a) or "u", session_for=lambda u: "sess")
        self.tab._ensure_relay = lambda host: relay
        self.tab.app._host = "h"
        self.assertEqual(self.tab._relay_add("https://c/x.mp4", {}, "mp4", 30.0), ("u", "sess"))
        self.assertEqual(calls, [("https://c/x.mp4", {}, "mp4", 30.0, 100.0, "hq", True)])
        self.assertEqual(plan["session"], "sess")                     # the stall warning follows the new stream

    def test_what_the_label_steps_through_is_remembered(self):
        painted = []
        self.tab.upscale, self.tab.app._save = "off", lambda: painted.append(self.tab.upscale)
        self.tab.upscale_lbl = SimpleNamespace(config=lambda **k: painted.append(k["text"]))
        for _ in range(4):
            self.tab.next_upscale()
        self.assertEqual(painted, ["Upscale: Anime4K", "fast", "Upscale: Anime4K HQ", "hq", "Upscale: Movie", "movie",
                                   "Upscale: Off", "off"])

    def test_every_quality_on_the_label_has_a_bundled_shader(self):
        for mode in web_tab.UPSCALE_NAMES:
            if mode != "off":
                self.assertTrue(os.path.isfile(upscaler.shader_path(mode)), mode)

    def test_the_label_wont_turn_on_a_pc_that_cant_do_it(self):
        self.tab.upscale, self.tab.app._save = "off", lambda: None
        self.tab.upscale_lbl = SimpleNamespace(config=lambda **k: None)
        with mock.patch.object(upscaler, "missing", lambda: "needs FFmpeg"):
            self.tab.next_upscale()
        self.assertEqual(self.tab.upscale, "off")
        self.assertIn("needs FFmpeg", self.notes[-1])


class Following(unittest.TestCase):
    def test_a_scrub_of_an_upscaled_mp4_restarts_the_tv_instead_of_pressing_keys(self):
        added, keys, casts = [], [], []
        sess = SimpleNamespace(actual_start=60.0)
        tv = SimpleNamespace(state=lambda: {"state": "play", "pos": 10.0, "app": "RokuCast"}, key=keys.append,
                             cast=casts.append)
        guard = SimpleNamespace(epoch=0, video_time=lambda d: None, video_playing=lambda d: False)
        sync = WebSync(guard, tv, lambda *a: added.append(a) or ("url", sess))
        sync.cand, sync.kind, sync.live, sync.active = Candidate(url="u", kind="mp4"), "mp4", False, False
        sync.restartable, sync.offset, sync._seek_at = True, 0.0, None
        sync._seek(60.0, paused=False)
        self.assertEqual(casts, ["url"])
        self.assertEqual(keys, [])
        self.assertEqual(added[0][2], "mp4")
        self.assertEqual(sync.offset, 60.0)


if __name__ == "__main__":
    unittest.main()
