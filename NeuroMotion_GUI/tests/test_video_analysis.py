from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np
import pandas as pd

from neuromotion.session import file_info
from neuromotion.video import Rect
from neuromotion.video_analysis import VideoAnalysisConfig, analyze_video, load_video_analysis


class IndependentVideoTests(unittest.TestCase):
    def make_video(self, root):
        path = root / "input.avi"
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 30, (200, 120))
        for index in range(60):
            frame = np.zeros((120, 200, 3), np.uint8)
            if (index // 15) % 2:
                frame[:12, :12] = 255
            for marker in range(5):
                cv2.circle(frame, (25 + 32 * marker, 40 + 20 * (marker % 3)), 4, (255, 255, 255), -1)
            writer.write(frame)
        writer.release()
        return path

    def test_led_only_has_no_emg_or_calibration_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = VideoAnalysisConfig(self.make_video(root), root / "led", led_roi=Rect(0, 0, 12, 12), led_metric="mean")
            report = analyze_video(config, progress=lambda _: None)
            self.assertTrue(report["led_extracted"])
            self.assertFalse(report["markers_extracted"])
            self.assertEqual(len(pd.read_csv(root / "led/video_ttl.csv")), 60)

    def test_marker_only_without_led_or_calibration_produces_pixel_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = analyze_video(VideoAnalysisConfig(self.make_video(root), root / "markers", marker_roi=Rect(15, 20, 180, 95), marker_threshold=180), progress=lambda _: None)
            rows = pd.read_csv(root / "markers/tracking.csv")
            self.assertEqual(len(rows), 300)
            self.assertTrue(rows.detected.all())
            self.assertTrue(rows.X_mm.isna().all())
            self.assertEqual(report["coordinate_system"], "raw_frame")
            self.assertFalse(report["millimeter_coordinates"])
            self.assertFalse((root / "markers/video_ttl.csv").exists())

    def test_combined_analysis_caches_same_frames_and_rejects_wrong_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = self.make_video(root)
            intrinsics, plane = root / "intrinsics.json", root / "plane.json"
            intrinsics.write_text(json.dumps({"K": [[150, 0, 100], [0, 150, 60], [0, 0, 1]], "dist": [[0, 0, 0, 0, 0]], "image_size": [200, 120], "rms_reprojection_px": 0}))
            plane.write_text(json.dumps({"H": [[0.5, 0, 0], [0, 0.5, 0], [0, 0, 1]], "image_size": [200, 120], "coordinate_system": "undistorted_full_frame", "rms_reprojection_mm": 0}))
            analyze_video(VideoAnalysisConfig(video, root / "both", Rect(0, 0, 12, 12), Rect(15, 20, 180, 95),
                                              intrinsics_path=intrinsics, homography_path=plane, led_metric="mean", marker_threshold=180), progress=lambda _: None)
            trace, tracking, report = load_video_analysis(root / "both", file_info(video), True)
            try:
                self.assertEqual(len(trace.frame_index), len(tracking.records) // 5)
                np.testing.assert_allclose(tracking.records["X_mm"], tracking.records["u_px"] / 2)
            finally:
                tracking.close()
                trace.close()
            with self.assertRaisesRegex(ValueError, "different source"):
                load_video_analysis(root / "both", {"sha256": "wrong"}, True)


if __name__ == "__main__":
    unittest.main()
