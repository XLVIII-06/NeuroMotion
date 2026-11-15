"""30-minute indexed replay benchmark with explicit synthetic geometry/time truth."""

import argparse
import csv
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cv2
import numpy as np

from neuromotion.replay import ReplayEngine
from neuromotion.runtime import resident_bytes
from neuromotion.session import file_info, write_json
from neuromotion.validation import ttl_state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--acquisition", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Benchmark directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    csv_path = args.acquisition.resolve() / "acquisition/emg.csv"
    acquisition = json.loads((args.acquisition / "acquisition/acquisition_report.json").read_text())
    sample_count = acquisition["recorded_samples"]
    seconds = sample_count / 1000
    fps = 5
    video = output / "synthetic_long_video.avi"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), fps, (200, 120))
    if not writer.isOpened():
        raise RuntimeError("MJPG encoder is unavailable")
    count = int((seconds + 0.5) * fps)
    with (output / "frame_alignment.csv").open("w", newline="") as frames_stream, (output / "tracking.csv").open("w", newline="") as tracking_stream:
        frames = csv.writer(frames_stream)
        frames.writerow(["frame_index", "video_time_s", "common_time_s", "led_state"])
        tracks = csv.writer(tracking_stream)
        tracks.writerow(["frame_index", "particle_id", "u_px", "v_px", "X_mm", "Y_mm", "detected", "quality_flag"])
        for index in range(count):
            t = index / fps
            frame = np.zeros((120, 200, 3), np.uint8)
            # Replay benchmark supplies its mapping as ground truth; it does not validate TTL fitting.
            led = ttl_state(t) if t < 11 else int(int((t - 4) / 0.5) % 2 == 0)
            frame[:12, :12] = 255 * led
            for marker in range(5):
                x, y = 25 + marker * 32, 40 + (marker % 3) * 20
                cv2.circle(frame, (x, y), 4, (255, 255, 255), -1)
                tracks.writerow([index, marker, x, y, x / 2, y / 2, True, "observed"])
            writer.write(frame)
            frames.writerow([index, t, t - 0.5, led])
    writer.release()
    intrinsics, homography = output / "intrinsics.json", output / "homography.json"
    write_json(intrinsics, {"K": [[150, 0, 100], [0, 150, 60], [0, 0, 1]], "dist": [[0, 0, 0, 0, 0]], "image_size": [200, 120], "rms_reprojection_px": 0})
    write_json(homography, {"H": [[0.5, 0, 0], [0, 0.5, 0], [0, 0, 1]], "image_size": [200, 120], "coordinate_system": "undistorted_full_frame", "rms_reprojection_mm": 0})
    report = {"status": "full_fusion_generated", "fixture_only": True, "validation_kind": "synthetic_30min_replay_fixture_not_ttl_fit_or_large_csv_export",
              "full_fusion_validated": False, "input_files": {"emg_csv": file_info(csv_path), "video": file_info(video)},
              "calibration": {"intrinsics": file_info(intrinsics), "homography": file_info(homography)},
              "alignment": {"session_number": 0, "slope_samples_per_second": 1000.0, "intercept_samples": -500.0,
                            "fitted_start_sample": 0.0, "video_start_s": 0.5},
              "emg_sessions": [{"session_number": 0, "source_row_start": 0, "source_row_end": sample_count,
                                "sample_index_start": 0, "sample_index_end": sample_count - 1}],
              "video": {"width": 200, "height": 120, "frame_count": count}}
    write_json(output / "alignment_report.json", report)
    started = time.monotonic()
    engine = ReplayEngine(output, progress=lambda text: print(text, flush=True))
    build_seconds = time.monotonic() - started
    delays = []
    memory = [resident_bytes()]
    try:
        assert engine.emg_times.flags.c_contiguous and engine.frame_times.flags.c_contiguous
        for target in np.random.default_rng(3).uniform(engine.start, engine.end, 100):
            start = time.monotonic()
            snapshot = engine.seek(float(target))
            delays.append(time.monotonic() - start)
            assert abs(snapshot["common_time_s"] - target) <= 1 / fps
            assert len(snapshot["positions"]) == 5
            assert len(snapshot["emg_window"]) <= 5001
            memory.append(resident_bytes())
    finally:
        engine.close()
    assert float(np.median(delays)) < 1
    assert max(delays) < 3
    finite_memory = [value for value in memory if value is not None]
    if finite_memory:
        assert max(finite_memory) - min(finite_memory) < 192 * 1024**2
    result = {"status": "passed", "validation_kind": report["validation_kind"], "equivalent_seconds": seconds,
              "emg_samples": sample_count, "video_frames": count, "random_seeks": 100,
              "build_seconds": build_seconds, "median_seek_s": float(np.median(delays)), "max_seek_s": max(delays),
              "resident_memory_bytes": memory, "time_index_contiguous": True, "hardware_validated": False}
    write_json(output / "long_replay_validation.json", result)
    print(json.dumps({key: value for key, value in result.items() if key != "resident_memory_bytes"}, indent=2), flush=True)


if __name__ == "__main__":
    main()
