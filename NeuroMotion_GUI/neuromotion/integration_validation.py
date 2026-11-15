"""Persistent acceptance for acquisition + independent video + alignment + replay."""

from dataclasses import replace
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import cv2
import numpy as np
import pandas as pd

from .acquisition import AcquisitionController, BinaryReplaySource, encode_frame
from .pipeline import RunConfig, run_pipeline
from .replay import ReplayEngine
from .review import prepare_review
from .session import file_info, write_json
from .validation import make_synthetic_inputs, ttl_state
from .video import Rect
from .video_analysis import VideoAnalysisConfig, analyze_video


def run_integration_validation(output: str | Path, progress=print) -> dict:
    root = Path(output).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("Integration validation output must be empty")
    root.mkdir(parents=True, exist_ok=True)
    project = Path(__file__).resolve().parents[1]
    summary = {"status": "running", "hardware_validation": "pending_user_hardware", "cases": {}}
    try:
        progress("运行全部回归和客户端流程测试")
        tests = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=project,
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
        (root / "tests.log").write_text(tests.stdout + tests.stderr, encoding="utf-8")
        if tests.returncode:
            raise AssertionError("Tests failed; inspect tests.log")
        summary["tests"] = "passed"
        configs = []
        for name, fps, rate, gaps in (("fps30", 30, 1000.8, False), ("fps5994_gaps", 59.94, 999.4, True)):
            progress(f"验证正式采集路径到同步回放：{name}")
            fixture = make_synthetic_inputs(root / name / "inputs", fps, rate, gaps)
            native = fixture.emg_csv.parent / "synthetic_serial.bin"
            with fixture.emg_csv.open(newline="", encoding="utf-8") as stream, native.open("wb") as output_stream:
                rows = []
                last_index = -1
                for row in csv.DictReader(stream):
                    last_index = int(row["sample_index"])
                    rows.append((last_index, float(row["ch1_raw_uv"]), float(row["ch2_raw_uv"]), int(row["sync"]), 0))
                    if len(rows) == 12:
                        output_stream.write(encode_frame(rows))
                        rows.clear()
                if rows:
                    while len(rows) < 12:
                        last_index += 1
                        t = 0.51 + last_index / rate
                        rows.append((last_index, 10 * np.sin(t), 5 * np.cos(t), ttl_state(t), 0))
                    output_stream.write(encode_frame(rows))
            controller = AcquisitionController()
            controller.connect(source=BinaryReplaySource(native, speed=0))
            session_dir = root / name / "session"
            controller.start_recording(session_dir)
            controller.wait()
            analysis = root / name / "video_analysis"
            analyze_video(VideoAnalysisConfig(fixture.video_path, analysis, fixture.led_roi, fixture.marker_roi, 5,
                                              fixture.intrinsics_path, fixture.homography_path, led_metric="mean", marker_threshold=180), progress)
            config = RunConfig(session_dir / "acquisition/emg.csv", fixture.video_path, root / name / "aligned",
                               fixture.led_roi, video_analysis_dir=analysis)
            configs.append(config)
            report = run_pipeline(config, progress)
            frame = pd.read_csv(config.output_dir / "aligned_by_frame.csv")
            sample = pd.read_csv(config.output_dir / "aligned_by_emg_sample.csv")
            inferred_video = frame.emg_sample_float / rate + 0.51
            error = float(np.max(np.abs(inferred_video - frame.video_time_s)))
            if error > 1 / fps:
                raise AssertionError("Actual acquisition CSV mapping exceeds one frame")
            if not sample.ch1_raw_uv.notna().all() or not sample.ch1_filtered_uv.notna().all():
                raise AssertionError("Raw/filtered branches were lost")
            if len(sample) != controller.record_count * 5:
                raise AssertionError("Acquisition-to-fusion sample count differs")
            replay = ReplayEngine(config.output_dir, progress)
            checked_frames = []
            try:
                for t in (1.0, 4.0, 8.0, 12.0):
                    snapshot = replay.seek(t)
                    if abs(snapshot["common_time_s"] - t) > 1 / fps or len(snapshot["positions"]) != 5:
                        raise AssertionError("Replay time/positions mismatch")
                    checked_frames.append(snapshot["frame_index"])
                cv2.imencode(".jpg", snapshot["image"])[1].tofile(config.output_dir / "replay_preview.jpg")
            finally:
                replay.close()
            review = prepare_review(config.output_dir)
            summary["cases"][name] = {"passed": True, "source_kind": "binary_replay_of_explicit_synthetic_fixture",
                                      "acquired_samples": controller.record_count, "sample_rows": len(sample), "frame_rows": len(frame),
                                      "max_mapping_error_s": error, "frame_interval_s": 1 / fps,
                                      "raw_and_filtered_preserved": True, "replay_checked_frames": checked_frames,
                                      "review_frames": review["frame_count"], "human_review": "pending", "hardware_validated": False}
        repeat = replace(configs[0], output_dir=root / "repeat")
        run_pipeline(repeat, progress)
        names = ("tracking.csv", "video_ttl.csv", "frame_alignment.csv", "ttl_edge_matches.csv", "aligned_by_frame.csv", "aligned_by_emg_sample.csv")
        for name in names:
            if file_info(configs[0].output_dir / name)["sha256"] != file_info(repeat.output_dir / name)["sha256"]:
                raise AssertionError("Repeated integration CSV differs")
        summary["repeat_csv_equal"] = list(names)
        sample_root = project.parent.parent / "NeuroMotion_demo_module"
        real = run_pipeline(RunConfig(sample_root / "record_20260721_231912.csv", sample_root / "VID_20260721_231923.mp4",
                                       root / "real_sync", Rect(350, 850, 250, 250), sync_only=True), progress)
        summary["real_sync"] = real["alignment"]
        summary["source_sha256"] = {str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted((project / "neuromotion").glob("*.py"))}
        long_root = project / "output/integration_20261104"
        for key, relative in (("long_acquisition", "acquisition_30min/long_acquisition_validation.json"), ("long_replay", "replay_30min/long_replay_validation.json")):
            path = long_root / relative
            summary[key] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"status": "not_run"}
        summary["status"] = "software_passed_hardware_pending"
    except Exception as exc:
        summary.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        write_json(root / "integration_summary.json", summary)
    return summary
