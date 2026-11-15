"""30-minute-equivalent acquisition through the actual byte/parser/writer path."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import struct
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neuromotion.acquisition import AcquisitionController, FrameDecoder, SimulatedSource
from neuromotion.session import write_json


def rss_bytes():
    try:
        import psutil
        return psutil.Process().memory_info().rss
    except ImportError:
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=1800)
    args = parser.parse_args()
    root = args.output.resolve()
    controller = AcquisitionController()
    controller.connect(source=SimulatedSource(args.seconds, speed=0))
    controller.start_recording(root)
    started = time.monotonic()
    measurements = []
    while controller.thread.is_alive():
        controller.thread.join(timeout=5)
        state = controller.snapshot(max_points=500)
        measurements.append({"samples": state["recorded_samples"], "rss_bytes": rss_bytes(), "display_samples": len(controller.history)})
        print(f"Saved {state['recorded_samples']} samples; elapsed {time.monotonic() - started:.1f}s", flush=True)
    controller.wait()
    expected = controller.source.total_frames * 12
    decoder = FrameDecoder()
    count = 0
    with (root / "acquisition/emg.csv").open(newline="", encoding="utf-8") as csv_stream, (root / "acquisition/serial.bin").open("rb") as binary:
        rows = iter(csv.DictReader(csv_stream))
        for chunk in iter(lambda: binary.read(65536), b""):
            for frame in decoder.feed(chunk):
                for sample in frame:
                    row = next(rows)
                    assert int(row["sample_index"]) == sample.sample_index
                    assert int(row["sync"]) == sample.sync
                    assert struct.pack("<f", float(row["ch1_raw_uv"])) == struct.pack("<f", sample.ch1)
                    assert struct.pack("<f", float(row["ch2_raw_uv"])) == struct.pack("<f", sample.ch2)
                    count += 1
        assert next(rows, None) is None
    assert count == expected == controller.record_count
    assert len(controller.history) <= 60000
    warmed = [item["rss_bytes"] for item in measurements if item["samples"] > 60000 and item["rss_bytes"] is not None]
    if warmed:
        assert max(warmed) - min(warmed) < 96 * 1024**2
    report = {"status": "passed", "validation_kind": "accelerated_simulated_acquisition_not_real_hardware",
              "equivalent_seconds": expected / 1000, "samples": count, "elapsed_seconds": time.monotonic() - started,
              "binary_bytes": (root / "acquisition/serial.bin").stat().st_size,
              "binary_csv_raw_ttl_equal": True, "display_buffer_limit": 60000, "measurements": measurements,
              "hardware_validated": False}
    write_json(root / "long_acquisition_validation.json", report)
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
