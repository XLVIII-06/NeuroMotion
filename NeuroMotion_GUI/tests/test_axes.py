from copy import deepcopy
import csv
import gc
from pathlib import Path
import struct
import tempfile
import time
from types import SimpleNamespace
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

import numpy as np

from neuromotion.acquisition import AcquisitionController, FrameDecoder, SimulatedSource
from neuromotion.axes import (LiveAxes, YAxisSettings, auto_limits, nice_ticks,
                             plot_window, tick_text, validate_window, value_unit)
from neuromotion.axis_dialog import AxisSettingsDialog
from neuromotion.gui import NeuroMotionApp
from neuromotion.live_ui import LiveAcquisitionPage, TraceCanvas


def history(first=0, count=60000, segment=0):
    indices = np.arange(first, first + count)
    times = indices / 1000
    return np.column_stack((indices, times, np.sin(indices), np.cos(indices) * 2000,
                            np.sin(indices) * 20, np.cos(indices) * 3000, (indices // 500) % 2,
                            np.full(count, segment)))


class AxisCalculationTests(unittest.TestCase):
    def test_auto_limits_zero_constants_nonfinite_and_margin(self):
        self.assertEqual(auto_limits([0, 0]), (-1, 1))
        self.assertEqual(auto_limits([10, 10]), (9, 11))
        self.assertEqual(auto_limits([-100, -100]), (-110, -90))
        self.assertEqual(auto_limits([-10, 20, np.nan]), (-13, 23))
        self.assertEqual(auto_limits([np.nan, np.inf]), (-1, 1))

    def test_units_ticks_and_negative_zero(self):
        self.assertEqual(value_unit((-999, 999)), ("µV", 1))
        self.assertEqual(value_unit((-1000, 900)), ("mV", 1000))
        self.assertEqual(value_unit((-5000, 5000), "uV"), ("µV", 1))
        self.assertEqual(value_unit((-1, 1), "mV"), ("mV", 1000))
        for limits in ((-.001, .001), (-550000, 600000), (1e11, 4e11), (-1, 1)):
            ticks, step = nice_ticks(limits)
            self.assertTrue(np.all(np.diff(ticks) > 0))
            self.assertTrue(np.all((ticks >= limits[0]) & (ticks <= limits[1])))
            self.assertEqual(len({tick_text(value, step) for value in ticks}), len(ticks))
        self.assertEqual(tick_text(-1e-15, .01), "0.00")
        ticks, step = nice_ticks((59.9995, 60.0005), 2)
        labels = [tick_text(value * 1000, step * 1000) for value in ticks]
        self.assertGreaterEqual(len(ticks), 2)
        self.assertEqual(len(labels), len(set(labels)))

    def test_linked_ranges_restore_original_independent_settings(self):
        axes = LiveAxes()
        axes.channels[1] = YAxisSettings(False, -2000, 3000, "mV")
        original = axes.channels.copy()
        axes.set_linked(True, [(-20, 50), (-2000, 3000)])
        self.assertFalse(axes.common_y.automatic)
        self.assertEqual(axes.y_ranges(history(count=3)), [(-2000, 3000)] * 2)
        axes.zoom_y(0, (-2000, 3000), 2, .5)
        self.assertEqual(axes.y_settings(0), axes.y_settings(1))
        axes.reset_y(1)
        self.assertTrue(axes.common_y.automatic)
        axes.set_linked(False, axes.y_ranges(history(count=3)))
        self.assertEqual(axes.channels, original)

    def test_visible_data_only_controls_auto_y(self):
        axes = LiveAxes(x_limits=(.002, .003))
        data = history(count=5)
        data[[0, 1, 4], 4:6] = 1e9
        self.assertLess(max(axes.y_ranges(data)[0]), 50)
        axes.set_linked(True, axes.y_ranges(data))
        self.assertEqual(axes.y_ranges(data)[0], axes.y_ranges(data)[1])

    def test_x_startup_zoom_anchor_bounds_and_smallest_window(self):
        axes = LiveAxes()
        self.assertEqual(axes.follow_range(2), (0, 10))
        self.assertEqual(axes.follow_range(100), (90, 100))
        axes.x_limits = (40, 50)
        axes.zoom_x(2, .25, (0, 60))
        self.assertEqual(axes.x_limits, (41.25, 46.25))
        axes.set_manual_x((-5, 5), (0, 60))
        self.assertEqual(axes.x_limits, (0, 10))
        axes.set_manual_x((70, 80), (0, 60))
        self.assertEqual(axes.x_limits, (50, 60))
        axes.zoom_x(1e9, .5, (0, 60))
        self.assertAlmostEqual(axes.x_limits[1] - axes.x_limits[0], .001)
        for invalid in (0, .0009, 61, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                validate_window(invalid)
        with self.assertRaises(ValueError):
            YAxisSettings(False, 5, 5).validate()

    def test_snapshot_crops_before_decimation_and_reports_full_bounds(self):
        controller = AcquisitionController()
        original = history()
        controller.history.extend(map(tuple, original))
        overview = controller.snapshot(max_points=2000)
        zoom = controller.snapshot(max_points=2000, plot_window_s=.02)
        self.assertLess(len(overview["plot"]), len(original))
        self.assertGreaterEqual(len(zoom["plot"]), 21)
        self.assertLessEqual(len(zoom["plot"]), 23)
        np.testing.assert_allclose(np.diff(zoom["plot"][:, 1]), .001)
        self.assertEqual(zoom["history_bounds_s"], (0, 59.999))
        self.assertEqual(zoom["plot_epoch"], (0, 0))
        self.assertEqual(len(controller.snapshot(include_plot=False)["plot"]), 0)
        self.assertEqual(list(controller.history), list(map(tuple, original)))

    def test_frozen_zoom_restores_samples_extrema_and_ttl_edges(self):
        data = history()
        data[25001, 4] = 999999
        overview = plot_window(data, (0, 60), max_points=2000)
        self.assertIn(25001, overview[:, 0])
        for index in (499, 500, 999, 1000):
            self.assertIn(index, overview[:, 0])
        zoom = plot_window(data, (25, 25.1))
        self.assertGreaterEqual(len(zoom), 101)
        np.testing.assert_allclose(np.diff(zoom[:, 0]), 1)


class TkAxisTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.attributes("-alpha", 0)
        self.root.geometry("1150x1100")
        self.root.option_add("*Font", ("Microsoft YaHei UI", 9))
        ttk.Style(self.root).configure("TLabel", font=("Microsoft YaHei UI", 9))

    def tearDown(self):
        if hasattr(self, "page"):
            self.page.shutdown()
        if hasattr(self, "root"):
            self.root.destroy()
        # Reclaim Tk reference cycles on the owning thread before the next
        # test starts an acquisition worker which may trigger cyclic GC.
        self.__dict__.pop("page", None)
        self.__dict__.pop("root", None)
        gc.collect()

    def make_page(self):
        host = SimpleNamespace(busy=False, replay_page=SimpleNamespace(playing=False),
                               _log=lambda text: None, _update_controls=lambda: None,
                               emg=tk.StringVar(self.root), output=tk.StringVar(self.root))
        self.page = LiveAcquisitionPage(self.root, host)
        self.page.pack(fill="both", expand=True)
        self.root.update()
        self.page.controller.history.extend(map(tuple, history(first=40000, count=60000)))
        self.page.refresh_plot()
        return self.page

    def event(self, channel=0, delta=120, state=0, fraction=.5):
        canvas = self.page.traces[channel]
        left, top, right, bottom = canvas.plot_rect
        return SimpleNamespace(x_root=canvas.winfo_rootx() + left + fraction * (right - left),
                               y_root=canvas.winfo_rooty() + (top + bottom) / 2, delta=delta, state=state)

    def test_labels_inside_canvas_at_four_windows_scales_and_widths(self):
        for scale in (1, 1.25, 1.5, 2):
            self.root.tk.call("tk", "scaling", 96 / 72 * scale)
            for width in (450, 1100):
                canvas = TraceCanvas(self.root, "CH1 · µV", "blue")
                canvas.pack(fill="x")
                self.root.geometry(f"{width}x700")
                self.root.update()
                for limits, unit in (((-650000, 550000), "uV"), ((-1e12, 1e12), "uV"),
                                     ((-650000, 550000), "auto"), ((-1, 1), "mV"), ((0, 9), "auto")):
                    canvas.draw([0, 2], limits, 10, y_limits=limits, unit=unit)
                    self.root.update_idletasks()
                    boxes = []
                    for tag in ("y_tick", "x_tick", "title", "x_unit"):
                        for item in canvas.find_withtag(tag):
                            box = canvas.bbox(item)
                            self.assertGreaterEqual(box[0], 0, (scale, width, tag, box))
                            self.assertGreaterEqual(box[1], 0, (scale, width, tag, box))
                            self.assertLessEqual(box[2], canvas.winfo_width(), (scale, width, tag, box))
                            self.assertLessEqual(box[3], canvas.winfo_height(), (scale, width, tag, box))
                            if tag == "y_tick":
                                boxes.append(box)
                    boxes.sort(key=lambda box: box[1])
                    for first, second in zip(boxes, boxes[1:]):
                        self.assertLessEqual(first[3], second[1])
                canvas.destroy()

    def test_startup_mapping_unit_title_ttl_ticks_and_clipping(self):
        canvas = TraceCanvas(self.root, "CH1 · µV", "blue")
        canvas.pack(fill="x")
        self.root.update()
        canvas.draw([0, 2], [0, 1000000], 10, y_limits=(-1000, 1000))
        self.assertEqual(canvas.current_x, (0, 10))
        self.assertIn("10", [canvas.itemcget(item, "text") for item in canvas.find_withtag("x_tick")])
        self.assertEqual(canvas.itemcget(canvas.find_withtag("title")[0], "text"), "CH1 · mV")
        coordinates = canvas.surface.coords(canvas.surface.find_withtag("waveform")[0])
        self.assertAlmostEqual(coordinates[-2] / (canvas.plot_rect[2] - canvas.plot_rect[0]), .2)
        self.assertLess(coordinates[-1], 0)  # Off-scale data lives in the clipping child.
        self.assertFalse(canvas.find_withtag("waveform"))
        ttl = TraceCanvas(self.root, "MCU TTL", "orange", digital=True)
        ttl.pack(fill="x")
        self.root.update()
        ttl.draw([0, 2], [0, 1], 10, x_unit="ms")
        self.assertEqual([ttl.itemcget(item, "text") for item in ttl.find_withtag("y_tick")], ["0", "1"])
        self.assertIn("10000", [ttl.itemcget(item, "text") for item in ttl.find_withtag("x_tick")])

    def test_freeze_zoom_pan_shared_axes_and_restore_preserve_y(self):
        page = self.make_page()
        before = page.axes.x_limits
        event = self.event()
        self.assertEqual(page._wheel(event, 0), "break")
        frozen = page.frozen_data.copy()
        self.assertFalse(page.axes.follow)
        self.assertTrue(page.pause.get())
        self.assertLess(np.diff(page.axes.x_limits)[0], np.diff(before)[0])
        self.assertEqual(len({trace.current_x for trace in page.traces}), 1)
        self.assertEqual(len({trace.plot_rect[0] for trace in page.traces}), 1)
        with page.controller.lock:
            page.controller.history.extend(map(tuple, history(first=100000, count=2000)))
        page.refresh_plot()
        np.testing.assert_array_equal(page.frozen_data, frozen)
        page.mode.set("原始")
        page.refresh_plot()
        indices = np.searchsorted(frozen[:, 1], page.traces[0].last[0])
        np.testing.assert_array_equal(page.traces[0].last[1], frozen[indices, 2])
        page.mode.set("滤波")
        page.refresh_plot()
        indices = np.searchsorted(frozen[:, 1], page.traces[0].last[0])
        np.testing.assert_array_equal(page.traces[0].last[1], frozen[indices, 4])
        low = page.axes.x_limits[0]
        event = self.event()
        page._drag_start(event, 0)
        moved = SimpleNamespace(**vars(event))
        moved.x_root += 50
        page._drag_move(moved)
        page._drag_end(moved)
        self.assertLess(page.axes.x_limits[0], low)
        page._wheel(self.event(state=1), 0)
        manual = page.axes.channels[0]
        self.assertFalse(manual.automatic)
        page.restore_live()
        self.assertIsNone(page.frozen_data)
        self.assertEqual(page.axes.channels[0], manual)
        self.assertAlmostEqual(page.axes.x_limits[1], 101.999)
        page._double_click(self.event(), 0)
        self.assertTrue(page.axes.channels[0].automatic)

    def test_plot_gestures_before_first_frame_do_not_freeze_preview(self):
        page = self.make_page()
        with page.controller.lock:
            page.controller.history.clear()
            page.controller.connection_id += 1
        page.refresh_plot()
        page._wheel(self.event(), 0)
        self.assertTrue(page.axes.follow, "Waiting for the MCU must not freeze an empty history")
        self.assertIsNone(page.frozen_data)
        # First real-protocol frame arrives after the user touches the plot.
        from neuromotion.acquisition import encode_frame
        frames = FrameDecoder().feed(encode_frame((i, 120 + i, -60 - i, i % 2, 0) for i in range(12)))
        page.controller._process_frame(frames[0])
        page.refresh_plot()
        self.assertEqual(page.controller.received_samples, 12)
        self.assertTrue(all(trace.surface.find_withtag("waveform") for trace in page.traces))
        self.assertFalse(page.pause.get())

    def test_click_without_drag_keeps_live_preview(self):
        page = self.make_page()
        event = self.event()
        page._drag_start(event, 0)
        page._drag_end(event)
        self.assertTrue(page.axes.follow, "Clicking a trace is not a pan gesture")
        self.assertIsNone(page.frozen_data)

    def test_stalled_input_status_keeps_received_frame_visible(self):
        page = self.make_page()
        from neuromotion.acquisition import encode_frame
        page.controller.history.clear()
        frame = FrameDecoder().feed(encode_frame((i, 120 + i, -60 - i, i % 2, 0) for i in range(12)))[0]
        page.controller._process_frame(frame)
        page.controller.state = "preview"
        page.controller.stats["raw_bytes"] = 245
        page.controller.last_byte_at = page.controller.last_sample_at = time.monotonic() - 3
        state = page.controller.snapshot()
        page.refresh_plot(state)
        page._update_stream_status(state)
        self.assertIn("停滞", page.data_status.get())
        self.assertIn("12", page.data_status.get())
        self.assertTrue(all(trace.surface.find_withtag("waveform") for trace in page.traces))
        page.controller.last_byte_at = time.monotonic()
        page._update_stream_status(page.controller.snapshot())
        self.assertIn("但仍有串口字节", page.data_status.get())
        page.controller.last_sample_at = time.monotonic()
        page._update_stream_status(page.controller.snapshot())
        self.assertIn("正在接收", page.data_status.get())
        state.update(samples=0, raw_bytes=0, source={"kind": "simulated"})
        page._update_stream_status(state)
        self.assertIn("开始记录", page.data_status.get())
        self.assertNotIn("复位", page.data_status.get())

    def test_vertical_drag_direction_linking_and_ttl_ignore(self):
        page = self.make_page()
        x = page.axes.x_limits
        original = page.last_y_ranges[0]
        event = self.event(state=1)
        page._drag_start(event, 0)
        event.y_root += 20
        page._drag_move(event)
        page._drag_end(event)
        self.assertGreater(page.axes.channels[0].minimum_uv, original[0])
        self.assertEqual(page.axes.x_limits, x)
        self.assertTrue(page.axes.follow)
        page.axes.set_linked(True, page.last_y_ranges)
        page._wheel(self.event(channel=1, state=1), 1)
        self.assertEqual(page.traces[0].current_y, page.traces[1].current_y)
        settings = deepcopy(page.axes)
        page._wheel(self.event(channel=2, state=1), 2)
        page._drag_start(self.event(channel=2, state=1), 2)
        self.assertEqual(page.axes, settings)
        self.assertIsNone(page.drag)

    def test_shared_margin_updates_atomically_when_units_change_in_history(self):
        page = self.make_page()
        page._freeze()
        page.axes.channels = [YAxisSettings(False, -650000, 550000, "uV")] * 2
        page.refresh_plot()
        old = page.traces[0].plot_rect[0]
        page.axes.channels = [YAxisSettings(False, -650000, 550000, "mV")] * 2
        page.refresh_plot()
        self.assertEqual(len({trace.plot_rect[0] for trace in page.traces}), 1)
        self.assertLess(page.traces[0].plot_rect[0], old)
        self.root.geometry("900x1000")
        self.root.update()
        self.assertEqual(len({trace.plot_rect[0] for trace in page.traces}), 1)

    def test_history_limit_is_sixty_seconds_even_with_large_sample_gaps(self):
        page = self.make_page()
        with page.controller.lock:
            page.controller.history.extend(map(tuple, history(first=200000, count=1000)))
        page.refresh_plot()
        page._freeze()
        cutoff = 200.999 - 60
        self.assertGreaterEqual(page.frozen_domain[0], cutoff)
        self.assertGreaterEqual(page.frozen_data[0, 1], cutoff)
        page.axes.set_manual_x((0, 10), page.frozen_domain)
        self.assertGreaterEqual(page.axes.x_limits[0], cutoff)

    def test_reconnect_and_mcu_epoch_clear_frozen_but_keep_y(self):
        page = self.make_page()
        page._wheel(self.event(state=1), 0)
        manual = page.axes.channels[0]
        for connection, segment in ((1, 0), (1, 1)):
            page._freeze()
            with page.controller.lock:
                page.controller.connection_id = connection
                page.controller.history.clear()
                page.controller.history.extend(map(tuple, history(count=100, segment=segment)))
            page.refresh_plot()
            self.assertIsNone(page.frozen_data)
            self.assertTrue(page.axes.follow)
            self.assertFalse(page.pause.get())
            self.assertEqual(page.axes.channels[0], manual)
            self.assertEqual(page.axes.x_limits[0], 0)
            self.assertLess(page.traces[0].last[0][-1], 1)

    def test_dialog_atomic_validation_conversions_and_unlink_restore(self):
        page = self.make_page()
        dialog = AxisSettingsDialog(page)
        original = deepcopy(page.axes)
        dialog.follow.set(False)
        dialog.begin.set("90")
        dialog.end.set("90.1")
        dialog.y_editors[0]["min"].set("bad")
        self.assertFalse(dialog.apply())
        self.assertEqual(page.axes, original)
        self.assertIsNone(page.frozen_data)
        self.assertIn("未应用", dialog.error.get())
        editor = dialog.y_editors[0]
        editor["min"].set("-2000")
        editor["max"].set("3000")
        editor["auto"].set(False)
        editor["input_unit"].set("mV")
        dialog._convert_y(editor)
        self.assertEqual(float(editor["min"].get()), -2)
        dialog.x_unit.set("ms")
        dialog._convert_x()
        self.assertEqual(float(dialog.end.get()), 90100)
        self.assertTrue(dialog.apply())
        saved = page.axes.channels.copy()
        self.assertEqual(saved[0].minimum_uv, -2000)
        self.assertAlmostEqual(np.diff(page.axes.x_limits)[0], .1)
        dialog.linked.set(True)
        dialog._toggle_link()
        self.assertTrue(dialog.apply())
        self.assertFalse(page.axes.common_y.automatic)
        self.assertLessEqual(page.axes.common_y.minimum_uv, -2000)
        dialog.linked.set(False)
        dialog._toggle_link()
        self.assertTrue(dialog.apply())
        self.assertEqual(page.axes.channels, saved)
        page.reset_axes()
        self.assertEqual(page.axes, LiveAxes(x_limits=page.axes.follow_range(99.999)))
        dialog.destroy()

    def test_interactions_during_recording_preserve_binary_csv_samples(self):
        page = self.make_page()
        page.controller.connect(source=SimulatedSource(.72, speed=1))
        page.refresh_plot()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record"
            page.controller.start_recording(path)
            deadline = time.monotonic() + 5
            while page.controller.received_samples < 24 and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.005)
            page.refresh_plot()
            page._wheel(self.event(), 0)
            frozen = page.frozen_data.copy()
            count = page.controller.record_count
            dialog = AxisSettingsDialog(page)
            self.assertTrue(dialog.apply())
            while page.active and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.005)
            page.controller.wait()
            self.assertGreater(page.controller.record_count, count)
            np.testing.assert_array_equal(page.frozen_data, frozen)
            dialog.destroy()
            page.restore_live()
            with (path / "acquisition/emg.csv").open() as stream:
                rows = list(csv.DictReader(stream))
            samples = [sample for group in FrameDecoder().feed((path / "acquisition/serial.bin").read_bytes()) for sample in group]
            self.assertEqual(len(rows), 720)
            self.assertEqual(len(samples), len(rows))
            for row, sample in zip(rows, samples):
                self.assertEqual(int(row["sample_index"]), sample.sample_index)
                self.assertEqual(int(row["sync"]), sample.sync)
                for key, value in (("ch1_raw_uv", sample.ch1), ("ch2_raw_uv", sample.ch2)):
                    self.assertEqual(struct.pack("<f", float(row[key])), struct.pack("<f", value))


class PageScrollTests(unittest.TestCase):
    def test_plot_wheel_stops_page_scroll_and_outside_wheel_still_scrolls(self):
        app = NeuroMotionApp()
        app.attributes("-alpha", 0)
        try:
            app.update()
            page = app.live_page
            scroll = app.live_tab.canvas
            scroll.yview_moveto(.4)
            app.update()
            before = scroll.yview()
            surface = page.traces[0].surface
            surface.event_generate("<MouseWheel>", delta=-120, x=100, y=30)
            app.update()
            self.assertEqual(scroll.yview(), before)
            page.window_box.event_generate("<MouseWheel>", delta=120)  # Combobox intentionally ignored by host.
            page.event_generate("<MouseWheel>", delta=120)
            app.update()
            self.assertLess(scroll.yview()[0], before[0])
        finally:
            with patch("tkinter.messagebox.askyesno", return_value=True):
                app._close()
            del app
            gc.collect()


if __name__ == "__main__":
    unittest.main()
