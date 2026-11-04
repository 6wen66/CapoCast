import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import roku_listen_gui as g  # noqa: E402  (importing it opens no window)


class SimTV:
    """A Roku's volume: moves one step per key press, clamps at 0 and 100, and announces each real change a moment
    later (stays silent when a press changes nothing, like the real one at its limits)."""

    def __init__(self, level):
        self.level, self.presses, self.muted, self.tv = level, [], False, None

    def key(self, name):
        self.presses.append(name)
        before = self.level
        if name == "VolumeUp":
            self.level = min(100, self.level + 1)
        elif name == "VolumeDown":
            self.level = max(0, self.level - 1)
        elif name == "VolumeMute":
            self.muted = not self.muted
            threading.Timer(0.01, self.announce).start()
            return
        if self.level != before:
            threading.Timer(0.01, self.announce).start()

    def announce(self):
        self.tv.heard({"volume": self.level, "mute": self.muted, "dest": "speakers"})


def wait_for(fn, seconds=6.0):
    end = time.time() + seconds
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.02)
    return fn()


class TvVolumeTests(unittest.TestCase):
    def make(self, level):
        self.sim, self.changes = SimTV(level), []
        self.original = g.Remote.key
        g.Remote.key = classmethod(lambda cls, host, name: self.sim.key(name))
        self.tv = g.TvVolume(lambda: "192.168.1.50", lambda: self.changes.append(self.tv.level))
        self.sim.tv = self.tv
        return self.tv

    def tearDown(self):
        g.Remote.key = self.original

    def test_it_learns_the_level_from_the_first_press_then_reaches_the_target(self):
        tv = self.make(50)
        self.assertIsNone(tv.level)                      # the Roku can't be asked
        tv.set_target(55)
        self.assertTrue(wait_for(lambda: self.sim.level == 55 and tv.target is None))
        self.assertEqual(self.sim.presses, ["VolumeUp"] * 5)
        self.assertEqual(tv.level, 55)

    def test_going_down(self):
        tv = self.make(50)
        tv.set_target(44)
        self.assertTrue(wait_for(lambda: self.sim.level == 44 and tv.target is None))
        self.assertEqual(self.sim.presses[0], "VolumeUp")       # the first press only finds the starting point
        self.assertEqual(self.sim.presses.count("VolumeDown"), 7)

    def test_it_follows_changes_made_with_the_remote(self):
        tv = self.make(30)
        self.sim.level = 31
        self.sim.announce()                               # you pressed the physical remote
        self.assertTrue(wait_for(lambda: tv.level == 31))
        self.assertEqual(self.sim.presses, [])            # and we pressed nothing

    def test_mute_state_is_tracked(self):
        tv = self.make(30)
        tv.toggle_mute()
        self.assertTrue(wait_for(lambda: tv.muted))
        tv.toggle_mute()
        self.assertTrue(wait_for(lambda: not tv.muted))

    def test_at_the_limit_it_gives_up_instead_of_pressing_forever(self):
        tv = self.make(100)
        tv.set_target(100)                                # already at max: Up changes nothing, nothing is announced
        self.assertTrue(wait_for(lambda: tv.target is None, 8))
        self.assertLessEqual(len(self.sim.presses), 3)

    def test_a_new_target_while_moving_takes_over(self):
        tv = self.make(20)
        tv.set_target(60)
        self.assertTrue(wait_for(lambda: tv.level is not None and tv.level >= 25))
        tv.set_target(22)                                 # you dragged back
        self.assertTrue(wait_for(lambda: self.sim.level == 22 and tv.target is None))

    def test_targets_are_clamped(self):
        tv = self.make(98)
        tv.set_target(500)
        self.assertEqual(tv.target, 100)
        tv.set_target(-5)
        self.assertEqual(tv.target, 0)


if __name__ == "__main__":
    unittest.main()
