"""BL preview, persisted form settings and event navigation through real Tk widgets."""
from dataclasses import asdict, replace
import gc
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from neuromotion.bl_ui import preview_bl
from neuromotion.acquisition import AcquisitionController, BinaryReplaySource
from neuromotion.gui import NeuroMotionApp
from neuromotion.package_check import pump_until
from neuromotion.pipeline import run_pipeline
from neuromotion.replay import ReplayEngine
from neuromotion.validation import make_synthetic_inputs
from test_stimulus_integration import make_bl
from test_four_channel_integration import make_wire


class StimulusUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        config = make_synthetic_inputs(cls.root/'fixture', 30, 1000)
        cls.bl = cls.root/'bl.csv'; make_bl(cls.bl)
        binary = cls.root/'native.bin'; make_wire(binary)
        controller = AcquisitionController()
        controller.connect(source=BinaryReplaySource(binary, speed=0))
        controller.start_recording(cls.root/'session'); controller.wait()
        cls.config = replace(config, bl_csv=cls.bl, emg_csv=cls.root/'session/acquisition/emg.csv')
        run_pipeline(cls.config)

    @classmethod
    def tearDownClass(cls):
        gc.collect(); cls.temp.cleanup()

    def setUp(self):
        self.app = NeuroMotionApp()
        self.app.geometry('1350x950'); self.app.attributes('-alpha', 0)
        self.errors = []
        self.app.report_callback_exception = lambda kind, value, tb: self.errors.append(f'{kind.__name__}: {value}')
        self.app.update_idletasks()

    def tearDown(self):
        self.app._close()
        self.assertFalse(self.errors)
        self.__dict__.pop('app', None); gc.collect()

    def test_background_preview_edit_and_parameter_save(self):
        self.app.bl_csv.set(str(self.bl))
        page = self.app.bl_page
        page.load()
        pump_until(self.app, lambda: not self.app.busy, 8, self.errors)
        self.assertEqual(len(page.table.get_children()), 5)
        self.assertEqual(self.app.book.select(), str(page))
        key = page.events[1]['event_id']
        page.table.selection_set(key); page._selected()
        page.width.set('.3'); page.notes.set('paper note'); page.apply_selected()
        self.assertEqual(page.events[1]['commanded_current_ma'], 0)
        self.assertEqual(page.events[1]['pulse_width_ms'], .3)
        dest = self.root/'edited.csv'
        with patch('neuromotion.bl_ui.filedialog.asksaveasfilename', return_value=str(dest)):
            page.save()
        loaded = preview_bl(self.bl, dest)['summary']
        self.assertEqual(loaded['events'][1]['pulse_width_ms'], .3)
        self.assertEqual(loaded['events'][1]['notes'], 'paper note')
        self.assertEqual(self.app.stimulus_parameters.get(), str(dest))

    def test_replay_event_navigation_does_not_repeat_subframe_event(self):
        page = self.app.replay_page
        page.set_engine(ReplayEngine(self.config.output_dir))
        settled_at = time.monotonic()+.1
        pump_until(self.app, lambda: time.monotonic() >= settled_at, 3, self.errors)
        self.app.update_idletasks()
        self.assertEqual(len(page.event_table.get_children()), 5)
        page.jump_event(1)
        first = page.event_cursor
        page.jump_event(1)
        self.assertGreater(page.event_cursor, first)
        self.assertEqual(sum(len(c.surface.find_withtag('stimulus_event')) for c in page.traces), 12)
        page.jump_event(-1)
        self.assertEqual(page.event_cursor, first)
        page.step(1)
        self.assertIsNone(page.event_cursor)
        self.assertGreater(page.canvas.winfo_height(), 100)
        self.assertGreater(page.event_table.winfo_height(), 50)
        self.assertGreaterEqual(page.positions.winfo_height(), page.positions.winfo_reqheight())
        self.assertLessEqual(page.positions.winfo_y()+page.positions.winfo_height(), page.trace_host.winfo_height())

    def test_new_config_round_trip_and_live_offline_exclusion(self):
        values = asdict(self.config)
        self.app._apply_config(values, self.root)
        config = self.app._form_config()
        self.assertEqual(config.bl_csv, self.bl)
        self.assertEqual(config.coordinate_mode, 'metric')
        self.app.pixel_only.set(True)
        self.assertEqual(self.app._form_config().coordinate_mode, 'pixel')
        self.app.live_page.operating = True
        try:
            with patch('neuromotion.gui.messagebox.showwarning') as warning:
                self.app.bl_page.load()
                warning.assert_called_once()
                self.assertFalse(self.app.busy)
        finally:
            self.app.live_page.operating = False


if __name__ == '__main__':
    unittest.main()
