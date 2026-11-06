"""End-to-end check of the upscaler with the real FFmpeg, GPU and relay, on a tiny clip so the GPU barely notices.

    python tests/upscale_smoke.py
    python tests/upscale_smoke.py --speed     how fast this PC really is at full size. Works the GPU flat out for
                                              about a minute, so don't run it during a game

A stand-in site serves an HLS clip only to requests with the right Referer. This fetches what the TV would through
the relay and looks inside. No window opens, nothing is cast to the TV, and it takes about 20 s. Prints PASS/FAIL per
check. Needs an NVIDIA GPU and an FFmpeg with libplacebo.
"""
import functools
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import upscaler  # noqa: E402
from media_relay import MediaRelay  # noqa: E402

REFERER = "https://anime.example/"
LENGTH = 72
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
failed = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail != "" else ""))
    if not ok:
        failed.append(name)


class Site(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get("Referer") != REFERER:
            return self.send_error(403)
        super().do_GET()


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass                                         # FFmpeg hanging up part-way is normal


def make_clip(folder):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=s=320x180:r=24:d={LENGTH}",
                    "-f", "lavfi", "-i", f"sine=f=440:d={LENGTH}", "-c:v", "libx264", "-g", "48", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", "-f", "hls", "-hls_time", "4", "-hls_playlist_type", "vod",
                    os.path.join(folder, "clip.m3u8")], check=True, creationflags=NO_WINDOW)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", os.path.join(folder, "clip.m3u8"), "-c", "copy",
                    "-movflags", "+faststart", os.path.join(folder, "clip.mp4")], check=True, creationflags=NO_WINDOW)
    # audio that begins after the picture, as a real site's did
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=24:d=16",
                    "-itsoffset", "1.6", "-f", "lavfi", "-i", "sine=f=440:d=16", "-map", "0:v", "-map", "1:a",
                    "-c:v", "libx264", "-g", "48", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", "-f", "hls",
                    "-hls_time", "4", "-hls_playlist_type", "vod", os.path.join(folder, "late.m3u8")], check=True,
                   creationflags=NO_WINDOW)
    # the way many anime sites serve it: AES-128, with the segments dressed up as pictures
    with open(os.path.join(folder, "k.bin"), "wb") as f:
        f.write(bytes(range(16)))
    info = os.path.join(folder, "k.info")
    with open(info, "w") as f:
        f.write("k.bin\n" + os.path.join(folder, "k.bin") + "\n")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", os.path.join(folder, "clip.mp4"), "-c", "copy", "-f", "hls",
                    "-hls_time", "4", "-hls_playlist_type", "vod", "-hls_key_info_file", info, "-hls_segment_filename",
                    os.path.join(folder, "pic%d.jpg"), os.path.join(folder, "locked.m3u8")], check=True,
                   creationflags=NO_WINDOW)


def fetch(url):
    with urllib.request.urlopen(url, timeout=40) as r:
        return r.read()


def look(init, piece, folder, name):
    """Join an init segment and a piece, as the TV does, and report (first timestamp, stream facts)."""
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(init + piece)
    run = lambda *a: subprocess.run(["ffprobe", "-v", "error", *a, path], capture_output=True, text=True,
                                    creationflags=NO_WINDOW).stdout
    first = run("-show_entries", "packet=pts_time", "-of", "csv=p=0").split()
    facts = run("-show_entries", "stream=codec_name,codec_tag_string,profile,width,height,pix_fmt", "-of", "csv=p=0")
    return (float(first[0].strip(",")) if first else None), facts.strip()


def main():
    why = upscaler.missing()
    if why:
        sys.exit(why)
    work = tempfile.mkdtemp(prefix="upscale-smoke-")
    site_dir = os.path.join(work, "site")
    os.mkdir(site_dir)
    make_clip(site_dir)
    site = QuietServer(("127.0.0.1", 0), functools.partial(Site, directory=site_dir))
    threading.Thread(target=site.serve_forever, daemon=True).start()
    source = f"http://127.0.0.1:{site.server_address[1]}/clip.m3u8"
    relay = MediaRelay("127.0.0.1", allow=lambda: "10.9.9.9", log=print, allow_loopback=True)
    relay.start()
    try:
        info = upscaler.probe(source, {"Referer": REFERER}, "hls")
        check("probe reads the protected stream", bool(info) and info["audio"] and info["video"], info)

        url = relay.add_upscaled(source, {"Referer": REFERER}, "hls", 0, LENGTH, "fast", True)
        sess = relay.session_for(url)
        began = time.time()
        ready = sess.wait_ready()
        check("first piece arrives", ready, f"{time.time() - began:.1f}s {sess.error}")
        if not ready:
            return
        at = lambda name: url.replace("index.m3u8", name)
        master = fetch(url).decode()
        check("master lists separate audio", 'TYPE=AUDIO' in master and "video.m3u8" in master)
        video = fetch(at("video.m3u8")).decode()
        check("playlist covers the whole clip", video.count("#EXTINF") == LENGTH // 4, video.count("#EXTINF"))
        v_init = fetch(at("v_init.mp4"))
        t, facts = look(v_init, fetch(at("v00001.m4s")), work, "v1.mp4")
        check("video is 4K 8-bit HEVC tagged hvc1", "hevc,Main,hvc1,3840,2160,yuv420p" in facts.replace(" ", ""), facts)
        check("piece 1 starts at 4 s", t is not None and abs(t - 4) < 0.1, t)
        t, facts = look(fetch(at("a_init.mp4")), fetch(at("a00001.m4s")), work, "a1.mp4")
        check("audio is AAC on the same timeline", "aac" in facts and t is not None and abs(t - 4) < 0.1, f"{facts} {t}")

        t, _ = look(v_init, fetch(at("v00006.m4s")), work, "v6.mp4")
        check("a jump lands on the video's timeline", t is not None and abs(t - 24) < 0.1, t)
        check("a restarted encoder's init segment matches the one the TV has", fetch(at("v_init.mp4")) == v_init)

        url = relay.add_upscaled(source, {"Referer": REFERER}, "hls", 13, LENGTH, "hq", True)
        seek = relay.session_for(url)
        check("HQ shaders run", seek.wait_ready(), seek.error)
        trimmed = fetch(url.replace("index.m3u8", "video.m3u8")).decode()
        check("a page seek is a trimmed playlist", "v00002.m4s" not in trimmed and seek.actual_start == 12.0,
              seek.actual_start)
        t, _ = look(fetch(url.replace("index.m3u8", "v_init.mp4")), fetch(url.replace("index.m3u8", "v00003.m4s")),
                    work, "s3.mp4")
        check("and starts where it says", t is not None and abs(t - 12) < 0.1, t)
        check("the replaced cast cleaned up", not os.path.exists(sess.dir))

        proc = seek.proc
        relay.stop_upscaling()
        check("stopping ends the encoder and clears its files",
              (proc is None or proc.poll() is not None) and not os.path.exists(seek.dir))

        url = relay.add_upscaled(source.replace(".m3u8", ".mp4"), {"Referer": REFERER}, "mp4", 8, LENGTH, "fast", True)
        mp4 = relay.session_for(url)
        check("an MP4 source works too, from any point", mp4.wait_ready(), mp4.error)
        t, _ = look(fetch(url.replace("index.m3u8", "v_init.mp4")), fetch(url.replace("index.m3u8", "v00002.m4s")),
                    work, "m2.mp4")
        check("and is on the timeline", t is not None and abs(t - 8) < 0.1, t)

        # the real TV's habit: race through the audio, and keep reading well past the point where the first pieces
        # have been cleaned up. Two different bugs each made this restart the encoder over and over.
        url = relay.add_upscaled(source, {"Referer": REFERER}, "hls", 0, LENGTH, "fast", True)
        ahead = relay.session_for(url)
        ahead.wait_ready()
        for n in range(LENGTH // 4 - 1):
            fetch(url.replace("index.m3u8", f"a{n:05d}.m4s"))
            fetch(url.replace("index.m3u8", f"v{n:05d}.m4s"))
            time.sleep(0.3)                          # let the clean-up run, as it does between a real TV's reads
        check("a long watch runs on one encoder", len(ahead.run_dirs) == 1, len(ahead.run_dirs))
        check("with the early pieces cleaned up", ahead._find("v00000.m4s") is None)
        check("and no failure", ahead.error == "", ahead.error)

        url = relay.add_upscaled(source, {"Referer": REFERER}, "hls", 0, LENGTH, "movie", True)
        film = relay.session_for(url)
        check("the Movie shader runs", film.wait_ready(), film.error)
        t, facts = look(fetch(url.replace("index.m3u8", "v_init.mp4")), fetch(url.replace("index.m3u8", "v00001.m4s")),
                        work, "f1.mp4")
        check("and makes the same 4K stream", "3840" in facts and t is not None and abs(t - 4) < 0.1, f"{facts} {t}")

        url = relay.add_upscaled(source.replace("clip", "late"), {"Referer": REFERER}, "hls", 0, 16, "fast", True)
        late = relay.session_for(url)
        late.wait_ready()
        t, _ = look(fetch(url.replace("index.m3u8", "a_init.mp4")), fetch(url.replace("index.m3u8", "a00001.m4s")),
                    work, "l1.mp4")
        check("audio that starts late stays on the playlist's timeline", t is not None and abs(t - 4) < 0.1, t)

        locked = source.replace("clip", "locked")
        info = upscaler.probe(locked, {"Referer": REFERER}, "hls")
        check("probe reads an encrypted stream in disguise", bool(info) and info["video"], info)
        url = relay.add_upscaled(locked, {"Referer": REFERER}, "hls", 0, LENGTH, "fast", True)
        enc = relay.session_for(url)
        check("and it upscales", enc.wait_ready(), enc.error)
        t, facts = look(fetch(url.replace("index.m3u8", "v_init.mp4")), fetch(url.replace("index.m3u8", "v00001.m4s")),
                        work, "e1.mp4")
        check("to real pictures", "3840" in facts and t is not None and abs(t - 4) < 0.1, f"{facts} {t}")

        url = relay.add_upscaled(source, {"Referer": "https://wrong.example/"}, "hls", 0, LENGTH, "fast", True)
        bad = relay.session_for(url)
        check("a refused source is reported, not hung on", not bad.wait_ready(timeout=30) and bool(bad.error), bad.error)
    finally:
        relay.stop()
        site.shutdown()
        shutil.rmtree(work, ignore_errors=True)


def speed(frames=240):
    """Full-size frames through the same filter and encoder as a real cast, for each source size and quality."""
    why = upscaler.missing()
    if why:
        sys.exit(why)
    work = tempfile.mkdtemp(prefix="upscale-speed-")
    try:
        for mode in upscaler.MODES:
            shutil.copyfile(upscaler.shader_path(mode), os.path.join(work, "shader.glsl"))
            run = os.path.join(work, "run")
            os.makedirs(run, exist_ok=True)
            for height, width in ((720, 1280), (1080, 1920)):
                cmd = upscaler.command("ffmpeg", "x", {}, "mp4", 0, False)
                picture = cmd[cmd.index("-vf") + 1]
                encoder = cmd[cmd.index("-c:v"):cmd.index("-force_key_frames")]
                began = time.time()
                subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", *cmd[5:9], "-f", "lavfi", "-i",
                                f"testsrc2=s={width}x{height}:r=24", "-frames:v", str(frames), "-vf", picture, *encoder,
                                "-f", "null", "-"], cwd=run, check=True, creationflags=NO_WINDOW)
                fps = frames / (time.time() - began)
                verdict = "keeps up with 24 fps" if fps > 24 * upscaler.READ_RATE else "TOO SLOW for 24 fps"
                print(f"{mode:6}{height}p -> 4K: {fps:5.1f} fps, startup included  ({verdict})")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    if "--speed" in sys.argv:
        sys.exit(speed())
    main()
    print("FAILED: " + ", ".join(failed) if failed else "ALL PASSED")
    sys.exit(1 if failed else 0)
