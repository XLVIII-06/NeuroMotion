from dataclasses import replace
import csv
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from neuromotion.acquisition import AcquisitionController, BinaryReplaySource, encode_frame
from neuromotion.pipeline import RunConfig, run_pipeline
from neuromotion.replay import ReplayEngine
from neuromotion.signals import load_signal_sessions
from neuromotion.timeline import AlignmentError, fit_session_alignment
from neuromotion.validation import make_synthetic_inputs, ttl_state
from neuromotion.video import Rect
from neuromotion.video_analysis import VideoAnalysisConfig, analyze_video


class FullIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        fixture = make_synthetic_inputs(cls.root / "input", 30, 1000)
        binary = cls.root / "synthetic_wire.bin"
        with binary.open("wb") as stream:
            for first in range(0, 14496, 12):
                rows = []
                for index in range(first, first + 12):
                    t = 0.51 + index / 1000
                    rows.append((index, 10 * np.sin(t), 5 * np.cos(t), ttl_state(t), 0))
                stream.write(encode_frame(rows))
        controller = AcquisitionController()
        controller.connect(source=BinaryReplaySource(binary, speed=0))
        controller.start_recording(cls.root / "session")
        controller.wait()
        cls.emg = cls.root / "session/acquisition/emg.csv"
        cls.video_analysis = cls.root / "video_analysis"
        analyze_video(VideoAnalysisConfig(fixture.video_path, cls.video_analysis, fixture.led_roi, fixture.marker_roi,
                                          5, fixture.intrinsics_path, fixture.homography_path, led_metric="mean", marker_threshold=180), progress=lambda _: None)
        cls.config = RunConfig(cls.emg, fixture.video_path, cls.root / "result", fixture.led_roi, video_analysis_dir=cls.video_analysis)
        # Reuse must not decode the source video again in the alignment module.
        with patch("neuromotion.pipeline.scan_video_led", side_effect=AssertionError("unnecessary decode")), patch("neuromotion.pipeline.track_video_markers", side_effect=AssertionError("unnecessary tracking")):
            cls.report = run_pipeline(cls.config)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_real_parser_csv_video_alignment_exports_both_branches(self):
        sample = pd.read_csv(self.config.output_dir / "aligned_by_emg_sample.csv")
        self.assertEqual(len(sample), 14496 * 5)
        self.assertTrue(sample.ch1_raw_uv.notna().all())
        self.assertTrue(sample.ch1_filtered_uv.notna().all())
        self.assertGreater(float(np.max(np.abs(sample.ch1_raw_uv - sample.ch1_filtered_uv))), 1)
        self.assertEqual(self.report["status"], "full_fusion_generated")
        self.assertLessEqual(self.report["alignment"]["max_residual_s"], self.report["alignment"]["frame_interval_s"])

    def test_replay_frame_time_sample_and_positions_share_mapping(self):
        engine = ReplayEngine(self.config.output_dir)
        try:
            self.assertTrue(engine.emg_times.flags.c_contiguous)
            self.assertTrue(engine.frame_times.flags.c_contiguous)
            for time_s in (0.2, 2.5, 7.0, 12.0):
                snapshot = engine.seek(time_s)
                self.assertLess(abs(snapshot["common_time_s"] - time_s), 1 / 30)
                self.assertEqual(len(snapshot["positions"]), 5)
                for point in snapshot["positions"]:
                    self.assertAlmostEqual(point["X_mm"], point["u_px"] / 2)
                t = snapshot["video_time_s"]
                self.assertAlmostEqual(snapshot["emg_sample_float"], self.report["alignment"]["slope_samples_per_second"] * t + self.report["alignment"]["intercept_samples"])
                self.assertLessEqual(len(snapshot["emg_window"]), 5001)
        finally:
            engine.close()

    def test_replay_reuses_index_and_seeks_without_reading_csv(self):
        first = ReplayEngine(self.config.output_dir)
        first.close()
        with patch("pandas.read_csv", side_effect=AssertionError("CSV scanned while seeking")):
            engine = ReplayEngine(self.config.output_dir)
            try:
                self.assertGreater(len(engine.seek(5)["emg_window"]), 100)
            finally:
                engine.close()

    def test_cancelled_export_does_not_leave_claimed_fusion_tables(self):
        event = threading.Event()
        config = replace(self.config, output_dir=self.root / "cancelled")
        before = self.emg.read_bytes()
        def progress(message):
            if "导出样本表" in message:
                event.set()
        with self.assertRaises(InterruptedError):
            run_pipeline(config, progress=progress, cancel_event=event)
        report = json.loads((config.output_dir / "alignment_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "cancelled")
        self.assertFalse((config.output_dir / "aligned_by_frame.csv").exists())
        self.assertFalse((config.output_dir / "aligned_by_emg_sample.csv").exists())
        self.assertEqual(before, self.emg.read_bytes())


class TimelineAndMetadataTests(unittest.TestCase):
    def test_normal_unmatched_tail_is_allowed_but_internal_missing_edges_fail(self):
        for video_stop, emg_stop in ((10.5, 9.1), (9.1, 10.5)):
            video_t = np.arange(0, video_stop, 1 / 60)
            emg_t = np.arange(0.51, emg_stop, 0.001)
            model = fit_session_alignment(np.array([ttl_state(t) for t in video_t]), video_t,
                                          np.array([ttl_state(t) for t in emg_t]), np.arange(len(emg_t)), session_number=0)
            self.assertGreater(model.unmatched_tail_video_edges + model.unmatched_tail_emg_edges, 0)
            self.assertLess(model.max_residual_s, 1 / 60)
        video_t = np.arange(0, 10.5, 1 / 60)
        emg_t = np.arange(0.51, 10.5, 0.001)
        corrupted = np.array([ttl_state(t) for t in video_t])
        corrupted[(video_t >= 6) & (video_t < 7)] = 0
        with self.assertRaises(AlignmentError):
            fit_session_alignment(corrupted, video_t, np.array([ttl_state(t) for t in emg_t]), np.arange(len(emg_t)), session_number=0)

    def test_many_filter_revisions_remain_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "filters.csv"
            with path.open("w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["sample_index", "ch1_raw_uv", "ch2_raw_uv", "ch1_uv", "ch2_uv", "sync", "high_pass_hz", "filter_revision"])
                for index in range(60):
                    writer.writerow([index, 1, 2, 3, 4, 1, index, index])
            session = load_signal_sessions(path)[0]
            self.assertEqual(session.metadata["high_pass_hz"]["unique_count"], 60)
            self.assertEqual(session.ch1_filtered_uv[0], 3)


if __name__ == "__main__":
    unittest.main()
