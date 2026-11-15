from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
import pandas as pd

from neuromotion.pipeline import RunConfig, run_pipeline
from neuromotion.calibration import calibrate_plane
from neuromotion.signals import load_signal_sessions
from neuromotion.timeline import AlignmentError, fit_session_alignment
from neuromotion.video import Rect


def state_at(time_s: float) -> int:
    start = 0.5
    stable = 4.0
    ending = 11.0
    if time_s < start:
        return 0
    if time_s < stable:
        return (1, 1, 1, 0, 0, 1, 0)[min(6, int((time_s - start) / 0.5))]
    if time_s < ending:
        return 1 if int((time_s - stable) / 0.5) % 2 == 0 else 0
    if time_s < ending + 3.5:
        return (0, 0, 0, 1, 1, 0, 1)[min(6, int((time_s - ending) / 0.5))]
    return 0


class SignalAndTimelineTests(unittest.TestCase):
    def test_csv_reset_creates_two_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.csv"
            path.write_text(
                "sampleIndex,ch1,ch2,sync,dropped_samples\n"
                "10,1,2,0,0\n11,2,3,1,0\n0,3,4,1,0\n1,4,5,0,0\n",
                encoding="utf-8",
            )
            sessions = load_signal_sessions(path)
        self.assertEqual([len(item.sample_index) for item in sessions], [2, 2])
        self.assertEqual(sessions[1].signal_kind, "winforms_filtered_or_unspecified")

    def test_infers_start_when_emg_begins_high(self):
        video_times = np.arange(0, 15, 1 / 60)
        video_states = np.asarray([state_at(t) for t in video_times], dtype=np.int8)
        emg_times = np.arange(0.51, 15, 0.001)
        emg_states = np.asarray([state_at(t) for t in emg_times], dtype=np.int8)
        indices = np.arange(len(emg_times))
        alignment = fit_session_alignment(video_states, video_times, emg_states, indices, session_number=0)
        self.assertEqual(alignment.session_number, 0)
        self.assertTrue(alignment.end_marker_verified)
        self.assertGreaterEqual(alignment.matched_edges, 10)
        self.assertLess(abs(alignment.slope_samples_per_second - 1000), 3)
        self.assertLess(alignment.rms_residual_s, 1 / 60)

    def test_missing_start_is_rejected(self):
        times = np.arange(4.0, 11.0, 1 / 60)
        states = np.asarray([state_at(t) for t in times], dtype=np.int8)
        emg_times = np.arange(4.0, 11.0, 0.001)
        emg_states = np.asarray([state_at(t) for t in emg_times], dtype=np.int8)
        with self.assertRaisesRegex(AlignmentError, "start Barker"):
            fit_session_alignment(states, times, emg_states, np.arange(len(emg_states)), session_number=0)

    def test_optional_end_and_extra_edge_rejection(self):
        video_times = np.arange(0, 10.5, 1 / 60)
        video_states = np.asarray([state_at(t) for t in video_times], dtype=np.int8)
        emg_times = np.arange(0.51, 10.5, 0.001)
        emg_states = np.asarray([state_at(t) for t in emg_times], dtype=np.int8)
        indices = np.arange(len(emg_states))
        alignment = fit_session_alignment(video_states, video_times, emg_states, indices, session_number=0)
        self.assertFalse(alignment.end_marker_verified)
        corrupted = video_states.copy()
        corrupted[420:426] = 1 - corrupted[420:426]
        with self.assertRaisesRegex(AlignmentError, "edge count|polarity|fit exceeds|intervals"):
            fit_session_alignment(corrupted, video_times, emg_states, indices, session_number=0)


class FullPipelineTests(unittest.TestCase):
    def _make_input(self, root: Path) -> tuple[Path, Path, Path, Path]:
        csv_path = root / "emg.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["sampleIndex", "ch1", "ch2", "sync", "dropped_samples"])
            for index in range(14990):
                time_s = 0.51 + index / 1000
                writer.writerow([index, 10 + index / 1000, -5 - index / 2000, state_at(time_s), 0])
        video_path = root / "video.avi"
        writer = cv2.VideoWriter(str(video_path), cv2.VideoWriter_fourcc(*"MJPG"), 60.0, (160, 100))
        self.assertTrue(writer.isOpened())
        for frame_index in range(900):
            time_s = frame_index / 60
            frame = np.zeros((100, 160, 3), dtype=np.uint8)
            if state_at(time_s):
                frame[:15, :15] = 255
            cv2.circle(frame, (round(35 + time_s * 2), 55), 4, (255, 255, 255), -1)
            cv2.circle(frame, (round(125 - time_s), 75), 4, (255, 255, 255), -1)
            writer.write(frame)
        writer.release()
        intrinsics = root / "intrinsics.json"
        intrinsics.write_text(json.dumps({
            "K": [[100, 0, 80], [0, 100, 50], [0, 0, 1]],
            "dist": [[0, 0, 0, 0, 0]], "image_size": [160, 100],
            "rms_reprojection_px": 0.0,
        }), encoding="utf-8")
        homography = root / "homography.json"
        homography.write_text(json.dumps({
            "H": [[0.5, 0, 0], [0, 0.5, 0], [0, 0, 1]],
            "image_size": [160, 100], "coordinate_system": "undistorted_full_frame",
            "rms_reprojection_mm": 0.0,
        }), encoding="utf-8")
        return csv_path, video_path, intrinsics, homography

    def test_full_pipeline_writes_both_granularities(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path, video_path, intrinsics, homography = self._make_input(root)
            result = run_pipeline(RunConfig(
                emg_csv=csv_path, video_path=video_path, output_dir=root / "result",
                led_roi=Rect(0, 0, 15, 15), marker_roi=Rect(20, 20, 135, 75),
                marker_count=2, intrinsics_path=intrinsics, homography_path=homography,
                led_metric="mean", marker_threshold=180,
            ))
            self.assertEqual(result["status"], "full_fusion_generated")
            self.assertFalse(result["full_fusion_validated"])
            self.assertLess(result["alignment"]["rms_residual_s"], 1 / 60)
            frame_table = pd.read_csv(root / "result" / "aligned_by_frame.csv")
            sample_table = pd.read_csv(root / "result" / "aligned_by_emg_sample.csv")
            self.assertEqual(len(frame_table), 900 * 2)
            self.assertEqual(len(sample_table), 14990 * 2)
            self.assertTrue(frame_table["detected"].all())
            example = frame_table[(frame_table.frame_index == 300) & (frame_table.particle_id == 0)].iloc[0]
            self.assertAlmostEqual(example.X_mm, example.u_px / 2, delta=0.1)
            self.assertAlmostEqual(example.Y_mm, example.v_px / 2, delta=0.1)
            self.assertGreater((sample_table.motion_value_type == "interpolated").sum(), 100)
            self.assertTrue((root / "result" / "alignment_report.json").exists())

    def test_bad_calibration_fails_without_fusion_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path, video_path, intrinsics, homography = self._make_input(root)
            bad = json.loads(homography.read_text(encoding="utf-8"))
            bad["image_size"] = [320, 240]
            homography.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "image_size"):
                run_pipeline(RunConfig(
                    emg_csv=csv_path, video_path=video_path, output_dir=root / "bad_result",
                    led_roi=Rect(0, 0, 15, 15), marker_roi=Rect(20, 20, 135, 75),
                    marker_count=2, intrinsics_path=intrinsics, homography_path=homography,
                    led_metric="mean", marker_threshold=180,
                ))
            self.assertFalse((root / "bad_result" / "aligned_by_frame.csv").exists())
            report = json.loads((root / "bad_result" / "alignment_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")


class CalibrationTests(unittest.TestCase):
    def test_plane_calibration_records_quality_and_coordinate_system(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            width, height = 320, 260
            gray = np.full((height, width), 127, dtype=np.uint8)
            for y in range(9):
                for x in range(12):
                    gray[30 + y * 20 : 30 + (y + 1) * 20, 40 + x * 20 : 40 + (x + 1) * 20] = 255 if (x + y) % 2 else 0
            video_path = root / "board.avi"
            writer = cv2.VideoWriter(str(video_path), cv2.VideoWriter_fourcc(*"MJPG"), 30.0, (width, height))
            self.assertTrue(writer.isOpened())
            writer.write(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR))
            writer.release()
            intrinsics = root / "intrinsics.json"
            intrinsics.write_text(json.dumps({
                "K": [[300, 0, width / 2], [0, 300, height / 2], [0, 0, 1]],
                "dist": [[0, 0, 0, 0, 0]], "image_size": [width, height],
                "rms_reprojection_px": 0.0,
            }), encoding="utf-8")
            data = calibrate_plane(video_path, intrinsics, root / "plane.json", frame_index=0, square_mm=3.0)
            self.assertEqual(data["coordinate_system"], "undistorted_full_frame")
            self.assertEqual(data["image_size"], [width, height])
            self.assertLess(data["rms_reprojection_mm"], 0.1)


if __name__ == "__main__":
    unittest.main()
