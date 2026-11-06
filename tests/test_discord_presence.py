import json
import os
import struct
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord_presence as dp  # noqa: E402

ID = "123456789012345678"


class FakeDiscord:
    """The Discord end of the pipe: records what it is sent and answers each packet, as Discord does."""

    def __init__(self, handshake_reply=None):
        self.sent, self.to_read, self.closed = [], b"", False
        self.handshake_reply = handshake_reply or {"cmd": "DISPATCH", "evt": "READY", "data": {}}

    def write(self, data):
        op, size = struct.unpack("<II", data[:8])
        body = json.loads(data[8:8 + size])
        self.sent.append((op, body))
        reply = self.handshake_reply if op == dp.HANDSHAKE else {"cmd": body.get("cmd"), "evt": None, "data": {}}
        self.to_read += dp.pack(dp.FRAME, reply)

    def read(self, n):
        out, self.to_read = self.to_read[:n], self.to_read[n:]
        return out

    def close(self):
        self.closed = True

    def activities(self):
        return [b["args"]["activity"] for op, b in self.sent if op == dp.FRAME]


def wait_for(fn, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.02)
    return False


class Packets(unittest.TestCase):
    def test_framing_round_trips(self):
        conn = FakeDiscord()
        conn.to_read = dp.pack(dp.FRAME, {"a": 1})
        self.assertEqual(dp.read_packet(conn), (dp.FRAME, {"a": 1}))

    def test_a_pipe_that_closes_part_way_is_an_error_not_a_hang(self):
        conn = FakeDiscord()
        conn.to_read = dp.pack(dp.FRAME, {"a": 1})[:5]
        with self.assertRaises(ConnectionError):
            dp.read_packet(conn)

    def test_the_application_id_is_checked(self):
        self.assertTrue(dp.valid_id(ID))
        for bad in ("", "abc", "12345", ID + "x", None, 123456789012345678):
            self.assertFalse(dp.valid_id(bad), bad)

    def test_text_is_tidied_to_what_discord_accepts(self):
        self.assertEqual(dp.clip("  Show \n Name  ", "x"), "Show Name")
        self.assertEqual(dp.clip("", "Watching"), "Watching")
        self.assertEqual(dp.clip("a", "Watching"), "Watching")
        long = dp.clip("x" * 500, "y")
        self.assertEqual(len(long), dp.MAX_TEXT)
        self.assertTrue(long.endswith("…"))


class PresenceCase(unittest.TestCase):
    def make(self, conn=None, **kw):
        self.conn = conn or FakeDiscord()
        self.p = dp.Presence(ID, log=lambda m: None, opener=lambda: self.conn, **{"min_gap": 0, **kw})
        self.addCleanup(self.p.stop)
        return self.p


class Showing(PresenceCase):

    def test_it_says_hello_with_the_application_id_then_sets_the_status(self):
        p = self.make()
        p.set("Frieren", "4K upscale · Anime4K HQ", since=1700000000)
        self.assertTrue(wait_for(lambda: self.conn.activities()))
        self.assertEqual(self.conn.sent[0], (dp.HANDSHAKE, {"v": 1, "client_id": ID}))
        act = self.conn.activities()[0]
        self.assertEqual(act["details"], "Frieren")
        self.assertEqual(act["state"], "4K upscale · Anime4K HQ")
        self.assertEqual(act["timestamps"], {"start": 1700000000})
        self.assertEqual(self.conn.sent[1][1]["args"]["pid"], os.getpid())

    def test_clearing_sends_an_empty_status(self):
        p = self.make()
        p.set("Frieren", "x")
        self.assertTrue(wait_for(lambda: len(self.conn.activities()) == 1))
        p.clear()
        self.assertTrue(wait_for(lambda: len(self.conn.activities()) == 2))
        self.assertIsNone(self.conn.activities()[1])

    def test_clearing_when_nothing_was_shown_doesnt_go_looking_for_discord(self):
        tried = []
        p = dp.Presence(ID, opener=lambda: tried.append(1))
        self.addCleanup(p.stop)
        p.clear()
        time.sleep(0.5)
        self.assertEqual(tried, [])

    def test_stopping_takes_the_status_down(self):
        p = self.make()
        p.set("Frieren", "x")
        self.assertTrue(wait_for(lambda: self.conn.activities()))
        p.stop()
        self.assertIsNone(self.conn.activities()[-1])
        self.assertTrue(self.conn.closed)

    def test_a_bad_application_id_never_touches_discord(self):
        tried = []
        p = dp.Presence("nope", opener=lambda: tried.append(1))
        p.set("Frieren", "x")
        time.sleep(0.4)
        self.assertEqual(tried, [])

    def test_discord_turning_the_id_down_is_logged_and_retried_later_not_spun_on(self):
        opened, logs = [], []
        bad = FakeDiscord({"cmd": "DISPATCH", "evt": "ERROR", "message": "Invalid Client ID"})

        def opener():
            opened.append(1)
            return bad
        p = dp.Presence(ID, log=logs.append, opener=opener)
        self.addCleanup(p.stop)
        p.set("Frieren", "x")
        self.assertTrue(wait_for(lambda: opened))
        time.sleep(1.5)
        self.assertEqual(len(opened), 1)                              # RETRY is 10 s, so not again yet

    def test_discord_opening_later_is_picked_up(self):
        conn, ready = FakeDiscord(), []

        def opener():
            return conn if ready else None
        p = dp.Presence(ID, opener=opener)
        self.addCleanup(p.stop)
        p.set("Frieren", "x")
        time.sleep(0.3)
        self.assertFalse(conn.activities())
        ready.append(1)
        p._next_try = 0.0
        self.assertTrue(wait_for(lambda: conn.activities()))

    def test_the_latest_status_wins_when_several_arrive_at_once(self):
        p = self.make()
        for n in range(5):
            p.set(f"Episode {n}", "x")
        self.assertTrue(wait_for(lambda: self.conn.activities() and self.conn.activities()[-1]["details"] == "Episode 4"))

    def test_a_dropped_pipe_is_survived(self):
        p = self.make()
        p.set("Frieren", "x")
        self.assertTrue(wait_for(lambda: self.conn.activities()))
        self.conn.write = lambda data: (_ for _ in ()).throw(BrokenPipeError())
        p.set("Frieren 2", "x")
        self.assertTrue(wait_for(lambda: not p.connected))


class Pictures(PresenceCase):
    def test_a_thumbnail_goes_up_beside_the_text(self):
        p = self.make()
        p.set("Frieren", "x", image="https://i.ytimg.com/vi/abc/hqdefault.jpg")
        self.assertTrue(wait_for(lambda: self.conn.activities()))
        self.assertEqual(self.conn.activities()[0]["assets"],
                         {"large_image": "https://i.ytimg.com/vi/abc/hqdefault.jpg", "large_text": "Frieren"})

    def test_only_https_pictures_of_sane_length_are_used(self):
        self.assertEqual(dp.clean_image(" https://a.b/c.jpg "), "https://a.b/c.jpg")
        for bad in (None, "", "http://a.b/c.jpg", "ftp://a/b", "https://a.b/ c.jpg", "https://a.b/" + "x" * 300, 5):
            self.assertIsNone(dp.clean_image(bad), bad)
        p = self.make()
        p.set("Frieren", "x", image="http://insecure/c.jpg")
        self.assertTrue(wait_for(lambda: self.conn.activities()))
        self.assertNotIn("assets", self.conn.activities()[0])

    def test_a_refused_picture_is_resent_as_text_alone_and_not_tried_again(self):
        conn = FakeDiscord()
        real_write = conn.write

        def write(data):
            op, size = struct.unpack("<II", data[:8])
            body = json.loads(data[8:8 + size])
            real_write(data)
            if op == dp.FRAME and (body["args"]["activity"] or {}).get("assets"):
                conn.to_read = dp.pack(dp.FRAME, {"cmd": "SET_ACTIVITY", "evt": "ERROR", "data": {"message": "bad image"}})
        conn.write = write
        p = self.make(conn)
        p.set("Frieren", "x", image="https://a.b/c.jpg")
        self.assertTrue(wait_for(lambda: len(conn.activities()) == 2))
        self.assertIn("assets", conn.activities()[0])
        self.assertNotIn("assets", conn.activities()[1])
        p.set("Heat", "x", image="https://a.b/d.jpg")
        self.assertTrue(wait_for(lambda: len(conn.activities()) == 3))
        self.assertNotIn("assets", conn.activities()[2])

    def test_updates_are_spaced_out_and_the_newest_wins(self):
        p = self.make(min_gap=0.6)
        p.set("One", "x")
        self.assertTrue(wait_for(lambda: len(self.conn.activities()) == 1))
        began = time.time()
        for n in ("Two", "Three", "Four"):
            p.set(n, "x")
        self.assertTrue(wait_for(lambda: len(self.conn.activities()) == 2))
        self.assertGreaterEqual(time.time() - began, 0.4)
        self.assertEqual(self.conn.activities()[1]["details"], "Four")


class FakePresence:
    def __init__(self, client_id, log=None):
        self.client_id, self.calls, self.stopped = client_id, [], False

    def set(self, title, state, since=None, image=None, image_text=None):
        self.calls.append(("set", title, state, image))

    def clear(self):
        self.calls.append(("clear",))

    def stop(self):
        self.stopped = True


class SharedStatus(unittest.TestCase):
    def make(self, client_id=ID):
        self.made = []
        st = dp.Status(client_id, factory=lambda cid, log: self.made.append(FakePresence(cid)) or self.made[-1])
        return st

    def test_a_source_that_is_off_shows_nothing(self):
        st = self.make()
        self.assertFalse(st.show("web", "Frieren", "x"))
        st.enable("web", True)
        self.assertTrue(st.show("web", "Frieren", "x", image="https://a.b/c.jpg"))
        self.assertEqual(self.made[0].calls, [("set", "Frieren", "x", "https://a.b/c.jpg")])

    def test_without_an_application_id_nothing_is_ever_shown(self):
        st = self.make("")
        st.enable("web", True)
        self.assertFalse(st.show("web", "Frieren", "x"))
        self.assertEqual(self.made, [])

    def test_the_most_recent_source_is_the_one_showing(self):
        st = self.make()
        st.enable("web", True)
        st.enable("youtube", True)
        st.show("youtube", "A video", "x")
        st.show("web", "An episode", "x")
        self.assertEqual(st.showing, "web")
        self.assertEqual(len(self.made), 1)                    # one connection to Discord, not one per tab

    def test_a_source_cannot_take_down_another_ones_status(self):
        st = self.make()
        st.enable("web", True)
        st.enable("youtube", True)
        st.show("youtube", "A video", "x")
        st.show("web", "An episode", "x")
        st.clear("youtube")                                    # YouTube stopping must leave the Web cast's status up
        self.assertEqual(st.showing, "web")
        self.assertEqual(self.made[0].calls[-1][0], "set")
        st.clear("web")
        self.assertEqual(self.made[0].calls[-1], ("clear",))
        self.assertIsNone(st.showing)

    def test_switching_a_source_off_takes_its_status_down(self):
        st = self.make()
        st.enable("youtube", True)
        st.show("youtube", "A video", "x")
        st.enable("youtube", False)
        self.assertEqual(self.made[0].calls[-1], ("clear",))
        self.assertFalse(st.is_on("youtube"))

    def test_a_new_application_id_starts_a_fresh_connection(self):
        st = self.make()
        st.enable("web", True)
        st.show("web", "Frieren", "x")
        st.set_id("987654321098765432")
        self.assertTrue(self.made[0].stopped)
        st.show("web", "Frieren", "x")
        self.assertEqual(self.made[1].client_id, "987654321098765432")


class Wiring(unittest.TestCase):
    """What the Web tab does with it: nothing at all unless it's switched on."""

    def tab(self, on):
        import web_tab
        from types import SimpleNamespace
        t = web_tab.WebTab.__new__(web_tab.WebTab)
        t.ui = SimpleNamespace(log=lambda m: None)
        t.upscaled, t.cast_since = None, 0.0
        self.calls = []
        t.discord = SimpleNamespace(
            is_on=lambda source: on, showing=None,
            show=lambda source, title, state, since=None, image=None, image_text=None: self.calls.append(
                ("show", source, title, state, image, image_text)),
            clear=lambda source: self.calls.append(("clear", source)))
        return t

    def test_it_shows_the_title_and_how_it_is_being_played(self):
        t = self.tab(True)
        t._set_presence("Frieren", "https://a.b/c.jpg")
        t.upscaled = {"mode": "movie"}
        t._set_presence("Heat")
        self.assertEqual(self.calls, [("show", "web", "Watching Frieren", "Casting to the TV", "https://a.b/c.jpg", "Frieren"),
                                      ("show", "web", "Watching Heat", "Casting to the TV · 4K upscale (Movie)", None,
                                       "Heat")])

    def test_the_site_is_named_under_the_title(self):
        t = self.tab(True)
        t._set_presence("Frieren Episode 5", "https://a.b/c.jpg", "Miruro")
        t.upscaled = {"mode": "movie"}
        t._set_presence("Heat", None, "FMovies")
        self.assertEqual(self.calls, [
            ("show", "web", "Watching Frieren Episode 5", "on Miruro", "https://a.b/c.jpg", "Frieren Episode 5 · Miruro"),
            ("show", "web", "Watching Heat", "on FMovies · 4K upscale (Movie)", None, "Heat · FMovies")])

    def test_the_upscale_mode_is_named_once(self):
        t = self.tab(True)
        t.upscaled = {"mode": "hq"}
        t._set_presence("Ghost in the Shell", None, "Miruro")
        self.assertEqual(self.calls[0][3], "on Miruro · Anime4K HQ upscale")
        self.assertEqual(self.calls[0][3].count("4K"), 1)

    def test_none_takes_only_the_web_status_down(self):
        t = self.tab(True)
        t._set_presence(None)
        self.assertEqual(self.calls, [("clear", "web")])

    def test_a_tab_built_without_init_has_discord_off(self):
        import web_tab
        t = web_tab.WebTab.__new__(web_tab.WebTab)
        self.assertFalse(t.discord_on)
        t._set_presence("Frieren")                             # no status object at all: quietly does nothing


class PageTitles(unittest.TestCase):
    def test_a_streaming_pages_boilerplate_comes_off(self):
        self.assertEqual(dp.clean_title("Watch Frieren Episode 5 English Sub Online - Miruro", "Miruro"),
                         "Frieren Episode 5")
        self.assertEqual(dp.clean_title("Heat (1995) Full Movie HD | FMovies", "", "https://fmovies.co/x"), "Heat (1995)")
        self.assertEqual(dp.clean_title("(3) Frieren - YouTube", "", "https://www.youtube.com/watch"), "Frieren")

    def test_titles_that_only_look_like_boilerplate_are_left_alone(self):
        for title in ("Sword Art Online", "Sword Art Online English Sub", "Spider-Man: No Way Home", "Watch Dogs",
                      "Re:Zero"):
            self.assertEqual(dp.clean_title(title, "Miruro", "https://miruro.cx/"), title.replace(" English Sub", "")
                             if title.endswith("English Sub") else title)

    def test_a_title_with_nothing_left_stays_as_it_was(self):
        self.assertEqual(dp.clean_title("Watch Anime Free Online", "", "https://miruro.cx"), "Watch Anime Free Online")
        self.assertEqual(dp.clean_title("Miruro", "Miruro"), "Miruro")
        self.assertEqual(dp.clean_title("", "Miruro"), "")

    def test_the_site_name(self):
        self.assertEqual(dp.site_label("https://www.miruro.cx/watch/1"), "Miruro")
        self.assertEqual(dp.site_label("https://www.bbc.co.uk/iplayer"), "Bbc")
        self.assertEqual(dp.site_label("https://x.example.com/", "Example TV"), "Example TV")
        self.assertEqual(dp.site_label("https://miruro.cx/", "Miruro - Watch anime online free in HD"), "Miruro")
        self.assertEqual(dp.site_label("https://localhost:8080/"), "")

    def test_a_leading_watch_goes_when_the_site_follows_it(self):
        self.assertEqual(dp.clean_title("Watch Ghost in the Shell - Miruro", "Miruro", "https://miruro.cx/w"),
                         "Ghost in the Shell")

    def test_a_logo_is_not_a_thumbnail(self):
        self.assertTrue(dp.looks_like_logo("https://cdn.example/img/logo-1200.png"))
        self.assertFalse(dp.looks_like_logo("https://cdn.example/thumbs/ep5.jpg"))


class Thumbnails(unittest.TestCase):
    def probe(self, sizes):
        return lambda url: sizes.get(url)

    def test_the_first_usable_poster_shaped_picture_wins(self):
        sizes = {"https://a/banner.jpg": (1900, 400), "https://a/hero.jpg": (3840, 2160)}
        self.assertEqual(dp.pick_image(["https://a/banner.jpg", "https://a/hero.jpg"], self.probe(sizes)),
                         "https://a/hero.jpg")

    def test_a_thin_banner_is_better_than_nothing(self):
        self.assertEqual(dp.pick_image(["https://a/banner.jpg"], self.probe({"https://a/banner.jpg": (1900, 400)})),
                         "https://a/banner.jpg")

    def test_pictures_discord_could_not_fetch_are_passed_over(self):
        sizes = {"https://a/ok.jpg": (460, 690)}
        self.assertEqual(dp.pick_image(["https://a/blocked.jpg", "http://a/insecure.jpg", "https://a/ok.jpg"],
                                       self.probe(sizes)), "https://a/ok.jpg")
        self.assertEqual(dp.pick_image(["https://a/blocked.jpg"], self.probe({})), "")

    def test_logos_and_repeats_are_never_fetched(self):
        asked = []
        dp.pick_image(["https://a/logo.png", "https://a/x.jpg", "https://a/x.jpg"], lambda u: asked.append(u))
        self.assertEqual(asked, ["https://a/x.jpg"])

    def test_probe_reads_a_real_picture_and_refuses_other_things(self):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (460, 690)).save(buf, "PNG")

        class Resp:
            def __init__(self, kind, body, status=200):
                self.headers, self.status, self.body = {"Content-Type": kind}, status, io.BytesIO(body)

            def read(self, n):
                return self.body.read(n)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass
        self.assertEqual(dp.probe_image("https://a/p.png", opener=lambda u: Resp("image/png", buf.getvalue())), (460, 690))
        self.assertIsNone(dp.probe_image("https://a/p", opener=lambda u: Resp("text/html", b"<html>")))
        self.assertIsNone(dp.probe_image("https://a/p.svg", opener=lambda u: Resp("image/svg+xml", b"<svg/>")))
        self.assertIsNone(dp.probe_image("https://a/p.png", opener=lambda u: Resp("image/png", b"x", status=403)))
        self.assertIsNone(dp.probe_image("https://a/p.png", opener=lambda u: 1 / 0))


class YoutubeInfo(unittest.TestCase):
    def setUp(self):
        dp._info_cache.clear()

    def test_channel_and_title_come_from_the_video_id(self):
        got = dp.youtube_info("dQw4w9WgXcQ", fetch=lambda v: {"author_name": " Rick ", "title": "Never Gonna"})
        self.assertEqual(got, ("Rick", "Never Gonna"))

    def test_a_failed_lookup_is_empty_and_not_remembered(self):
        def boom(v):
            raise OSError("offline")
        self.assertEqual(dp.youtube_info("dQw4w9WgXcQ", fetch=boom), ("", ""))
        self.assertEqual(dp.youtube_info("dQw4w9WgXcQ", fetch=lambda v: {"author_name": "A", "title": "T"}), ("A", "T"))

    def test_a_repeat_is_answered_from_memory(self):
        calls = []
        f = lambda v: calls.append(v) or {"author_name": "A", "title": "T"}
        dp.youtube_info("abc123", fetch=f)
        dp.youtube_info("abc123", fetch=f)
        self.assertEqual(len(calls), 1)

    def test_a_bad_id_is_never_looked_up(self):
        self.assertEqual(dp.youtube_info("a&b=c", fetch=lambda v: 1 / 0), ("", ""))


if __name__ == "__main__":
    unittest.main()
