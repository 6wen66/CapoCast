import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import phone_store as ps  # noqa: E402


class Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "phone.json")
        self.s = ps.PhoneStore(self.path)

    def reopen(self):
        return ps.PhoneStore(self.path)

    def test_only_real_web_pages_are_kept(self):
        for bad in ("", "about:blank", "data:text/html,hi", "javascript:alert(1)", "file:///c:/x", "https://", "x" * 3000):
            self.s.visit(bad, "t")
            self.assertEqual(self.s.recent(), [], bad)
            with self.assertRaises(ValueError):
                self.s.toggle_bookmark(bad)

    def test_history_is_newest_first_and_one_row_per_page(self):
        for u in ("https://a.example/", "https://b.example/", "https://a.example/"):
            self.s.visit(u, "t")
        self.assertEqual([r["url"] for r in self.s.recent()], ["https://a.example/", "https://b.example/"])

    def test_the_title_arriving_late_updates_the_same_row(self):
        self.s.visit("https://a.example/watch", "")
        self.s.visit("https://a.example/watch", "Ghost in the Shell")
        self.s.visit("https://a.example/watch", "Ghost in the Shell")
        rows = self.s.recent()
        self.assertEqual((len(rows), rows[0]["title"]), (1, "Ghost in the Shell"))

    def test_history_is_capped_and_can_be_cleared(self):
        for i in range(ps.MAX_HISTORY + 25):
            self.s.visit(f"https://site.example/{i}")
        self.assertEqual(len(self.s.history), ps.MAX_HISTORY)
        self.assertEqual(self.s.history[0]["url"], f"https://site.example/{ps.MAX_HISTORY + 24}")
        self.s.clear_history()
        self.assertEqual(self.reopen().recent(), [])

    def test_bookmarks_toggle_and_survive_the_history_cap_and_clear(self):
        self.assertTrue(self.s.toggle_bookmark("https://a.example/", "A"))
        self.assertTrue(self.s.is_bookmarked("https://a.example/"))
        for i in range(ps.MAX_HISTORY + 5):
            self.s.visit(f"https://site.example/{i}")
        self.s.clear_history()
        self.assertEqual([b["title"] for b in self.reopen().all_bookmarks()], ["A"])
        self.assertFalse(self.s.toggle_bookmark("https://a.example/"))
        self.assertEqual(self.s.all_bookmarks(), [])

    def test_remove_bookmark_is_quiet_when_missing(self):
        self.s.toggle_bookmark("https://a.example/")
        self.s.remove_bookmark("https://nope.example/")
        self.s.remove_bookmark("https://a.example/")
        self.assertEqual(self.s.all_bookmarks(), [])

    def test_everything_is_saved_and_reloaded(self):
        self.s.toggle_bookmark("https://a.example/", "A")
        self.s.visit("https://b.example/", "B")
        again = self.reopen()
        self.assertEqual([b["url"] for b in again.all_bookmarks()], ["https://a.example/"])
        self.assertEqual([r["title"] for r in again.recent()], ["B"])

    def test_a_damaged_or_hostile_file_starts_empty_instead_of_crashing(self):
        for content in ("{not json", "[]", '{"bookmarks": 5, "history": [{"url": 7}]}', '{"history": [null]}'):
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(content)
            s = ps.PhoneStore(self.path)
            self.assertEqual((s.all_bookmarks(), s.recent()), ([], []), content)

    def test_a_file_with_bad_rows_keeps_the_good_ones(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"bookmarks": [{"url": "https://ok.example/", "title": "ok"}, {"url": "javascript:x"}]}, f)
        self.assertEqual([b["url"] for b in ps.PhoneStore(self.path).all_bookmarks()], ["https://ok.example/"])


if __name__ == "__main__":
    unittest.main()
