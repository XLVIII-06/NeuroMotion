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
from .simulated_v2 import SimulatedFourChannelSource
from .protocol_v2 import V2Decoder, SAMPLES
from .gui import NeuroMotionApp
from .paths import app_root, resource_path
from .session import write_json


def pump_until(app, stop, timeout, errors):
    """Run the normal Tk dispatcher; a live stream need never become idle."""
    deadline = time.monotonic()+timeout
    expired = False

    def poll():
        nonlocal expired
        if stop() or errors:
            app.quit()
        elif time.monotonic() >= deadline:
            expired = True
            app.quit()
        else:
            app.after(10, poll)

    app.after(0, poll)
    app.mainloop()
    if errors:
        raise AssertionError(errors)
    if expired:
        raise TimeoutError('GUI validation stage timed out')


def finish_live_view(app, page, errors):
    page.controller.disconnect()
    # update() waits for an empty queue, which a busy periodic GUI need never
    # reach. Dispatch normally and quit on a timer, as during live validation.
    settled_at = time.monotonic()+.1
    pump_until(app, lambda: time.monotonic() >= settled_at, 5, errors)
    page.refresh_plot()
    app.update_idletasks()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--serial-port", help="Optional real device; never reset or flash it")
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--channels", type=int, choices=[2,4], default=2)
    parser.add_argument('--bl-csv', type=Path, help='Optional local BL preview check')
    parser.add_argument('--replay-result', type=Path, help='Optional derived result including stimulus events')
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
            pump_until(app, lambda: controller.snapshot(include_plot=False)["valid_frames"] >= 2, 10, errors)
        else:
            page.kind.set("四通道模拟" if args.channels==4 else "模拟输入")
            controller.connect(source=SimulatedFourChannelSource(4.5,speed=0) if args.channels==4 else SimulatedSource(.72, speed=0))
        session = folder / "session"
        controller.start_recording(session)
        started = time.monotonic()
        pump_until(app, lambda: not controller.thread.is_alive() or
                   bool(args.serial_port and time.monotonic()-started >= args.seconds),
                   args.seconds+10 if args.serial_port else 20, errors)
        finish_live_view(app, page, errors)
        state = controller.snapshot(include_plot=False)
        waveform_items = [len(c.surface.find_withtag("waveform")) for c in page.traces]
        four=state.get("channel_count")==4
        frame_decoder = V2Decoder() if four else FrameDecoder()
        decoded_packets=frame_decoder.feed((session / "acquisition/serial.bin").read_bytes())
        samples = [s for p in decoded_packets if p.kind==SAMPLES for s in p.value] if four else [sample for group in decoded_packets for sample in group]
        table = pd.read_csv(session / "acquisition/emg.csv")
        if len(table) != state["recorded_samples"] or (not four and len(table)!=len(samples)):
            raise AssertionError("Binary and CSV sample counts disagree")
        if not args.serial_port and not four and len(table) != 720:
            raise AssertionError("Simulation lost samples")
        if args.serial_port and len(table) < args.seconds * 950:
            raise AssertionError("Real serial input did not sustain the nominal sample rate")
        if four:
            native=pd.read_csv(session/'acquisition/native_samples.csv')
            assert len(native)==len(samples)==state['native_recorded_samples']
            for key,values in [('device',[s.device for s in samples]),('native_index',[s.index for s in samples]),
                               ('drdy_time_us',[s.time_us for s in samples]),('ch1_code',[s.code1 for s in samples]),('ch2_code',[s.code2 for s in samples])]:
                np.testing.assert_array_equal(native[key],values)
            assert len(page.traces)==5 and len(table)>4000
            assert all(f'ch{i}_aligned_uv' in table for i in range(1,5))
            assert (np.diff(table.mcu_time_us)==1000).all()
        else:
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
                      channel_count=state.get('channel_count',2),native_records=len(samples),
                      page_count=len(app.book.tabs()), video_frames=decoded, template=str(template),
                      board_spec=board, frame_errors=acquisition["frame_errors"], index_gaps=state["index_gaps"])
        if args.bl_csv:
            from .bl_ui import preview_bl
            preview = preview_bl(args.bl_csv)['summary']
            app.bl_page.show_summary(preview)
            settled_at = time.monotonic()+.1
            pump_until(app, lambda: time.monotonic() >= settled_at, 5, errors)
            assert len(app.bl_page.table.get_children()) == preview['event_count']
            assert app.bl_page.plot.surface.find_withtag('waveform')
            report['bl_preview'] = {key: preview[key] for key in ('sample_count', 'event_count')}
        if args.replay_result:
            from .replay import ReplayEngine
            replay = app.replay_page
            replay.set_engine(ReplayEngine(args.replay_result))
            pump_until(app, lambda: replay.canvas.winfo_ismapped() and replay.canvas.winfo_height() > 100
                       and replay.positions.winfo_height() >= replay.positions.winfo_reqheight(), 8, errors)
            replay.jump_event(1)
            assert len(replay.event_table.get_children()) == len(replay.engine.events)
            assert replay.canvas.winfo_height() > 100
            assert all(trace.surface.find_withtag('waveform') for trace in replay.traces)
            report['derived_replay'] = {'channels': len(replay.traces), 'events': len(replay.engine.events),
                                        'index_schema_version': replay.engine.manifest['schema_version'],
                                        'video_size': [replay.canvas.winfo_width(), replay.canvas.winfo_height()],
                                        'position_table_height': replay.positions.winfo_height()}
    except Exception:
        report.update(status="failed", error=traceback.format_exc())
    finally:
        if app is not None:
            app.live_page.shutdown()
            app.replay_page.shutdown()
            app.destroy()
        write_json(folder / "package_check.json", report)
    return 0 if report["status"] == "passed" else 1
