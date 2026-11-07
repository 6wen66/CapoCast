"""Keeps test runs out of the real RokuListen.log: lines from fake videos and fake audio devices read like real faults."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import roku_listen_gui  # noqa: E402

roku_listen_gui.LOG = os.path.join(tempfile.gettempdir(), "RokuListen.test.log")
