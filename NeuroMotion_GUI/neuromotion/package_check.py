"""Explicit --smoke-test entry for validating the built Windows application."""

from pathlib import Path
import argparse
import json
import sys
import time
import traceback

import cv2
import numpy as np
import pandas as pd

from .acquisition import FrameDecoder, SimulatedSource
from .gui import NeuroMotionApp
from .paths import app_root, resource_path
from .session import write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--serial-port", help="Optional real device; never reset or flash it")
    parser.add_argument("--seconds", type=float, default=10)
    args = parser.parse_args(argv)
    if not np.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("Duration must be positive")
    folder = args.output.resolve()
    folder.mkdir(parents=True, exist_ok=False)
    report = {"status": "running", "frozen": bool(getattr(sys, "frozen", False)),
              "app_root": str(app_root()), "real_serial": bool(args.serial_port),
              "physiological_signal_quality_validated": False}
    app = None
    try:
        # Matplotlib's native font/image modules and the OpenCV video backend
        # must really work, rather than just successfully import in the bundle.
        from matplotlib import pyplot as plt
        figure, axis = plt.subplots()
        axis.plot([0, 1, 2], [0, 1, 0])
        figure.savefig(folder / "plot.png")
        plt.close(figure)
        movie = folder / "decoder_test.avi"
        writer = cv2.VideoWriter(str(movie), cv2.VideoWriter_fourcc(*"MJPG"), 10, (64, 48))
        if not writer.isOpened():
            raise AssertionError("OpenCV video encoder is unavailable")
        try:
            for index in range(8):
                writer.write(np.full((48, 64, 3), index * 30, dtype=np.uint8))
        finally:
            writer.release()
        capture = cv2.VideoCapture(str(movie))
        decoded = 0
        try:
            while capture.read()[0]:
                decoded += 1
        finally:
            capture.release()
        if decoded != 8:
            raise AssertionError("OpenCV could not decode the generated video")
        board = json.loads(resource_path("calibration/board_spec.json").read_text(encoding="utf-8"))
        template = resource_path("config.sync-template.json")
        json.loads(template.read_text(encoding="utf-8"))
        app = NeuroMotionApp()
        app.title("NeuroMotion · 安装验证" + ("（真实串口）" if args.serial_port else "（模拟输入）"))
        errors = []
        app.report_callback_exception = lambda kind, value, tb: errors.append(f"{kind.__name__}: {value}")
        page = app.live_page
        controller = page.controller
        if Path(page.root_dir.get()) != app_root() / "sessions" or not Path(app.output.get()).is_relative_to(app_root()):
            raise AssertionError("Writable experiment paths escaped the EXE directory")
        if args.serial_port:
            controller.connect(port=args.serial_port)
        else:
            page.kind.set("模拟输入")
            controller.connect(source=SimulatedSource(.72, speed=0))
        session = folder / "session"
        controller.start_recording(session)
        started = time.monotonic()
        while controller.thread.is_alive():
            app.update()
            if errors:
                raise AssertionError(errors)
            if args.serial_port and time.monotonic() - started >= args.seconds:
                break
            if not args.serial_port and time.monotonic() - started > 20:
                raise TimeoutError("Simulated input did not finish")
            time.sleep(.01)
        controller.disconnect()
        app.update()
        page.refresh_plot()
        app.update_idletasks()
        state = controller.snapshot(include_plot=False)
        waveform_items = [len(c.surface.find_withtag("waveform")) for c in page.traces]
        frame_decoder = FrameDecoder()
        samples = [sample for group in frame_decoder.feed((session / "acquisition/serial.bin").read_bytes()) for sample in group]
        table = pd.read_csv(session / "acquisition/emg.csv")
        if len(table) != len(samples) or len(table) != state["recorded_samples"]:
            raise AssertionError("Binary and CSV sample counts disagree")
        if not args.serial_port and len(table) != 720:
            raise AssertionError("Simulation lost samples")
        if args.serial_port and len(table) < args.seconds * 950:
            raise AssertionError("Real serial input did not sustain the nominal sample rate")
        np.testing.assert_array_equal(table.sample_index, [s.sample_index for s in samples])
        np.testing.assert_array_equal(table.sync, [s.sync for s in samples])
        for key, values in (("ch1_raw_uv", [s.ch1 for s in samples]), ("ch2_raw_uv", [s.ch2 for s in samples])):
            np.testing.assert_array_equal(table[key].to_numpy(dtype=np.float32), np.asarray(values, dtype=np.float32))
        acquisition = json.loads((session / "acquisition/acquisition_report.json").read_text())
        if state["error"] or state["index_gaps"] or state["invalid_records"] or state.get("firmware_dropped_latest", 0):
            raise AssertionError("Acquisition errors or sample gaps observed")
        if frame_decoder.frame_errors or frame_decoder.invalid_records or acquisition["frame_errors"]:
            raise AssertionError("Invalid frames in the saved recording")
        if not all(waveform_items) or errors:
            raise AssertionError("Actual live plots or Tk callbacks failed")
        report.update(status="passed", saved_samples=len(table), csv_binary_equal=True, plots=waveform_items,
                      page_count=len(app.book.tabs()), video_frames=decoded, template=str(template),
                      board_spec=board, frame_errors=acquisition["frame_errors"], index_gaps=state["index_gaps"])
    except Exception:
        report.update(status="failed", error=traceback.format_exc())
    finally:
        if app is not None:
            app.live_page.shutdown()
            app.replay_page.shutdown()
            app.destroy()
        write_json(folder / "package_check.json", report)
    return 0 if report["status"] == "passed" else 1
