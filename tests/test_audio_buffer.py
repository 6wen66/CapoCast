"""Concealing lost RTP packets and riding out the TV's Wi-Fi pauses."""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import roku_listen_gui as g  # noqa: E402


def pkt(seq, ts, body=b"\x78abcdef"):
    return bytes([0x80, 0x61]) + (seq & 0xFFFF).to_bytes(2, "big") + (ts & 0xFFFFFFFF).to_bytes(4, "big") + bytes(4) + body


class Fillers(unittest.TestCase):
    def test_nothing_is_added_when_nothing_is_missing(self):
        self.assertEqual(g.filler_packets(pkt(5, 480), pkt(6, 960)), [])

    def test_each_lost_packet_gets_a_stand_in_with_its_own_number_and_time(self):
        out = g.filler_packets(pkt(10, 4800), pkt(14, 4800 + 4 * 480))
        self.assertEqual([int.from_bytes(f[2:4], "big") for f in out], [11, 12, 13])
        self.assertEqual([int.from_bytes(f[4:8], "big") for f in out], [4800 + 480, 4800 + 960, 4800 + 1440])
        self.assertTrue(all(f[12:] == b"\x78" and f[:2] == b"\x80\x61" and f[8:12] == bytes(4) for f in out))

    def test_the_toc_byte_asks_for_an_empty_frame(self):
        out = g.filler_packets(pkt(1, 0, b"\x7b\x01\x02"), pkt(3, 960))
        self.assertEqual(out[0][12] & 0x03, 0)                   # code 0: one frame, and that frame is empty

    def test_numbers_and_times_wrap_around(self):
        out = g.filler_packets(pkt(0xFFFF, 0xFFFFFF00), pkt(2, 0xFFFFFF00 + 3 * 480))
        self.assertEqual([int.from_bytes(f[2:4], "big") for f in out], [0, 1])
        self.assertEqual([int.from_bytes(f[4:8], "big") for f in out], [(0xFFFFFF00 + 480) & 0xFFFFFFFF, (0xFFFFFF00 + 960) & 0xFFFFFFFF])

    def test_late_duplicate_or_huge_gaps_and_odd_timestamps_are_left_alone(self):
        self.assertEqual(g.filler_packets(pkt(9, 4320), pkt(8, 3840)), [])           # late
        self.assertEqual(g.filler_packets(pkt(9, 4320), pkt(9, 4320)), [])           # duplicate
        self.assertEqual(g.filler_packets(pkt(1, 0), pkt(1 + g.MAX_FILL + 5, 480 * (g.MAX_FILL + 5))), [])   # an outage
        self.assertEqual(g.filler_packets(pkt(1, 0, b""), pkt(3, 960)), [])          # nothing to copy a header byte from


    def test_timestamps_that_do_not_divide_evenly_still_get_fillers(self):
        """The Roku's real timestamps often don't (only about half its gaps were being filled before this)."""
        out = g.filler_packets(pkt(1, 0), pkt(4, 1000))
        self.assertEqual([int.from_bytes(f[2:4], "big") for f in out], [2, 3])
        ts = [int.from_bytes(f[4:8], "big") for f in out]
        self.assertEqual(ts, [333, 666])
        self.assertTrue(0 < ts[0] < ts[1] < 1000)

    def test_a_timestamp_that_stood_still_or_went_backwards_falls_back_to_ten_milliseconds(self):
        for later in (0, 0xFFFFFF00):
            out = g.filler_packets(pkt(1, 5000), pkt(3, later))
            self.assertEqual([int.from_bytes(f[4:8], "big") for f in out], [5480])


class Decoder(unittest.TestCase):
    def test_libopus_is_requested_only_when_asked_for(self):
        with_it = g.decoder_command("ff", "x.sdp", "libopus")
        self.assertEqual(with_it[with_it.index("-c:a") + 1], "libopus")
        self.assertLess(with_it.index("-c:a"), with_it.index("-i"))
        self.assertNotIn("-c:a", g.decoder_command("ff", "x.sdp", ""))

    def test_reports_count_what_was_filled_in(self):
        st = g.RtpStats()
        st.concealed = 6
        self.assertIn("6 concealed", st.report())
        self.assertEqual(st.concealed, 0)


class Reader:
    """Stands in for FFmpeg's output: 20 ms chunks in real time. A stall holds everything back, then what piled up
    behind it arrives at once, which is what the TV's Wi-Fi does (about 250 ms late, about 56 ms of it lost for good)."""

    def __init__(self, stalls=(), total=None):
        self.stalls, self.t0, self.n, self.total = list(stalls), time.monotonic(), 0, total

    def __call__(self, size):
        if self.total is not None and self.n >= self.total:
            return b""
        for at, length in list(self.stalls):
            if self.n * 0.02 >= at:
                self.stalls.remove((at, length))
                time.sleep(length)
        time.sleep(max(0.0, self.t0 + self.n * 0.02 - time.monotonic()))     # already due after a stall: no wait
        self.n += 1
        return b"" * size


class BufferBehaviour(unittest.TestCase):
    def play(self, buf, seconds):
        end, got = time.monotonic() + seconds, 0
        while time.monotonic() < end:
            if buf.get(0.05) is not None:
                got += 1
                time.sleep(0.02)
        return got

    def test_it_waits_until_enough_is_queued_before_playing(self):
        stop = threading.Event()
        buf = g.AudioBuffer(Reader(), 300, stop)
        self.addCleanup(stop.set)
        t0 = time.monotonic()
        while buf.get(0.05) is None:
            pass
        self.assertGreater(time.monotonic() - t0, 0.2)           # not until about 300 ms had arrived

    def test_the_measured_pauses_are_covered(self):
        """A quarter-second pause every 10 s (measured on this TV), squeezed to every 1.2 s."""
        stop = threading.Event()
        buf = g.AudioBuffer(Reader(stalls=[(1.0, 0.25), (2.0, 0.25), (3.0, 0.25)]), 300, stop)
        self.addCleanup(stop.set)
        self.play(buf, 4.5)
        self.assertEqual(buf.underruns, 0)

    def test_without_a_buffer_the_same_pauses_are_heard(self):
        stop = threading.Event()
        buf = g.AudioBuffer(Reader(stalls=[(1.0, 0.25), (2.0, 0.25)]), 0, stop)
        self.addCleanup(stop.set)
        self.play(buf, 3.5)
        self.assertGreaterEqual(buf.underruns, 2)

    def test_a_pause_longer_than_the_buffer_is_counted_and_playing_resumes(self):
        stop = threading.Event()
        buf = g.AudioBuffer(Reader(stalls=[(1.0, 0.8)]), 300, stop)
        self.addCleanup(stop.set)
        got = self.play(buf, 3.0)
        self.assertEqual(buf.underruns, 1)
        self.assertGreater(got, 60)

    def test_a_sound_card_slower_than_the_stream_does_not_pile_up_delay(self):
        stop = threading.Event()
        buf = g.AudioBuffer(Reader(), 300, stop, spill_ms=100)
        self.addCleanup(stop.set)
        time.sleep(1.0)                                          # nobody is playing: the stream keeps arriving
        self.assertLessEqual(buf.level(), 0.42)
        self.assertGreater(buf.dropped, 20)

    def test_the_end_of_the_stream_is_reported_after_what_is_left_has_played(self):
        stop = threading.Event()
        buf = g.AudioBuffer(Reader(total=5), 300, stop)
        self.addCleanup(stop.set)
        got = 0
        while not buf.finished() or buf.q:
            if buf.get(0.05) is not None:
                got += 1
        self.assertEqual(got, 5)
        self.assertTrue(buf.finished())
        self.assertIsNone(buf.get(0.05))


class LiveSyncAdjust(unittest.TestCase):
    def test_the_roku_is_asked_for_exactly_the_delay_box_and_the_local_buffer_is_not_added_to_it(self):
        ln = g.Listener("10.0.0.5", 500, lambda *a, **k: None, 800)
        self.assertEqual(ln.delay_us, 500_000)
        self.assertEqual(ln.buffer_ms, 800)

    def test_more_delay_goes_quiet_for_the_difference_without_counting_an_underrun(self):
        stop = threading.Event()
        buf = g.AudioBuffer(Reader(), 300, stop)
        self.addCleanup(stop.set)
        while buf.get(0.05) is None:
            pass
        self.assertTrue(buf.primed)
        buf.retarget(0.8)                                       # the sound should now be 500 ms later
        t0, quiet = time.monotonic(), 0.0
        while buf.get(0.02) is None and time.monotonic() - t0 < 2:
            quiet = time.monotonic() - t0
        self.assertGreater(quiet, 0.25)                         # it waited for the extra audio to arrive
        self.assertEqual(buf.underruns, 0)

    def test_less_delay_skips_ahead_at_once(self):
        stop = threading.Event()
        buf = g.AudioBuffer(Reader(), 800, stop, spill_ms=100)
        self.addCleanup(stop.set)
        while buf.get(0.05) is None:
            pass
        time.sleep(0.3)
        before = buf.level()
        buf.retarget(0.2)
        self.assertLess(buf.level(), before - 0.3)              # about 600 ms of queued sound was dropped
        self.assertLessEqual(buf.level(), 0.3 + 0.02)
        self.assertGreater(buf.dropped, 20)

    def test_the_listener_passes_the_slider_on_whole(self):
        """The cushion that covers a pause is the queue itself: it must not shrink by the sound card's own latency (it
        did, and the queue then sat at about 180 ms, under the TV's 250 ms pauses)."""
        ln = g.Listener("10.0.0.5", 500, lambda *a, **k: None, 300)
        ln.buffer, ln.out_latency = mock.Mock(), 0.04
        ln.set_buffer(500)
        ln.buffer.retarget.assert_called_once_with(0.5)
        self.assertEqual(ln.buffer_ms, 500)
        ln.buffer = None
        ln.set_buffer(100)                                      # before the audio loop has started: nothing to retarget
        self.assertEqual(ln.buffer_ms, 100)


if __name__ == "__main__":
    unittest.main()
