import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_view import ALT, CTRL, SHIFT, key_events, modifiers  # noqa: E402


class Modifiers(unittest.TestCase):
    def test_bits(self):
        self.assertEqual(modifiers(0), 0)
        self.assertEqual(modifiers(SHIFT), 8)
        self.assertEqual(modifiers(CTRL), 2)
        self.assertEqual(modifiers(ALT), 1)
        self.assertEqual(modifiers(SHIFT | CTRL), 10)


class Keys(unittest.TestCase):
    def test_letter_types_text(self):
        down, up = [e for _, e in key_events("a", "a", 0)]
        self.assertEqual((down["type"], down["text"], up["type"]), ("keyDown", "a", "keyUp"))

    def test_capital_keeps_shift(self):
        down = key_events("A", "A", SHIFT)[0][1]
        self.assertEqual((down["text"], down["modifiers"]), ("A", 8))

    def test_enter_submits_with_text(self):
        down = key_events("Return", "\r", 0)[0][1]
        self.assertEqual((down["key"], down["text"], down["windowsVirtualKeyCode"]), ("Enter", "\r", 13))

    def test_backspace_and_arrows_have_no_text(self):
        for sym, key in (("BackSpace", "Backspace"), ("Left", "ArrowLeft"), ("Next", "PageDown")):
            down = key_events(sym, "", 0)[0][1]
            self.assertEqual(down["key"], key)
            self.assertEqual(down["type"], "rawKeyDown")
            self.assertNotIn("text", down)

    def test_ctrl_shortcuts_use_editing_commands(self):
        down = key_events("a", "\x01", CTRL)[0][1]
        self.assertEqual(down["commands"], ["selectAll"])
        self.assertEqual(key_events("c", "\x03", CTRL)[0][1]["commands"], ["copy"])

    def test_ctrl_v_is_a_paste(self):
        self.assertEqual(key_events("v", "\x16", CTRL), [("paste", None)])

    def test_altgr_is_a_character_not_a_shortcut(self):
        events = key_events("e", "€", CTRL | ALT)
        self.assertEqual(events[0][1]["text"], "€")

    def test_modifier_only_and_control_chars_do_nothing(self):
        self.assertEqual(key_events("Shift_L", "", SHIFT), [])
        self.assertEqual(key_events("F5", "", 0), [])


if __name__ == "__main__":
    unittest.main()


class Placement(unittest.TestCase):
    """A frame that isn't the canvas's size (a phone is browsing) is shown whole, centred, and clicks still land right."""

    def test_same_size_is_left_alone(self):
        from web_view import fit
        self.assertEqual(fit(1280, 700, 1280, 700), (0, 0, 1.0))
        self.assertEqual(fit(0, 0, 1280, 700), (0, 0, 1.0))

    def test_a_phone_sized_frame_is_scaled_up_to_fit_and_centred(self):
        from web_view import fit
        ox, oy, scale = fit(390, 555, 1900, 850)
        self.assertAlmostEqual(scale, 850 / 555)
        self.assertEqual(oy, 0)
        self.assertEqual(ox, round((1900 - 390 * scale) / 2))

    def test_a_wide_frame_in_a_tall_canvas_is_letterboxed_top_and_bottom(self):
        from web_view import fit
        ox, oy, scale = fit(1000, 500, 500, 1000)
        self.assertEqual((ox, scale), (0, 0.5))
        self.assertEqual(oy, 375)

    def test_clicks_map_back_onto_the_page(self):
        from web_view import fit, to_page
        placement = fit(390, 555, 1900, 850)
        ox, oy, scale = placement
        self.assertEqual(to_page(ox, oy, placement), (0, 0))                     # the picture's top-left corner
        x, y = to_page(ox + 390 * scale, oy + 555 * scale, placement)           # ...and its bottom-right
        self.assertAlmostEqual(x, 390)
        self.assertAlmostEqual(y, 555)
        self.assertEqual(to_page(10, 20, (0, 0, 1.0)), (10, 20))                # normal case: unchanged
