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
