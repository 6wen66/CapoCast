import os
import sys
import tkinter as tk
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import roku_listen_gui as g  # noqa: E402


class FakeTvVolume:
    def __init__(self):
        self.level, self.muted, self.target, self.targets = None, False, None, []

    def set_target(self, v):
        self.target = v
        self.targets.append(v)


class VolumeRow(unittest.TestCase):
    """The real _refresh_volume / _on_volume code on real widgets, in a Tk root that's never shown."""

    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        r = self.root
        self.app = SimpleNamespace(
            listener=None, pc_volume=120, tvvol=FakeTvVolume(),
            vol_title=tk.Label(r), vol_label=tk.Label(r), mute_lbl=tk.Label(r),
            scale=g.Slider(r, 0, 200, 120, lambda v: None))
        self.app._refresh_volume = lambda: g.App._refresh_volume(self.app)
        self.app.scale.command = lambda v: g.App._on_volume(self.app, v)

    def show(self):
        g.App._refresh_volume(self.app)
        return self.app.vol_title.cget("text"), self.app.vol_label.cget("text"), self.app.scale.hi

    def test_playing_on_the_roku_the_slider_is_the_tvs_volume(self):
        self.assertEqual(self.show(), ("Volume  ·  TV", "—", 100))       # level unknown until the TV says

    def test_it_follows_the_tv(self):
        self.app.tvvol.level = 40
        self.assertEqual(self.show(), ("Volume  ·  TV", "40", 100))
        self.assertEqual(self.app.scale.get(), 40)
        self.app.tvvol.level, self.app.tvvol.muted = 25, True            # you used the remote, then muted
        g.App._refresh_volume(self.app)
        self.assertEqual(self.app.scale.get(), 25)
        self.assertEqual(self.app.mute_lbl.cget("text"), "Muted")

    def test_routed_to_the_pc_the_slider_is_the_pc_volume(self):
        self.app.listener = object()
        self.assertEqual(self.show(), ("Volume  ·  this PC", "120%", 200))
        self.assertEqual(self.app.scale.get(), 120)

    def test_connecting_and_disconnecting_switches_by_itself_and_keeps_both_volumes(self):
        self.app.tvvol.level = 33
        g.App._refresh_volume(self.app)                                   # on the Roku
        self.app.listener = SimpleNamespace(volume=1.0)
        g.App._refresh_volume(self.app)                                   # connect: now the PC
        self.assertEqual((self.app.scale.hi, self.app.scale.get()), (200, 120))
        self.app.tvvol.level = 35                                         # the TV changes while we listen on the PC
        g.App._refresh_volume(self.app)
        self.assertEqual((self.app.scale.hi, self.app.scale.get()), (200, 120))   # the PC slider isn't disturbed
        self.app.listener = None
        g.App._refresh_volume(self.app)                                   # disconnect: back to the TV
        self.assertEqual((self.app.scale.hi, self.app.scale.get()), (100, 35))
        self.assertEqual(self.app.pc_volume, 120)                         # the PC volume was never overwritten

    def test_dragging_the_slider_drives_the_right_device(self):
        g.App._on_volume(self.app, 70)                                    # on the Roku
        self.assertEqual(self.app.tvvol.targets, [70])
        self.assertEqual(self.app.pc_volume, 120)                         # PC volume untouched
        self.app.listener = SimpleNamespace(volume=1.0)
        g.App._on_volume(self.app, 150)                                   # routed to the PC
        self.assertEqual(self.app.pc_volume, 150)
        self.assertEqual(self.app.listener.volume, 1.5)
        self.assertEqual(self.app.tvvol.targets, [70])                    # the TV wasn't touched

    def test_the_tvs_own_updates_dont_fight_your_hand(self):
        self.app.tvvol.level = 50
        g.App._refresh_volume(self.app)
        self.app.scale.dragging = True
        self.app.tvvol.level = 60                                         # an announcement arrives mid-drag
        g.App._refresh_volume(self.app)
        self.assertEqual(self.app.scale.get(), 50)
        self.app.scale.dragging = False
        g.App._refresh_volume(self.app)
        self.assertEqual(self.app.scale.get(), 60)


if __name__ == "__main__":
    unittest.main()
