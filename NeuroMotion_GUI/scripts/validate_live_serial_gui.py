"""Exercise the actual Tk client against the finite MCU synthetic UART mode.

The serial transport is real. Values are synthetic diagnostic data, not ADS EMG.
The matching D_UART image must be explicitly supplied and is verified before
the reset; this script never downloads a firmware image.
"""
from pathlib import Path
import argparse
import csv
import json
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neuromotion.gui import NeuroMotionApp
from neuromotion.mcu_diagnostics import validate_uart_binary
from neuromotion.session import write_json
from scripts.reset_mcu_diag import reset_diagnostic


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hex", type=Path, required=True)
    parser.add_argument("--port", default="COM5")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    errors, progress, actions = [], [], []
    app = NeuroMotionApp()
    app.title("NeuroMotion - MCU synthetic UART test (not ADS EMG)")
    page = app.live_page
    page.port.set(args.port)
    page.mode.set("原始")
    started, progress_at = time.monotonic(), 0.0
    finished = False
    first_data_at = last_data_at = None
    previous_samples = 0
    reset_done = threading.Event()
    controller = page.controller

    def reset_worker():
        try:
            reset_diagnostic(args.hex, args.output)
        except Exception as exc:
            errors.append(f"reset: {type(exc).__name__}: {exc}")
        finally:
            reset_done.set()

    def callback_error(kind, value, trace):
        errors.append(f"Tk callback: {kind.__name__}: {value}")

    app.report_callback_exception = callback_error
    controller.connect(port=args.port)
    controller.start_recording(args.output / "synthetic_session")
    print("GUI recording opened; resetting verified D_UART; synthetic values only", flush=True)
    threading.Thread(target=reset_worker, daemon=True).start()

    def poll():
        nonlocal finished, progress_at, first_data_at, last_data_at, previous_samples
        if finished:
            return
        now = time.monotonic()
        state = controller.snapshot(include_plot=False)
        count = state["samples"]
        if count != previous_samples:
            if first_data_at is None:
                first_data_at = now
            last_data_at = now
            previous_samples = count
        elapsed = now - started
        if count > 10000 and not actions:
            page._freeze()
            actions.append("freeze history while recording")
        elif count > 15000 and len(actions) == 1:
            page.restore_live()
            actions.append("restore real-time")
        elif count > 20000 and len(actions) == 2:
            page.axes.window_s = 2.0
            page._sync_view_controls()
            page.refresh_plot()
            actions.append("change display window to2s")
        if now - progress_at >= 5:
            row = {"elapsed_s": round(elapsed, 2), "samples": count,
                   "saved": state["recorded_samples"], "frame_errors": state["frame_errors"],
                   "gaps": state["index_gaps"],
                   "waveform_items": [len(c.surface.find_withtag("waveform")) for c in page.traces],
                   "ui_status": page.data_status.get()}
            progress.append(row)
            write_json(args.output / "progress.json", {"synthetic_uart_only": True, "progress": progress})
            print(json.dumps({k: v for k, v in row.items() if k != "ui_status"}), flush=True)
            progress_at = now
        if state["error"]:
            errors.append(state["error"])
        if count >= 120000 or errors or elapsed > 150:
            finished = True
            app.quit()
            return
        app.after(100, poll)

    app.after(100, poll)
    app.protocol("WM_DELETE_WINDOW", lambda: (errors.append("test window closed early"), app.quit()))
    with patch("tkinter.messagebox.showerror", side_effect=lambda *a, **kw: errors.append(str(a))):
        try:
            app.mainloop()
        finally:
            controller.disconnect()
            final = controller.snapshot(include_plot=False)
            app.live_page.shutdown()
            app.replay_page.shutdown()
            app.destroy()
    session = args.output / "synthetic_session"
    raw = (session / "acquisition/serial.bin").read_bytes()
    result = validate_uart_binary(raw)
    csv_rows = 0
    csv_exact = True
    with (session / "acquisition/emg.csv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            i = csv_rows
            expected = (i % 1000 - 500) * .25
            csv_exact &= (int(row["sample_index"]) == i and float(row["ch1_raw_uv"]) == expected and
                          float(row["ch2_raw_uv"]) == -expected and int(row["sync"]) == (i // 500) % 2)
            csv_rows += 1
    ui_drawn = any(all(row["waveform_items"]) for row in progress)
    manifest_path = session / "session.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(synthetic_uart_only=True, diagnostic_firmware="D_UART", hardware_validated=False)
    write_json(manifest_path, manifest)
    result.update(errors=errors, gui_waveforms_drawn=ui_drawn, csv_exact=bool(csv_exact), csv_rows=csv_rows,
                  saved_samples=final["recorded_samples"], frame_errors=final["frame_errors"],
                  index_gaps=final["index_gaps"], display_actions=actions,
                  arrival_duration_s=None if first_data_at is None else last_data_at-first_data_at,
                  synthetic_uart_only=True, real_adc_validated=False)
    if errors or not ui_drawn or not csv_exact or csv_rows != 120000 or final["index_gaps"] or final["frame_errors"]:
        result["status"] = "failed"
    write_json(args.output / "gui_uart_validation.json", result)
    print(json.dumps(result), flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
