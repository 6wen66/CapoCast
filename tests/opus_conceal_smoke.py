"""Does a TOC-only RTP packet make FFmpeg's libopus decoder conceal a lost frame? Headless, needs ffmpeg with libopus.

Encodes a steady tone to 10 ms Opus RTP (what the Roku sends), loses 6 packets every second, plays the rest into the same
FFmpeg command the Listener uses, and measures the result three ways: nothing lost, lost, lost but filled in.
Run: python tests/opus_conceal_smoke.py
"""
import array
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import roku_listen_gui as g  # noqa: E402

FF = shutil.which("ffmpeg")
SECONDS, FREQ = 6, 440


def capture_packets():
    """A tone as RTP/Opus packets, from FFmpeg's own packetiser."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 22)
    s.settimeout(3)
    port = s.getsockname()[1]
    p = subprocess.Popen([FF, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                          f"sine=frequency={FREQ}:sample_rate=48000:duration={SECONDS}", "-ac", "2", "-c:a", "libopus",
                          "-b:a", "128k", "-frame_duration", "10", "-application", "audio", "-payload_type", "97",
                          "-f", "rtp", f"rtp://127.0.0.1:{port}?rtcpport={port + 1}"], stdout=subprocess.DEVNULL)
    out = []
    try:
        while True:
            d = s.recv(2048)
            if len(d) >= 12 and d[1] & 0x7F == 97:
                out.append(d)
    except socket.timeout:
        pass
    p.wait(5)
    return out


def free_port():
    """A UDP port nobody is using. Never g.FFPLAY_PORT: a running CapoCast listens there, and test packets sent to it
    would play in the user's headphones."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def play(packets, codec):
    """Send packets at real speed-ish into the Listener's FFmpeg command and return the decoded mono samples."""
    port = free_port()
    with tempfile.NamedTemporaryFile("w", suffix=".sdp", delete=False) as f:
        f.write(g.SDP.replace(str(g.FFPLAY_PORT), str(port)))
    cmd = g.decoder_command(FF, f.name, codec)
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    pcm = []
    t = threading.Thread(target=lambda: pcm.append(p.stdout.read()), daemon=True)
    t.start()
    time.sleep(1.0)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for i, d in enumerate(packets):
        s.sendto(d, ("127.0.0.1", port))
        if i % 10 == 9:
            time.sleep(0.02)
    time.sleep(1.0)
    p.terminate()
    t.join(5)
    os.unlink(f.name)
    a = array.array("h")
    a.frombytes(pcm[0][:len(pcm[0]) // 4 * 4])
    return a[0::2]


def quiet_ms(samples):
    """How much of the middle of the tone is silent, in 5 ms windows."""
    n, quiet = 240, 0
    body = samples[4800:-4800]
    for i in range(0, len(body) - n, n):
        if math.sqrt(sum(x * x for x in body[i:i + n]) / n) < 300:
            quiet += 5
    return quiet


def main():
    pk = capture_packets()
    print(f"{len(pk)} packets, step {int.from_bytes(pk[2][4:8], 'big') - int.from_bytes(pk[1][4:8], 'big')}, "
          f"sizes {min(map(len, pk))}-{max(map(len, pk))}")
    lost, filled = [], []
    prev, dropped = None, 0
    for i, d in enumerate(pk):
        if i > 20 and i % 100 < 6:
            dropped += 1
            continue
        if prev is not None:
            filled.extend(g.filler_packets(prev, d))
        filled.append(d)
        lost.append(d)
        prev = d
    print(f"dropped {dropped} packets = {dropped * 10} ms")
    for name, packets, codec in (("clean", pk, "libopus"), ("lost", lost, "libopus"), ("filled", filled, "libopus"),
                                 ("filled, native decoder", filled, "")):
        s = play(packets, codec)
        print(f"{name:24s} {len(s) / 48:7.0f} ms decoded, {quiet_ms(s):5d} ms quiet")


if __name__ == "__main__":
    main()
