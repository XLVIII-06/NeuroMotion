"""Capture a reset-delimited 120-second synthetic UART benchmark."""
from pathlib import Path
import argparse
from datetime import datetime
import json
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import serial
from neuromotion.acquisition import FrameDecoder
from neuromotion.mcu_diagnostics import validate_uart_binary
from neuromotion.session import write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM5")
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    decoder = FrameDecoder()
    timings = []
    run_start = last_frame_at = None
    run_frames = 0
    previous = None
    started = time.monotonic()
    raw = bytearray()
    port = serial.Serial(args.port, 921600, timeout=.05)
    print(f"LISTENING {args.port}; reset MCU now; synthetic UART only", flush=True)
    progress_at = started - 1
    try:
        with (args.output / "serial.bin").open("wb") as stream:
            while time.monotonic() - started < args.timeout:
                data = port.read(min(8192, max(1, port.in_waiting)))
                now = time.monotonic()
                if data:
                    stream.write(data)
                    raw.extend(data)
                    for group in decoder.feed(data):
                        if len(group) != 12:
                            continue
                        first = group[0].sample_index
                        if first == 0:
                            run_start, run_frames, previous = now, 0, None
                        if run_start is not None:
                            if previous is not None and first != previous + 12:
                                run_start, run_frames = None, 0
                                continue
                            run_frames += 1
                            previous, last_frame_at = first, now
                    if run_frames >= 10000:
                        break
                if now - progress_at >= 5:
                    row = {"elapsed_s": round(now - started, 2), "bytes": len(raw),
                           "run_frames": run_frames, "last_index": previous}
                    timings.append(row)
                    print(json.dumps(row), flush=True)
                    progress_at = now
    finally:
        port.close()
    result = validate_uart_binary(bytes(raw))
    measured = None if run_start is None or last_frame_at is None else last_frame_at - run_start
    result.update(created_at=datetime.now().astimezone().isoformat(), port=args.port,
                  capture_elapsed_s=time.monotonic() - started, run_observed_duration_s=measured,
                  complete_run_frames_observed=run_frames, capture_bytes=len(raw),
                  global_frame_errors=decoder.frame_errors, global_invalid_records=decoder.invalid_records,
                  progress=timings)
    if result["status"] == "passed" and (measured is None or not 118 <= measured <= 122):
        result.update(status="failed", reason="Full run did not arrive at the required sustained cadence")
    write_json(args.output / "uart_validation.json", result)
    print(json.dumps({key: value for key, value in result.items() if key != "progress"}), flush=True)


if __name__ == "__main__":
    main()
