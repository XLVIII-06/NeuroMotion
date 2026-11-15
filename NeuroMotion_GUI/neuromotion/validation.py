"""Reproducible software acceptance with persistent inputs and ground truth."""

from __future__ import annotations

import csv
import gc
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys
import tracemalloc
from unittest.mock import patch

import cv2
import numpy as np
import pandas as pd

from .pipeline import RunConfig, run_pipeline
from .review import prepare_review
from .video import Rect, scan_video_led


def ttl_state(t: float) -> int:
    if t < 0.5:
        return 0
    if t < 4:
        return (1, 1, 1, 0, 0, 1, 0)[min(6, int((t - 0.5) / 0.5))]
    if t < 11:
        return int(int((t - 4) / 0.5) % 2 == 0)
    if t < 14.5:
        return (0, 0, 0, 1, 1, 0, 1)[min(6, int((t - 11) / 0.5))]
    return 0


def marker_position(marker: int, t: float) -> tuple[float, float]:
    return 30 + 35 * marker + (1 + marker * 0.15) * t, 40 + 20 * (marker % 3)


def make_synthetic_inputs(root: Path, fps: float, actual_rate: float, occlusion: bool = False) -> RunConfig:
    root.mkdir(parents=True, exist_ok=True)
    emg = root / "emg.csv"
    with emg.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["sample_index", "time_sec", "ch1_raw_uv", "ch2_raw_uv", "ch1_uv", "ch2_uv", "sync", "dropped_samples", "sample_rate_hz", "high_pass_hz", "low_pass_hz", "notch_50hz"])
        for index in range(int((15 - 0.51) * actual_rate)):
            if occlusion and 5000 <= index < 5040:
                continue
            t = 0.51 + index / actual_rate
            writer.writerow([index, index / 1000, 10 * np.sin(t), 5 * np.cos(t), 0, 0, ttl_state(t), 0, 1000, 10, 100, 1])
    video = root / "video.avi"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), fps, (240, 140))
    if not writer.isOpened():
        raise RuntimeError("MJPG encoder is unavailable")
    truth = []
    try:
        for frame_index in range(int(15 * fps)):
            t = frame_index / fps
            frame = np.zeros((140, 240, 3), dtype=np.uint8)
            frame[:15, :15] = 255 * ttl_state(t)
            for marker in range(5):
                x, y = marker_position(marker, t)
                visible = not (occlusion and ((marker == 2 and 5 <= t < 5.034) or (marker == 4 and 7 <= t < 7.3)))
                if visible:
                    cv2.circle(frame, (round(x), round(y)), 4, (255, 255, 255), -1)
                truth.append({"frame_index": frame_index, "particle_id": marker, "u_px": x, "v_px": y, "X_mm": x / 2, "Y_mm": y / 2, "visible": visible})
            writer.write(frame)
    finally:
        writer.release()
    pd.DataFrame(truth).to_csv(root / "ground_truth_tracking.csv", index=False)
    (root / "ground_truth.json").write_text(json.dumps({"synthetic": True, "fps": fps, "actual_rate_hz": actual_rate,
                                                        "video_start_s": 0.5, "sample_zero_video_s": 0.51,
                                                        "marker_count": 5, "occlusion": occlusion}, indent=2), encoding="utf-8")
    intrinsics, homography = root / "intrinsics.json", root / "homography.json"
    intrinsics.write_text(json.dumps({"K": [[200, 0, 120], [0, 200, 70], [0, 0, 1]], "dist": [[0, 0, 0, 0, 0]], "image_size": [240, 140], "rms_reprojection_px": 0}), encoding="utf-8")
    homography.write_text(json.dumps({"H": [[0.5, 0, 0], [0, 0.5, 0], [0, 0, 1]], "image_size": [240, 140], "coordinate_system": "undistorted_full_frame", "rms_reprojection_mm": 0}), encoding="utf-8")
    return RunConfig(emg_csv=emg, video_path=video, output_dir=root / "result", led_roi=Rect(0, 0, 15, 15),
                     marker_roi=Rect(20, 20, 215, 110), marker_count=5, intrinsics_path=intrinsics,
                     homography_path=homography, led_metric="mean", marker_threshold=180)


def check_synthetic_result(config: RunConfig) -> dict:
    output = config.output_dir
    truth = json.loads((config.emg_csv.parent / "ground_truth.json").read_text())
    report = json.loads((output / "alignment_report.json").read_text(encoding="utf-8"))
    frame = pd.read_csv(output / "aligned_by_frame.csv")
    sample = pd.read_csv(output / "aligned_by_emg_sample.csv")
    expected = pd.read_csv(config.emg_csv.parent / "ground_truth_tracking.csv")
    comparison = frame.merge(expected, on=["frame_index", "particle_id"], suffixes=("", "_truth"))
    detected = comparison[comparison.detected]
    pixel_error = np.hypot(detected.u_px - detected.u_px_truth, detected.v_px - detected.v_px_truth)
    mm_error = np.hypot(detected.X_mm - detected.X_mm_truth, detected.Y_mm - detected.Y_mm_truth)
    inferred_video = frame.emg_sample_float / truth["actual_rate_hz"] + truth["sample_zero_video_s"]
    max_time_error = float(np.max(np.abs(inferred_video - frame.video_time_s)))
    if max_time_error > 1 / truth["fps"] or pixel_error.max() > 1.2 or mm_error.max() > 0.6:
        raise AssertionError("Synthetic time/position mapping exceeds ground truth tolerance")
    if len(frame) != len(expected) or len(sample) != len(pd.read_csv(config.emg_csv)) * 5:
        raise AssertionError("Fusion row count is incorrect")
    for _, rows in frame.groupby("particle_id"):
        if not np.all(np.diff(rows.common_time_s) > 0) or not np.all(np.diff(rows.frame_index) == 1):
            raise AssertionError("Frame timeline is not monotonic/continuous")
    for _, rows in sample.groupby("particle_id"):
        if not np.all(np.diff(rows.common_time_s) > 0) or not np.all(np.diff(rows.sample_index) > 0):
            raise AssertionError("Sample timeline is not monotonic")
    if not np.allclose(sample.ch1_uv, 10 * np.sin(sample.sample_index / truth["actual_rate_hz"] + 0.51), atol=1e-10):
        raise AssertionError("Raw signal values were replaced by filtered values")
    if not sample[sample.motion_value_type == "missing"].u_px.isna().all():
        raise AssertionError("Missing motion values were silently filled")
    outside = (sample.common_time_s < frame.common_time_s.min()) | (sample.common_time_s > frame.common_time_s.max())
    if not sample.loc[outside, "u_px"].isna().all():
        raise AssertionError("Motion positions were extrapolated outside video coverage")
    if truth["occlusion"]:
        if comparison.detected.all() or not (frame.emg_value_type == "emg_gap").any():
            raise AssertionError("Synthetic missing samples/markers were not preserved")
        late = sample[(sample.particle_id == 4) & (sample.common_time_s > 7)]
        if not (late.quality_flag == "reacquired_unverified").any():
            raise AssertionError("Lost marker identity was not flagged after reacquisition")
    return {"passed": True, "marker_count": 5, "frame_rows": len(frame), "sample_rows": len(sample),
            "max_mapping_error_s": max_time_error, "frame_interval_s": 1 / truth["fps"],
            "max_pixel_error": float(pixel_error.max()), "max_mm_error": float(mm_error.max()),
            "missing_motion_rows": int((sample.motion_value_type == "missing").sum()),
            "full_fusion_validated": report["full_fusion_validated"]}


def check_video_metadata_memory(root: Path) -> dict:
    class Capture:
        def __init__(self, count):
            self.count, self.index = count, 0
        def get(self, prop):
            return {cv2.CAP_PROP_FRAME_WIDTH: 12, cv2.CAP_PROP_FRAME_HEIGHT: 12, cv2.CAP_PROP_FPS: 60,
                    cv2.CAP_PROP_FRAME_COUNT: self.count, cv2.CAP_PROP_POS_MSEC: self.index * 1000 / 60}.get(prop, 0)
        def read(self):
            self.index += 1
            return (True, np.full((12, 12, 3), 255 if (self.index // 30) % 2 else 0, np.uint8)) if self.index <= self.count else (False, None)
        def release(self):
            pass
    measurements = []
    for count in (1000, 20000):
        folder = root / str(count)
        folder.mkdir(parents=True)
        gc.collect()
        tracemalloc.start()
        trace = None
        try:
            with patch("neuromotion.video._open_video", return_value=Capture(count)):
                trace = scan_video_led("synthetic", Rect(0, 0, 12, 12), metric="mean", storage_dir=folder)
            _, peak = tracemalloc.get_traced_memory()
            measurements.append({"frames": count, "peak_python_heap_bytes": peak, "disk_metadata_bytes": (folder / "led.bin").stat().st_size})
        finally:
            tracemalloc.stop()
            if trace is not None:
                trace.close()
    increase = measurements[1]["peak_python_heap_bytes"] - measurements[0]["peak_python_heap_bytes"]
    if increase > 3 * 1024**2:
        raise AssertionError("Metadata heap usage grows beyond the fixed chunk allowance")
    return {"passed": True, "measurements": measurements, "heap_increase_bytes": increase,
            "scope": "Python metadata heap; excludes decoder native allocations, OS mapped-file cache, and in-memory EMG"}


def run_validation_suite(output: str | Path, progress=print) -> dict:
    root = Path(output).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("Validation output must be empty")
    root.mkdir(parents=True, exist_ok=True)
    project = Path(__file__).resolve().parents[1]
    summary = {"status": "running", "real_full_fusion": "waiting_for_recording_and_calibration", "stages": {}}
    try:
        progress("运行回归与拒绝输入测试")
        tests = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=project,
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
        (root / "tests.log").write_text(tests.stdout + tests.stderr, encoding="utf-8")
        if tests.returncode:
            raise AssertionError("Regression tests failed; see tests.log")
        summary["regression_tests"] = "passed"
        summary["environment"] = {"python": sys.version, "platform": platform.platform(),
                                  "packages": {name: importlib.metadata.version(name) for name in ("numpy", "pandas", "opencv-python", "scipy", "matplotlib")}}
        summary["source_sha256"] = {str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted((project / "neuromotion").glob("*.py"))}
        progress("验证磁盘暂存与固定大小元数据缓冲")
        summary["memory"] = check_video_metadata_memory(root / "memory")
        summary["synthetic"] = {}
        configs = []
        for name, fps, rate, occlusion in (("fps30_drift", 30.0, 1000.8, False), ("fps5994_gaps", 59.94, 999.4, True)):
            progress(f"验证五反光点合成全链路：{name}")
            config = make_synthetic_inputs(root / name, fps, rate, occlusion)
            configs.append(config)
            run_pipeline(config, progress=progress)
            summary["synthetic"][name] = check_synthetic_result(config)
            summary["synthetic"][name]["review_pack"] = prepare_review(config.output_dir)
        from dataclasses import replace
        repeat = replace(configs[0], output_dir=root / "repeat_result")
        run_pipeline(repeat, progress=progress)
        names = ("video_ttl.csv", "ttl_edge_matches.csv", "frame_alignment.csv", "tracking.csv", "aligned_by_frame.csv", "aligned_by_emg_sample.csv")
        if any(hashlib.sha256((configs[0].output_dir / name).read_bytes()).digest() != hashlib.sha256((repeat.output_dir / name).read_bytes()).digest() for name in names):
            raise AssertionError("Repeated run produced different CSV output")
        summary["reproducibility"] = {"passed": True, "identical_csv_files": list(names)}
        progress("重新验证用户提供的真实 LED/TTL 配对样例")
        values = json.loads((project / "config.sync-example.json").read_text(encoding="utf-8"))
        real = run_pipeline(RunConfig(emg_csv=(project / values["emg_csv"]).resolve(), video_path=(project / values["video_path"]).resolve(),
                                      output_dir=root / "real_sync", led_roi=Rect(**values["led_roi"]), sync_only=True), progress=progress)
        summary["real_sync"] = real["alignment"]
        summary["stages"] = {"0_protocol_and_quality": "software_and_real_sync_verified", "1_input_and_streaming": "software_verified",
                             "2_ttl_alignment": "software_and_real_sync_verified", "3_fusion_and_qc": "synthetic_verified_real_pending"}
        summary["status"] = "software_validation_passed_real_full_pending"
    except Exception as exc:
        summary["status"] = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        (root / "validation_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary
