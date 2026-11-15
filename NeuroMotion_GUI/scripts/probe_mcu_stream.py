"""Passively capture the MCU UART without sending commands or changing firmware."""
from pathlib import Path
import argparse
import json
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import serial
from neuromotion.acquisition import FrameDecoder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM5")
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--observe-current", action="store_true",
                        help="Capture the already-running MCU; do not request another reset")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    decoder = FrameDecoder()
    counts = []
    samples = 0
    first = last = None
    begin = time.monotonic()
    port = serial.Serial(args.port, 921600, timeout=.05)
    instruction = "observing current run; no reset needed" if args.observe_current else "press MCU RESET"
    print(f"LISTENING {args.port}: {instruction}; no commands will be transmitted", flush=True)
    try:
        with (args.output / "serial.bin").open("wb") as stream:
            total_bytes, last_progress = 0, -1
            while time.monotonic() - begin < args.seconds:
                data = port.read(min(4096, max(1, port.in_waiting)))
                stream.write(data)
                total_bytes += len(data)
                for frame in decoder.feed(data):
                    samples += len(frame)
                    if frame:
                        first = frame[0].sample_index if first is None else first
                        last = frame[-1].sample_index
                elapsed = time.monotonic() - begin
                if elapsed - last_progress >= 1:
                    row = {"elapsed_s": round(elapsed, 3), "bytes": total_bytes, "samples": samples}
                    counts.append(row)
                    print(json.dumps(row), flush=True)
                    last_progress = elapsed
    finally:
        port.close()
    report = {"port": args.port, "seconds": args.seconds, "observed_current_run": args.observe_current,
              "bytes": total_bytes, "samples": samples,
              "first_index": first, "last_index": last, "valid_frames": decoder.valid_frames,
              "frame_errors": decoder.frame_errors, "invalid_records": decoder.invalid_records,
              "progress": counts}
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "progress"}), flush=True)


if __name__ == "__main__":
    main()
