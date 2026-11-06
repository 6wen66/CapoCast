import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tv_apps  # noqa: E402

APPS_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<apps>
	<app id="tvinput.hdmi1" type="tvin" version="1.0.0">PlayStation 5</app>
	<app id="tvinput.dtv" type="tvin" version="1.0.0">Live TV</app>
	<app id="12" type="appl" version="61.2523.1521020">Netflix</app>
	<app id="693751" type="appl" version="0.47.2">Playlet</app>
	<app id="dev" type="appl" version="1.0.1">RokuCast</app>
	<app id="13" type="appl" version="15.4">Prime Video</app>
	<app id="837" type="appl" version="2.26.95">YouTube &amp; More</app>
	<app type="appl">No id</app>
	<app id="99" type="appl"></app>
</apps>"""


class ParsingTheAppList(unittest.TestCase):
    def test_apps_come_back_sorted_and_tv_inputs_are_left_out(self):
        names = [a["name"] for a in tv_apps.parse_apps(APPS_XML)]
        self.assertEqual(names, ["Netflix", "Playlet", "Prime Video", "RokuCast", "YouTube & More"])

    def test_ids_are_kept_as_text_including_the_sideloaded_channel(self):
        ids = {a["name"]: a["id"] for a in tv_apps.parse_apps(APPS_XML)}
        self.assertEqual(ids["Playlet"], "693751")
        self.assertEqual(ids["RokuCast"], "dev")

    def test_entries_that_cant_be_launched_are_skipped(self):
        self.assertNotIn("No id", [a["name"] for a in tv_apps.parse_apps(APPS_XML)])
        self.assertEqual(len(tv_apps.parse_apps(APPS_XML)), 5)

    def test_anything_else_is_an_empty_list_not_a_crash(self):
        for junk in ("", "not xml", "<html><body>no</body></html>", "<apps></apps>", None):
            self.assertEqual(tv_apps.parse_apps(junk), [])


class Pins(unittest.TestCase):
    def test_playlet_is_pinned_until_you_choose_otherwise(self):
        self.assertEqual(tv_apps.clean_pins(None), ["693751"])
        self.assertEqual(tv_apps.clean_pins("garbage"), ["693751"])

    def test_unpinning_everything_is_remembered(self):
        self.assertEqual(tv_apps.clean_pins([]), [])

    def test_saved_pins_are_tidied(self):
        self.assertEqual(tv_apps.clean_pins(["12", 13, "12", "", "dev"]), ["12", "13", "dev"])
        self.assertEqual(len(tv_apps.clean_pins([str(n) for n in range(20)])), tv_apps.MAX_PINS)

    def test_pinning_and_unpinning(self):
        pins, what = tv_apps.toggle_pin(["693751"], "12")
        self.assertEqual((pins, what), (["693751", "12"], "pinned"))
        pins, what = tv_apps.toggle_pin(pins, "693751")
        self.assertEqual((pins, what), (["12"], "unpinned"))

    def test_a_full_row_refuses_more_without_changing(self):
        full = [str(n) for n in range(tv_apps.MAX_PINS)]
        pins, what = tv_apps.toggle_pin(full, "99")
        self.assertEqual((pins, what), (full, "full"))
        self.assertIsNot(pins, full)                       # a copy: callers can't alter the original by accident
        self.assertEqual(tv_apps.toggle_pin(full, "0")[1], "unpinned")     # but unpinning one is always allowed


class Finding(unittest.TestCase):
    def test_the_filter_ignores_case_and_matches_anywhere_in_the_name(self):
        apps = tv_apps.parse_apps(APPS_XML)
        self.assertEqual([a["name"] for a in tv_apps.filter_apps(apps, "PRIME")], ["Prime Video"])
        self.assertEqual([a["name"] for a in tv_apps.filter_apps(apps, "e")][:2], ["Netflix", "Playlet"])
        self.assertEqual(len(tv_apps.filter_apps(apps, "  ")), 5)
        self.assertEqual(tv_apps.filter_apps(apps, "zzz"), [])

    def test_names_are_available_before_the_list_has_loaded(self):
        self.assertEqual(tv_apps.name_for([], "693751"), "Playlet")
        self.assertEqual(tv_apps.name_for([], "555"), "555")
        self.assertEqual(tv_apps.name_for(tv_apps.parse_apps(APPS_XML), "12"), "Netflix")


if __name__ == "__main__":
    unittest.main()
