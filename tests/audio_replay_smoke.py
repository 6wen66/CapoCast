"""The measured TV Wi-Fi pattern, replayed through the real Listener and a real FFmpeg, old behaviour against new.

Headless: private ports (never the live 6970 / 5153 a running CapoCast uses), no websocket, no sound card (writes are
captured). Every 2 s the "TV" goes quiet for 250 ms, loses the packets of the first 56 ms of it, and delivers the rest
in a burst. Reports, for each setup, how many times the sound card would have been left with nothing to play, and how
much of the audio is missing or silent. Needs ffmpeg with libopus and NVENC-free. Run: python tests/audio_replay_smoke.py
"""
import array
import math
import os
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import roku_listen_gui as g  # noqa: E402
from opus_conceal_smoke import capture_packets, free_port  # noqa: E402

SECONDS = 8


def stall_plan(packets):
    """[(send_time, packet)] for a 10 ms stream with a 250 ms pause every 2 s (the real TV does it every 10 s)."""
    plan = []
    for i, d in enumerate(packets):
        t = i * 0.01
        phase = t % 2.0
        if 1.0 <= phase < 1.25:
            if phase < 1.056:
                continue                                 # lost for good
            t = t - phase + 1.25                         # held back, delivered when the pause ends
        plan.append((t, d))
    return sorted(plan, key=lambda x: x[0])


def run(packets, buffer_ms, conceal):
    rtp, ff = free_port(), free_port()
    writes = []

    class Out:
        latency = 0.0

        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def write(self, buf):
            a = array.array("h")
            a.frombytes(buf)
            rms = math.sqrt(sum(x * x for x in a[::8]) / max(1, len(a[::8])))
            writes.append((time.monotonic(), rms))
            time.sleep(len(buf) / 192000)               # the card plays in real time

    sdp = g.SDP.replace(str(g.FFPLAY_PORT), str(ff))     # before the ports are swapped, or there is nothing left to replace
    assert str(ff) in sdp and str(g.FFPLAY_PORT) not in sdp
    with mock.patch.object(g, "RTP_PORT", rtp), mock.patch.object(g, "FFPLAY_PORT", ff), \
            mock.patch.object(g, "SDP", sdp), \
            mock.patch.object(g, "check_support"), mock.patch.object(g, "local_ip_for", return_value="127.0.0.1"), \
            mock.patch.object(g, "RTCP_PORT", free_port()), \
            mock.patch.object(g.Listener, "_ws_thread", lambda self: None), \
            mock.patch.object(g.sd, "RawOutputStream", Out), \
            mock.patch.object(g, "has_libopus", return_value=conceal), \
            mock.patch.object(g, "LOG", os.path.join(tempfile.gettempdir(), "audio_replay.log")):
        ln = g.Listener("127.0.0.1", 0, lambda *a, **k: None, buffer_ms)
        ln.start()
        time.sleep(1.0)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        t0 = time.monotonic()
        for t, d in stall_plan(packets):
            time.sleep(max(0.0, t0 + t - time.monotonic()))
            s.sendto(d, ("127.0.0.1", rtp))
        time.sleep(1.5)
        ln.stop()
        under = ln.buffer.underruns if ln.buffer else 0
        concealed = ln.stats.concealed if ln.stats else 0
    return writes, under, concealed


def analyse(writes, under, concealed, name):
    if not writes:
        print(f"{name:34s} nothing played")
        return
    gaps = [b[0] - a[0] for a, b in zip(writes, writes[1:])]
    starved = sum(1 for x in gaps if x > 0.045)             # a 20 ms chunk is due every 20 ms
    worst = max(gaps) * 1000
    quiet = sum(1 for _, r in writes if r < 300) * 20
    print(f"{name:34s} played {len(writes) * 20 / 1000:5.2f} s  starved {starved:2d}x (worst {worst:4.0f} ms)  "
          f"quiet {quiet:4d} ms  underruns {under}  concealed {concealed}")


def main():
    packets = capture_packets()
    print(f"{len(packets)} packets of tone; the pattern repeats every 2 s with a 250 ms pause and 56 ms lost")
    analyse(*run(packets, 0, False), "before (no buffer, no concealment)")
    analyse(*run(packets, 0, True), "concealment only")
    analyse(*run(packets, 300, True), "after (300 ms buffer + concealment)")


if __name__ == "__main__":
    main()
