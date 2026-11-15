from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import tkinter as tk

from neuromotion.acquisition import SimulatedSource
from neuromotion.gui import NeuroMotionApp


class ClientWorkflowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.app = NeuroMotionApp()
        except tk.TclError as exc:
            self.skipTest(f"Tk display is unavailable: {exc}")
        self.app.withdraw()
        self.temp = tempfile.TemporaryDirectory()
        self.app.live_page.root_dir.set(self.temp.name)

    def tearDown(self):
        if hasattr(self, "app"):
            with patch("tkinter.messagebox.askyesno", return_value=True):
                self.app._close()
            self.temp.cleanup()

    def test_paused_plot_records_and_hands_off_saved_csv(self):
        self.app.live_page.pause.set(True)
        self.app.live_page.controller.connect(source=SimulatedSource(0.24, speed=1))
        self.app.live_page.start_recording()
        deadline = time.monotonic() + 10
        with patch("tkinter.messagebox.showerror") as errors:
            while time.monotonic() < deadline:
                self.app.update()
                if not self.app.live_page.active and Path(self.app.emg.get()).is_file():
                    break
                time.sleep(0.01)
        self.assertEqual(self.app.live_page.controller.record_count, 240)
        self.assertTrue(Path(self.app.emg.get()).is_file())
        self.assertFalse(errors.called)

    def test_offline_jobs_are_blocked_while_acquisition_is_connected(self):
        self.app.live_page.controller.connect(source=SimulatedSource(0.24, speed=1))
        called = []
        with patch("tkinter.messagebox.showwarning") as warning:
            self.app._background("should not run", lambda: called.append(True))
        self.assertTrue(warning.called)
        self.assertFalse(called)
        self.assertFalse(self.app.busy)


if __name__ == "__main__":
    unittest.main()
