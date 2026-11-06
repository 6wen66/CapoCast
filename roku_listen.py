"""Play a Roku TV's private-listening audio on this PC (no phone needed).

Protocol (from the open-source RPListening client):
  1. WebSocket ws://<roku>:8060/ecp-session (subprotocol ecp-2), answer the auth challenge.
  2. Send set-audio-output pointing the Roku at <this pc>:RTP_PORT (UDP).
  3. Roku streams RTP/Opus (pt 97, 48 kHz stereo); we send RTCP back to <roku>:5150
     (VDLY, CVER, then receiver reports) so it keeps streaming.
  4. Forward the RTP to ffplay, which decodes it using a tiny SDP description.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import websockets

DEFAULT_HOST = "10.0.0.162"
ECP_PORT = 8060
RTP_PORT = 6970       # where the Roku sends audio; we also send RTCP from this port
RTCP_PORT = 5150      # Roku's RTCP port
FFPLAY_PORT = 5153    # local loopback port ffplay listens on
AUTH_SECRET = "95E610D0-7C29-44EF-FB0F-97F1FCE4C297"

SDP = (
    "v=0\no=- 0 0 IN IP4 127.0.0.1\ns=-\nc=IN IP4 127.0.0.1\n"
    f"m=audio {FFPLAY_PORT} RTP/AVP 97\na=rtpmap:97 opus/48000/2\n"
)


def _hash_char(c: str, shift: int) -> str:
    if "0" <= c <= "9":
        v = ord(c) - ord("0")
    elif "A" <= c <= "F":
        v = ord(c) - ord("A") + 10
    else:
        return c
    v = ((15 - v) + shift) & 15
    return chr(v + 48) if v < 10 else chr(v + 55)


def auth_response(challenge: str) -> str:
    salt = "".join(_hash_char(c, 9) for c in AUTH_SECRET)
    return base64.b64encode(hashlib.sha1((challenge + salt).encode()).digest()).decode()


def rtcp_app(name: bytes, value: int) -> bytes:
    return struct.pack(">BBHI4sI", 0x80, 204, 3, 0, name, value)


RTCP_RR = bytes.fromhex("81c9000700000000000000000000000000000000000000000000000000000000")


def local_ip_for(host: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, ECP_PORT))
        return s.getsockname()[0]
    finally:
        s.close()


def check_support(host: str) -> None:
    xml = urllib.request.urlopen(f"http://{host}:{ECP_PORT}/query/device-info", timeout=5).read().decode()
    if "<supports-private-listening>true<" not in xml:
        sys.exit("This Roku does not report private listening support.")
    if "<private-listening-blocked>true<" in xml:
        sys.exit("Private listening is blocked on this Roku (another device is using it?).")


def rtp_loop(sock: socket.socket, host: str, delay_us: int, stop: threading.Event) -> None:
    """Forward RTP to ffplay; start RTCP on the first packet and repeat receiver reports."""
    fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    roku = (host, RTCP_PORT)
    sock.settimeout(1.0)
    started, last_rr, count = False, 0.0, 0
    loop_time = time.monotonic
    while not stop.is_set():
        try:
            data, _ = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except ConnectionResetError:
            continue                       # Windows reports an earlier send to a closed port here; not a failure
        except OSError:
            break
        if len(data) < 12 or data[0] >> 6 != 2:
            continue
        fwd.sendto(data, ("127.0.0.1", FFPLAY_PORT))
        count += 1
        if not started:
            started = True
            print("Audio flowing.")
            sock.sendto(rtcp_app(b"VDLY", delay_us), roku)
            sock.sendto(rtcp_app(b"CVER", 808464434), roku)
            sock.sendto(RTCP_RR, roku)
            last_rr = loop_time()
        elif loop_time() - last_rr > 5:
            sock.sendto(RTCP_RR, roku)
            last_rr = loop_time()


async def session(host: str, my_ip: str) -> None:
    uri = f"ws://{host}:{ECP_PORT}/ecp-session"
    async with websockets.connect(
        uri, subprotocols=["ecp-2"], additional_headers={"Sec-WebSocket-Origin": "Android"}
    ) as ws:
        async for text in ws:
            msg = json.loads(text)
            if msg.get("notify") == "authenticate":
                await ws.send(json.dumps({
                    "request": "authenticate",
                    "request-id": "0",
                    "param-response": auth_response(msg["param-challenge"]),
                }))
            elif msg.get("response") == "authenticate":
                if msg.get("status") != "200":
                    sys.exit(f"Auth failed: {msg}")
                await ws.send(json.dumps({
                    "request": "set-audio-output",
                    "request-id": "1",
                    "param-audio-output": "datagram",
                    "param-devname": f"{my_ip}:{RTP_PORT}",
                }))
            elif msg.get("response") == "set-audio-output":
                if msg.get("status") != "200":
                    sys.exit(f"set-audio-output failed: {msg}")
                print("Roku accepted; waiting for audio (TV speakers should mute).")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default=DEFAULT_HOST, help="Roku IP")
    ap.add_argument("--delay-ms", type=int, default=500, help="audio delay requested from Roku (VDLY)")
    args = ap.parse_args()

    ffplay = shutil.which("ffplay")
    if not ffplay:
        sys.exit("ffplay not found on PATH (install ffmpeg).")
    check_support(args.host)
    my_ip = local_ip_for(args.host)
    print(f"Roku {args.host}  <-  this PC {my_ip}:{RTP_PORT}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", RTP_PORT))

    # ffplay on Windows can't read the SDP from stdin ("fd" protocol), so use a temp file.
    sdp_file = tempfile.NamedTemporaryFile("w", suffix=".sdp", delete=False)
    sdp_file.write(SDP)
    sdp_file.close()
    player = subprocess.Popen(
        [ffplay, "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "file,udp,rtp",
         "-fflags", "nobuffer", "-flags", "low_delay", "-vn", "-nodisp", "-nostats", "-i", sdp_file.name],
    )

    stop = threading.Event()
    t = threading.Thread(target=rtp_loop, args=(sock, args.host, args.delay_ms * 1000, stop), daemon=True)
    t.start()
    try:
        asyncio.run(session(args.host, my_ip))
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        sock.close()
        player.terminate()
        os.unlink(sdp_file.name)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if getattr(sys, "frozen", False) and e.code not in (None, 0):
            print(e.code)
            input("Press Enter to close...")
        raise
    except Exception as e:  # keep the console window open so the error can be read
        if getattr(sys, "frozen", False):
            print(f"Error: {e}")
            input("Press Enter to close...")
        raise
