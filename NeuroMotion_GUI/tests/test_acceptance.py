from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from neuromotion.signals import SignalInputError, load_signal_sessions
from neuromotion.timeline import AlignmentError, fit_session_alignment
from neuromotion.video import Rect, VideoInputError, scan_video_led
from neuromotion.pipeline import RunConfig, _select_session, _interpolate_emg
from neuromotion.video import VideoTrace
from test_pipeline import state_at


class AcceptanceRegressionTests(unittest.TestCase):
    def test_missing_single_emg_sample_is_not_silently_interpolated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gaps.csv"
            path.write_text("sample_index,ch1_uv,ch2_uv,sync\n0,0,0,1\n1,1,1,1\n3,3,3,1\n")
            session = load_signal_sessions(path)[0]
            config = RunConfig(path, path, Path(directory), Rect(0, 0, 1, 1))
            before, after, ch1, ch2, kind = _interpolate_emg(session, 2, config.max_emg_gap_samples)
            self.assertEqual((before, after, ch1, ch2, kind), (1, 3, None, None, "emg_gap"))

    def test_raw_format_preserves_channels_and_filter_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.csv"
            path.write_text("sample_index,ch1_raw_uv,ch2_raw_uv,ch1_uv,ch2_uv,sync,sample_rate_hz,high_pass_hz\n0,20,30,1,2,1,1000,10\n1,21,31,1,2,1,1000,10\n")
            session = load_signal_sessions(path)[0]
            self.assertEqual(session.ch1_uv.tolist(), [20, 21])
            self.assertEqual(session.signal_kind, "raw")
            self.assertEqual(session.metadata["high_pass_hz"], [10])

    def test_duplicates_and_invalid_optional_counters_are_rejected(self):
        for content in ("0,1,2,0,0\n0,1,2,1,0", "0,1,2,1,invalid", "0,1,2,1,-1"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bad.csv"
                path.write_text("sample_index,ch1_uv,ch2_uv,sync,dropped_samples\n" + content + "\n")
                with self.assertRaises(SignalInputError):
                    load_signal_sessions(path)

    def test_video_gap_during_sync_is_rejected(self):
        video_t = np.arange(0, 15, 1 / 60)
        emg_t = np.arange(0.51, 15, 0.001)
        keep = ~((video_t > 6.05) & (video_t < 6.4))
        with self.assertRaisesRegex(AlignmentError, "timestamp gap"):
            fit_session_alignment(np.array([state_at(t) for t in video_t[keep]]), video_t[keep],
                                  np.array([state_at(t) for t in emg_t]), np.arange(len(emg_t)), session_number=0)

    def test_end_in_only_one_recording_is_rejected(self):
        video_t = np.arange(0, 15, 1 / 60)
        emg_t = np.arange(0.51, 10.5, 0.001)
        with self.assertRaisesRegex(AlignmentError, "only one"):
            fit_session_alignment(np.array([state_at(t) for t in video_t]), video_t,
                                  np.array([state_at(t) for t in emg_t]), np.arange(len(emg_t)), session_number=0)

    def test_two_matching_sessions_are_rejected_as_ambiguous(self):
        video_t = np.arange(0, 15, 1 / 60)
        video_states = np.array([state_at(t) for t in video_t], dtype=np.int8)
        trace = VideoTrace(np.arange(len(video_t)), video_t, video_t, video_t, video_t, video_states, 100, "mean", "pts", 10, 10, 60)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.csv"
            with path.open("w") as stream:
                stream.write("sample_index,ch1_uv,ch2_uv,sync\n")
                for _ in range(2):
                    for index, t in enumerate(np.arange(0.51, 15, 0.001)):
                        stream.write(f"{index},1,2,{state_at(t)}\n")
            sessions = load_signal_sessions(path)
            with self.assertRaisesRegex(AlignmentError, "found 2"):
                _select_session(trace, sessions, 1000)

    def test_end_code_at_wrong_time_is_rejected(self):
        video_t = np.arange(0, 15, 1 / 60)
        emg_t = np.arange(0.51, 15, 0.001)
        video = np.array([state_at(t) for t in video_t])
        emg = np.array([state_at(t) if t < 11 else (0 if t < 11.2 else state_at(t - 0.2)) for t in emg_t])
        with self.assertRaisesRegex(AlignmentError, "End marker.*disagree"):
            fit_session_alignment(video, video_t, emg, np.arange(len(emg)), session_number=0)

    def test_fractional_required_integer_is_rejected(self):
        for row in ("0.5,1,2,1", "0,1,2,1.5", "0,1,2,256"):
            with self.subTest(row=row), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bad.csv"
                path.write_text("sample_index,ch1_uv,ch2_uv,sync\n" + row + "\n")
                with self.assertRaises(SignalInputError):
                    load_signal_sessions(path)

    def test_invalid_pts_needs_explicit_cfr_opt_in(self):
        class Capture:
            def get(self, prop):
                return {cv2.CAP_PROP_FRAME_WIDTH: 10, cv2.CAP_PROP_FRAME_HEIGHT: 10,
                        cv2.CAP_PROP_FPS: 30, cv2.CAP_PROP_FRAME_COUNT: 6}.get(prop, 0)
            def read(self):
                self.index = getattr(self, "index", 0) + 1
                return (True, np.full((10, 10, 3), self.index * 30, np.uint8)) if self.index <= 6 else (False, None)
            def release(self):
                pass
        with patch("neuromotion.video._open_video", return_value=Capture()):
            with self.assertRaisesRegex(VideoInputError, "timestamps"):
                scan_video_led("unused", Rect(0, 0, 10, 10), threshold=100)
        with patch("neuromotion.video._open_video", return_value=Capture()):
            trace = scan_video_led("unused", Rect(0, 0, 10, 10), threshold=100, allow_fps_fallback=True)
            self.assertEqual(trace.timestamp_source, "assumed_constant_fps")

    def test_nonstartup_high_with_large_index_is_not_assumed_start(self):
        video_t = np.arange(0, 15, 1 / 60)
        emg_t = np.arange(0.51, 15, 0.001)
        with self.assertRaisesRegex(AlignmentError, "start Barker"):
            fit_session_alignment(np.array([state_at(t) for t in video_t]), video_t,
                                  np.array([state_at(t) for t in emg_t]), np.arange(len(emg_t)) + 8000,
                                  session_number=0)


if __name__ == "__main__":
    unittest.main()
