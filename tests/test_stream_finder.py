import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream_finder import (Candidate, apply_page_videos, choose, classify, is_ad_url, parse_hls,  # noqa: E402
                           parse_mpd, reject_reason, site_of, sniff)

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080
high/index.m3u8
"""
MEDIA = "#EXTM3U\n#EXT-X-TARGETDURATION:10\n" + "".join(f"#EXTINF:10.0,\nseg{i}.ts\n" for i in range(12)) + "#EXT-X-ENDLIST\n"


class Classify(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(classify("https://x.com/a/master.m3u8?t=1"), "hls")
        self.assertEqual(classify("https://x.com/a", "application/vnd.apple.mpegurl"), "hls")
        self.assertEqual(classify("https://x.com/movie.mp4"), "mp4")
        self.assertEqual(classify("https://x.com/stream", "video/mp4"), "mp4")

    def test_pieces_are_not_videos(self):
        for u in ("https://x.com/seg1.ts", "https://x.com/a.m4s", "https://x.com/t.jpg", "https://x.com/s.vtt"):
            self.assertIsNone(classify(u, "video/mp4"), u)

    def test_dash_is_supported(self):
        self.assertEqual(classify("https://x.com/a.mpd"), "dash")
        self.assertEqual(classify("https://x.com/a", "application/dash+xml"), "dash")

    def test_unsupported(self):
        self.assertEqual(classify("https://x.com/a.mkv"), "unsupported")
        self.assertEqual(classify("https://x.com/a", "video/webm"), "unsupported")

    def test_sniffing_finds_disguised_playlists(self):
        self.assertEqual(sniff("#EXTM3U\n#EXT-X-VERSION:3\n"), "hls")
        self.assertEqual(sniff("﻿  #EXTM3U\n"), "hls")
        self.assertEqual(sniff('<?xml version="1.0"?>\n<MPD type="static">'), "dash")
        self.assertIsNone(sniff("<html><body>hi</body></html>"))
        self.assertIsNone(sniff('{"url": "x"}'))


class Sites(unittest.TestCase):
    def test_site_of(self):
        self.assertEqual(site_of("v.cdn.example.com"), "example.com")
        self.assertEqual(site_of("a.b.example.co.uk"), "example.co.uk")
        self.assertEqual(site_of("127.0.0.1"), "127.0.0.1")

    def test_ads(self):
        self.assertTrue(is_ad_url("https://pubads.g.doubleclick.net/x.mp4"))
        self.assertTrue(is_ad_url("https://cdn.site.com/ads/preroll.mp4"))
        self.assertFalse(is_ad_url("https://cdn.site.com/shows/ep1/master.m3u8"))


class Hls(unittest.TestCase):
    def test_master(self):
        info = parse_hls(MASTER, "https://x.com/m/master.m3u8")
        self.assertTrue(info["master"])
        self.assertEqual(info["height"], 1080)
        self.assertEqual(info["variants"][0]["url"], "https://x.com/m/high/index.m3u8")

    def test_media_duration_and_live(self):
        info = parse_hls(MEDIA)
        self.assertAlmostEqual(info["duration"], 120)
        self.assertFalse(info["live"])
        self.assertTrue(parse_hls(MEDIA.replace("#EXT-X-ENDLIST\n", ""))["live"])

    def test_drm_key(self):
        text = MEDIA.replace("#EXT-X-TARGETDURATION:10", '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://x",KEYFORMAT="com.apple.streamingkeydelivery"')
        self.assertTrue(parse_hls(text)["drm"])
        plain = MEDIA.replace("#EXT-X-TARGETDURATION:10", '#EXT-X-KEY:METHOD=AES-128,URI="k.key"')
        self.assertFalse(parse_hls(plain)["drm"])


MPD = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT1H2M3.5S">
 <Period><AdaptationSet mimeType="video/mp4">
  <Representation id="a" height="720" bandwidth="1"/><Representation id="b" height="1080" bandwidth="2"/>
 </AdaptationSet></Period></MPD>"""


class AudioHints(unittest.TestCase):
    def test_codecs_say_there_is_sound(self):
        m = ('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1280x720,CODECS="avc1.64001f,mp4a.40.2"\nv.m3u8\n')
        info = parse_hls(m)
        self.assertTrue(info["audio"])
        self.assertTrue(info["video"])

    def test_video_only_codecs(self):
        m = ('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1280x720,CODECS="avc1.64001f"\nv.m3u8\n')
        self.assertFalse(parse_hls(m)["audio"])

    def test_a_separate_audio_group_counts_as_sound(self):
        m = ('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="en",URI="a.m3u8"\n'
             '#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1280x720,CODECS="avc1.64001f",AUDIO="a"\nv.m3u8\n')
        self.assertTrue(parse_hls(m)["audio"])

    def test_no_codecs_means_unknown(self):
        self.assertIsNone(parse_hls(MASTER)["audio"])

    def test_dash_tracks(self):
        mpd = MPD.replace("</AdaptationSet>", '</AdaptationSet><AdaptationSet mimeType="audio/mp4"/>')
        info = parse_mpd(mpd)
        self.assertTrue(info["audio"])
        self.assertTrue(info["video"])


class Dash(unittest.TestCase):
    def test_duration_height_and_live(self):
        info = parse_mpd(MPD)
        self.assertTrue(info["valid"])
        self.assertAlmostEqual(info["duration"], 3723.5)
        self.assertEqual(info["height"], 1080)
        self.assertFalse(info["live"])
        self.assertTrue(parse_mpd(MPD.replace('type="static"', 'type="dynamic"'))["live"])

    def test_drm_content_protection(self):
        opener = '<AdaptationSet mimeType="video/mp4">'
        widevine = MPD.replace(opener, opener + '<ContentProtection schemeIdUri="urn:uuid:EDEF8BA9-79D6-4ACE-A3C8-'
                               '27DCD51D21ED"/>')
        self.assertTrue(parse_mpd(widevine)["drm"])
        cenc_only = MPD.replace(opener, opener + '<ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011" '
                                'value="cenc"/>')
        self.assertTrue(parse_mpd(cenc_only)["valid"])
        self.assertFalse(parse_mpd(cenc_only)["drm"])

    def test_garbage_is_not_a_manifest(self):
        self.assertFalse(parse_mpd("not xml")["valid"])
        self.assertFalse(parse_mpd("<html/>")["valid"])


def cand(url, kind="hls", **kw):
    return Candidate(url=url, kind=kind, **kw)


class Choosing(unittest.TestCase):
    def test_short_clip_loses_to_episode(self):
        ad = cand("https://cdn.x.com/clip.mp4", "mp4", size=900_000, seen=1)
        ep = cand("https://cdn.x.com/ep/master.m3u8", duration=1400, master=True, height=1080, seen=2)
        d = choose([ad, ep])
        self.assertIs(d.best, ep)
        self.assertEqual(d.rejected, 1)

    def test_ad_host_rejected_even_if_long(self):
        ad = cand("https://imasdk.googleapis.com/x.m3u8", duration=600)
        self.assertEqual(reject_reason(ad), "ad")
        self.assertIsNone(choose([ad]).best)

    def test_short_hls_rejected(self):
        self.assertEqual(reject_reason(cand("https://x.com/a.m3u8", duration=30)), "short")

    def test_active_frame_wins(self):
        a = cand("https://x.com/a/master.m3u8", duration=1500, frame="F1", seen=1)
        b = cand("https://y.com/b/master.m3u8", duration=1500, frame="F2", seen=2)
        self.assertIs(choose([a, b], active_frames={"F1"}).best, a)

    def test_page_video_matches_and_sets_duration(self):
        ep = cand("https://x.com/ep.mp4", "mp4", size=50_000_000)
        other = cand("https://x.com/other.mp4", "mp4", size=60_000_000)
        apply_page_videos([ep, other], [{"src": ep.url, "dur": 1320, "playing": True}])
        self.assertTrue(ep.on_page)
        self.assertEqual(ep.duration, 1320)
        self.assertIs(choose([ep, other]).best, ep)

    def test_close_call_is_reported(self):
        a = cand("https://x.com/a.m3u8", duration=1400, master=True)
        b = cand("https://x.com/b.m3u8", duration=1380, master=True)
        d = choose([a, b])
        self.assertEqual(len(d.close), 1)

    def test_clear_winner_has_no_close_calls(self):
        a = cand("https://x.com/a.m3u8", duration=7000, master=True, height=1080)
        b = cand("https://x.com/b.m3u8", duration=200)
        self.assertEqual(choose([a, b]).close, [])

    def test_the_server_the_player_is_downloading_from_wins(self):
        old = cand("https://old.cdn/a/master.m3u8", duration=1400, master=True, height=1080, seen=1)
        live = cand("https://new.cdn/b/master.m3u8", duration=1400, master=True, height=1080, seen=2, active_age=3.0)
        d = choose([old, live])
        self.assertIs(d.best, live)
        self.assertTrue(d.playing)
        self.assertEqual(d.close, [])

    def test_nothing_playing_means_not_playing(self):
        a = cand("https://a.cdn/m.m3u8", duration=1400, master=True)
        b = cand("https://b.cdn/m.m3u8", duration=1300, master=True)         # a genuinely different video
        d = choose([a, b])
        self.assertFalse(d.playing)
        self.assertTrue(d.close)

    def test_the_same_video_offered_twice_is_not_a_choice(self):
        a = cand("https://a.cdn/m.m3u8", duration=1400, master=True, height=1080)
        b = cand("https://b.cdn/m.m3u8", duration=1401, master=True, height=1080)    # a mirror of it
        self.assertEqual(choose([a, b]).close, [])

    def test_a_picture_without_sound_loses_to_one_with_sound(self):
        silent = cand("https://a.cdn/v.m3u8", duration=1400, height=1080, has_audio=False, has_video=True, seen=9)
        good = cand("https://a.cdn/m.m3u8", duration=1400, master=True, height=720, has_audio=True, has_video=True)
        self.assertIs(choose([silent, good]).best, good)

    def test_audio_only_is_never_the_video(self):
        sound = cand("https://a.cdn/a.m3u8", duration=1400, has_video=False, has_audio=True)
        self.assertEqual(reject_reason(sound), "audio only")
        self.assertIsNone(choose([sound]).best)

    def test_unreadable_playlists_rank_below_readable_ones(self):
        ok = cand("https://a.cdn/m.m3u8", duration=1400, master=True, height=1080)
        bad = cand("https://b.cdn/m.m3u8", measured=False, seen=9)
        self.assertIs(choose([bad, ok]).best, ok)

    def test_variants_are_dropped(self):
        m = cand("https://x.com/master.m3u8", duration=1400, master=True)
        v = cand("https://x.com/high/index.m3u8", duration=1400, covered=True)
        self.assertIs(choose([m, v]).best, m)

    def test_live_is_allowed(self):
        self.assertIsNone(reject_reason(cand("https://x.com/live.m3u8", live=True)))

    def test_preview_url_is_penalised(self):
        real = cand("https://x.com/ep1.mp4", "mp4", size=80_000_000)
        prev = cand("https://x.com/ep1_preview.mp4", "mp4", size=85_000_000)
        self.assertIs(choose([prev, real]).best, real)


if __name__ == "__main__":
    unittest.main()
