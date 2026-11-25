import gc
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import tkinter as tk
from tkinter import font as tkfont
import unittest

import numpy as np

from neuromotion.axes import YAxisSettings
from neuromotion.axis_dialog import AxisSettingsDialog
from neuromotion.gui import NeuroMotionApp
from neuromotion.recording_v2 import FourChannelRecorder
from neuromotion.simulated_v2 import simulation_metadata
from scripts.validate_axes_layout import check_traces


class FourChannelUITests(unittest.TestCase):
    def setUp(self):
        try:self.app=NeuroMotionApp()
        except tk.TclError as exc:self.skipTest(str(exc))
        self.app.attributes('-alpha',0);self.app.geometry('1300x900')
        self.page=self.app.live_page
        self.page.controller.four=FourChannelRecorder(self.page.controller.settings,simulation_metadata())
        t=np.arange(2000)/1000
        values=np.column_stack((np.arange(len(t)),t,np.sin(t*40)*550000,np.cos(t*50)*650000,
                               np.sin(t*40)*100,np.cos(t*50)*200,(t%1<.5).astype(int),np.zeros(len(t)),
                               np.sin(t*60)*850000,np.cos(t*70)*950000,np.sin(t*60)*300,np.cos(t*70)*400))
        self.page.controller.history.extend(map(tuple,values))
        self.page.mode.set('原始');self.app.update();self.page.refresh_plot();self.app.update_idletasks()

    def tearDown(self):
        if hasattr(self,'app'):self.app._close()
        self.__dict__.pop('page',None);self.__dict__.pop('app',None);gc.collect()

    def test_five_axes_and_four_editor_tabs_fit_at_font_scales(self):
        page=self.page
        self.assertEqual(len(page.traces),5)
        for scale in (1,1.25,1.5,2):
            self.app.tk.call('tk','scaling',96/72*scale)
            page.axes.channels=[YAxisSettings(unit='uV') for _ in range(4)]
            self.app.update();page.refresh_plot();self.app.update_idletasks()
            self.assertEqual(len(check_traces(page.traces)),5)
            dialog=AxisSettingsDialog(page);self.app.update_idletasks()
            self.assertEqual(len(dialog.y_editors),4)
            self.assertLess(dialog.winfo_width(),self.app.winfo_screenwidth())
            self.assertLess(dialog.winfo_height(),self.app.winfo_screenheight())
            dialog.destroy()

    def test_ch4_mouse_and_linked_ranges_and_freeze(self):
        page=self.page;canvas=page.traces[3]
        left,top,right,bottom=canvas.plot_rect
        event=SimpleNamespace(x_root=canvas.winfo_rootx()+(left+right)/2,y_root=canvas.winfo_rooty()+(top+bottom)/2,delta=120,state=1)
        page._wheel(event,3)
        self.assertFalse(page.axes.channels[3].automatic)
        self.assertTrue(page.axes.channels[0].automatic)
        page.axes.set_linked(True,page.last_y_ranges);page.refresh_plot()
        self.assertEqual(len(set(page.last_y_ranges)),1)
        before=page.axes.common_y
        page._wheel(event,4)
        self.assertEqual(page.axes.common_y,before)
        event.state=0;page._wheel(event,3)
        self.assertEqual(page.frozen_data.shape[1],12)
        self.assertFalse(page.axes.follow)
        page.restore_live();self.assertIsNone(page.frozen_data)
        self.assertTrue(page.axes.follow)

    def test_recording_start_clears_frozen_preview(self):
        page = self.page
        page._freeze()
        self.assertIsNotNone(page.frozen_data)
        with tempfile.TemporaryDirectory() as folder:
            page.controller.source = SimpleNamespace(description={"kind": "simulated"})
            page.controller._open_recording(Path(folder)/"session")
            page.refresh_plot()
            self.assertIsNone(page.frozen_data)
            self.assertTrue(page.axes.follow)
            page.controller._finalize("test_finished", True)

    def test_hardware_fault_counts_are_visible(self):
        page = self.page
        page.controller.clock_snapshot = page.controller.four.status()
        page.controller.clock_snapshot["hardware"] = {"spi_errors_b": 3, "capture_overrun_a": 1}
        state = page.controller.snapshot(include_plot=False)
        state["state"] = "preview"
        page._update_stream_status(state)
        self.assertIn("SPI 错误 A/B 0/3", page.clock_info.get())
        self.assertIn("捕获溢出 A/B 1/0", page.clock_info.get())
        self.assertIn("采集或传输异常", page.data_status.get())

    def test_destroyed_trace_fonts_are_released_on_gui_thread(self):
        from unittest.mock import patch
        gc.collect()
        owner = threading.current_thread().name
        old_fonts = {id(trace.axis_font) for trace in self.page.traces}
        destroyed = []
        original = tkfont.Font.__del__

        def observe(font):
            thread = threading.current_thread().name
            if id(font) in old_fonts:
                destroyed.append(thread)
            # A wrong-thread Tk call waits for dispatch. Record the actual
            # resource-lifecycle violation without blocking this unit test.
            if thread == owner:
                original(font)

        was_enabled = gc.isenabled()
        gc.disable()
        try:
            with patch.object(tkfont.Font, '__del__', observe):
                self.page._set_channels(2)
                worker = threading.Thread(target=gc.collect, name='acquisition-gc-check')
                worker.start(); worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(destroyed), len(old_fonts))
                self.assertEqual(set(destroyed), {owner})
        finally:
            if was_enabled:
                gc.enable()

    def test_gui_validation_returns_while_stream_is_still_running(self):
        from neuromotion.package_check import pump_until
        from neuromotion.simulated_v2 import SimulatedFourChannelSource
        controller = self.page.controller
        with tempfile.TemporaryDirectory() as folder:
            controller.connect(source=SimulatedFourChannelSource(30, speed=1))
            controller.start_recording(Path(folder)/'session')
            start = time.monotonic()
            try:
                pump_until(self.app, lambda: time.monotonic()-start >= .8, 3, [])
                self.assertTrue(controller.thread.is_alive())
                self.assertGreater(controller.snapshot(include_plot=False)['samples'], 200)
                self.assertLess(time.monotonic()-start, 3)
            finally:
                controller.disconnect()

    def test_gui_validation_finishes_without_waiting_for_timer_queue_to_empty(self):
        from neuromotion.package_check import finish_live_view
        timer = [None]
        deadline = time.monotonic()+1.5
        def busy():
            if time.monotonic() < deadline:
                timer[0] = self.app.after(0, busy)
        timer[0] = self.app.after(0, busy)
        started = time.monotonic()
        try:
            finish_live_view(self.app, self.page, [])
            self.assertLess(time.monotonic()-started, 1.)
        finally:
            self.app.after_cancel(timer[0])


if __name__=='__main__':unittest.main()
