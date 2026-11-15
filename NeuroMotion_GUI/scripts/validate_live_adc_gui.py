"""Validate the actual Tk client and binary/CSV recording against real ADS input.

The supplied normal acquisition image is compared before a hardware reset.
This script never flashes firmware. It tests transport and ADC continuity,
not electrode placement or physiological signal quality.
"""
from pathlib import Path
import argparse
import csv
import json
import math
import struct
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neuromotion.acquisition import FrameDecoder
from neuromotion.gui import NeuroMotionApp
from neuromotion.session import file_info, write_json
from scripts.reset_mcu_diag import reset_diagnostic


def compare_recording(session):
    """Compare each real decoded sample with the corresponding saved CSV row."""
    decoder = FrameDecoder()
    count, mismatches, missing_rows = 0, 0, 0
    previous = first = last = None
    gaps = duplicates = transitions = dropped = 0
    previous_ttl = None
    minimum, maximum = [math.inf] * 2, [-math.inf] * 2
    with (session / "acquisition/emg.csv").open(encoding="utf-8", newline="") as stream:
        rows = iter(csv.DictReader(stream))
        with (session / "acquisition/serial.bin").open("rb") as raw:
            while block := raw.read(8192):
                for frame in decoder.feed(block):
                    for sample in frame:
                        row = next(rows, None)
                        if first is None:
                            first = sample.sample_index
                        last = sample.sample_index
                        if previous is not None:
                            gaps += max(0, sample.sample_index - previous - 1)
                            duplicates += int(sample.sample_index <= previous)
                        previous = sample.sample_index
                        transitions += int(previous_ttl is not None and previous_ttl != sample.sync)
                        previous_ttl = sample.sync
                        dropped = max(dropped, sample.dropped)
                        for ch, value in enumerate((sample.ch1, sample.ch2)):
                            minimum[ch] = min(minimum[ch], value)
                            maximum[ch] = max(maximum[ch], value)
                        if row is None:
                            missing_rows += 1
                        else:
                            # CSV stores nine significant digits, sufficient to
                            # reconstruct the original float32 wire values.
                            same = (int(row["sample_index"]) == sample.sample_index and
                                    int(row["sync"]) == sample.sync and
                                    int(row["dropped_samples"]) == sample.dropped and
                                    struct.pack("<f", float(row["ch1_raw_uv"])) == struct.pack("<f", sample.ch1) and
                                    struct.pack("<f", float(row["ch2_raw_uv"])) == struct.pack("<f", sample.ch2) and
                                    math.isfinite(float(row["ch1_uv"])) and
                                    math.isfinite(float(row["ch2_uv"])))
                            mismatches += int(not same)
                        count += 1
        extra_rows = sum(1 for _ in rows)
    return {"decoded_samples": count, "csv_rows": count - missing_rows + extra_rows,
            "csv_mismatches": mismatches, "missing_csv_rows": missing_rows, "extra_csv_rows": extra_rows,
            "first_sample_index": first, "last_sample_index": last,
            "binary_frame_errors": decoder.frame_errors, "binary_invalid_records": decoder.invalid_records,
            "trailing_fragment_bytes": len(decoder.buffer), "index_gaps": gaps, "duplicates_or_resets": duplicates,
            "firmware_dropped_latest": dropped, "ttl_transitions": transitions,
            "channel_min_uv": minimum if count else None, "channel_max_uv": maximum if count else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hex", type=Path, required=True)
    parser.add_argument("--port", default="COM5")
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds < 10:
        parser.error("Use at least ten seconds of continuous input")
    args.output.mkdir(parents=True, exist_ok=False)
    errors, progress, actions = [], [], []
    app = NeuroMotionApp()
    app.title("NeuroMotion - real ADS acquisition validation")
    page, finished = app.live_page, False
    page.port.set(args.port)
    page.mode.set("原始")
    controller = page.controller
    session = args.output / "adc_session"
    first_at = last_at = None
    first_count = last_count = 0
    previous = 0
    progress_at = 0
    started = time.monotonic()
    ui_drawn = False
    freeze_count = restore_count = None

    def callback_error(kind, value, trace):
        errors.append(f"Tk callback: {kind.__name__}: {value}")

    app.report_callback_exception = callback_error

    def poll():
        nonlocal finished, first_at, last_at, first_count, last_count
        nonlocal previous, progress_at, ui_drawn, freeze_count, restore_count
        if finished:
            return
        now = time.monotonic()
        state = controller.snapshot(include_plot=False)
        count = state["recorded_samples"]
        if count != previous:
            if first_at is None:
                first_at, first_count = now, count
            last_at, last_count, previous = now, count, count
        duration = 0 if first_at is None else now - first_at
        if count and not actions and duration > 2:
            if not page._freeze():
                errors.append("Could not freeze the live data")
            freeze_count = count
            actions.append("freeze history while recording")
        elif len(actions) == 1 and duration > 4:
            restore_count = count
            page.restore_live()
            if count <= freeze_count or page.pause.get() or page.frozen_data is not None:
                errors.append("Recording stopped during freeze or live view did not restore")
            actions.append("restore real-time")
        elif len(actions) == 2 and duration > 5:
            page.axes.window_s = 2.0
            page._sync_view_controls()
            page.refresh_plot()
            actions.append("change display window to2s")
        elif len(actions) == 3 and duration > 6:
            page.open_axis_settings()
            actions.append("open axis settings while recording")
        elif len(actions) == 4 and duration > 8:
            page.axis_dialog.destroy()
            actions.append("close axis settings")
        waveforms = [len(c.surface.find_withtag("waveform")) for c in page.traces]
        ui_drawn |= all(waveforms)
        if now - progress_at >= 5:
            row = {"elapsed_s": round(now - started, 2), "input_duration_s": round(duration, 2),
                   "samples": state["samples"], "saved": count, "frame_errors": state["frame_errors"],
                   "gaps": state["index_gaps"], "firmware_dropped": state.get("firmware_dropped_latest", 0),
                   "start_marker": state["start_marker"], "waveform_items": waveforms,
                   "paused": page.pause.get(), "following": page.axes.follow}
            progress.append(row)
            write_json(args.output / "progress.json", {"real_serial": True, "progress": progress})
            print(json.dumps(row), flush=True)
            progress_at = now
        if state["error"]:
            errors.append(state["error"])
        if first_at is None and now - started > 15:
            errors.append("No real ADC samples within fifteen seconds after reset")
        elif last_at is not None and now - last_at > 2:
            errors.append("Real ADC input stalled for more than two seconds")
        if errors or duration >= args.seconds:
            finished = True
            app.quit()
        else:
            app.after(100, poll)

    with patch("tkinter.messagebox.showerror", side_effect=lambda *a, **kw: errors.append(str(a))):
        try:
            # Reset before opening COM5. The normal firmware waits five
            # seconds before starting, so the recorder can capture index0.
            reset_diagnostic(args.hex, args.output)
            controller.connect(port=args.port)
            controller.start_recording(session)
            started = time.monotonic()
            print("Verified normal firmware reset; actual GUI recording COM5 ADS data", flush=True)
            app.after(100, poll)
            app.protocol("WM_DELETE_WINDOW", lambda: (errors.append("test window closed early"), app.quit()))
            app.mainloop()
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            controller.disconnect()
            final = controller.snapshot(include_plot=False)
            page.shutdown()
            app.replay_page.shutdown()
            app.destroy()

    result = compare_recording(session) if (session / "acquisition/emg.csv").exists() else {}
    acquisition_report = session / "acquisition/acquisition_report.json"
    if acquisition_report.exists():
        recording = json.loads(acquisition_report.read_text(encoding="utf-8"))
        result["recording_frame_errors"] = recording["frame_errors"]
        result["connection_frame_errors"] = final["frame_errors"]
        result["recording_valid_frames"] = recording["valid_frames"]
    arrival_duration = None if first_at is None else last_at - first_at
    rate = None if not arrival_duration else (last_count - first_count) / arrival_duration
    bad_fields = ("csv_mismatches", "missing_csv_rows", "extra_csv_rows", "recording_frame_errors", "binary_frame_errors",
                  "binary_invalid_records", "index_gaps", "duplicates_or_resets", "firmware_dropped_latest")
    if any(result.get(key) for key in bad_fields):
        errors.append("Binary/CSV sample consistency or continuity check failed")
    if result.get("decoded_samples") != final["recorded_samples"] or final["recorded_samples"] < args.seconds * 990:
        errors.append("Saved sample count is missing or below expected duration")
    if result.get("first_sample_index") != 0 or final["start_marker"] != "verified":
        errors.append("Start index0 or Barker start marker missing")
    if rate is None or not 990 <= rate <= 1010:
        errors.append(f"Arrival rate outside1000+/-1%: {rate}")
    if not ui_drawn or len(actions) != 5 or restore_count is None or restore_count <= freeze_count:
        errors.append("Live plot or interaction checks incomplete")
    result.update(status="failed" if errors else "passed", errors=errors, gui_waveforms_drawn=ui_drawn,
                  saved_samples=final["recorded_samples"], start_marker=final["start_marker"],
                  frame_errors=final["frame_errors"], invalid_records=final["invalid_records"],
                  display_actions=actions, frozen_at_samples=freeze_count, restored_at_samples=restore_count,
                  arrival_duration_s=arrival_duration, measured_arrival_rate_sps=rate, requested_duration_s=args.seconds,
                  real_adc_validated=not errors, physiological_signal_quality_validated=False,
                  firmware=file_info(args.hex), synthetic_uart_only=False)
    if session.exists():
        manifest_path = session / "session.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(synthetic_uart_only=False, validation_report="../gui_adc_validation.json",
                        hardware_transport_validated=not errors)
        write_json(manifest_path, manifest)
    write_json(args.output / "gui_adc_validation.json", result)
    print(json.dumps(result), flush=True)
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
