from __future__ import annotations

import csv
import json
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np

from neuromotion.acquisition import AcquisitionController, BinaryReplaySource, FrameDecoder, SimulatedSource, encode_frame
from neuromotion.filters import FilterSettings, RealtimeFilters


def frame(first=0):
    return encode_frame((first + i, i + 0.125, -i - 0.25, i % 2, 0) for i in range(12))


class ProtocolTests(unittest.TestCase):
    def test_every_frame_split_and_noise_resynchronization(self):
        data = frame()
        for split in range(1, len(data)):
            decoder = FrameDecoder()
            result = decoder.feed(b"noise" + data[:split]) + decoder.feed(data[split:] + frame(12))
            self.assertEqual([sample.sample_index for group in result for sample in group], list(range(24)))
            self.assertLess(len(decoder.buffer), 245)

    def test_bad_end_invalid_samples_and_header_inside_payload(self):
        decoder = FrameDecoder()
        data = bytearray(frame())
        data[-1] = 0
        good = bytearray(frame())
        good[16] = 2
        struct.pack_into("<f", good, 28, float("nan"))
        result = decoder.feed(bytes(data) + bytes(good) + frame(24))
        self.assertEqual(sum(len(rows) for rows in result), 22)
        self.assertEqual(decoder.invalid_records, 2)
        special = struct.unpack("<f", bytes([0xA5, 0xF2, 0x11, 0xE3]))[0]
        self.assertEqual(len(FrameDecoder().feed(encode_frame((i, special, 0, 1, 0) for i in range(12)))[0]), 12)

    def test_long_noise_does_not_accumulate(self):
        decoder = FrameDecoder()
        for _ in range(50):
            decoder.feed(b"x" * 4096 + b"\xa5")
            self.assertLessEqual(len(decoder.buffer), 3)


class FilterTests(unittest.TestCase):
    def test_chunk_boundaries_do_not_change_output_and_raw_is_unchanged(self):
        raw = np.random.default_rng(1).normal(size=(5000, 2))
        original = raw.copy()
        settings = FilterSettings(high_pass_hz=10, low_pass_hz=200, notch_50hz=True)
        complete, _ = RealtimeFilters(settings).process(raw)
        filters = RealtimeFilters(settings)
        chunks = np.concatenate([filters.process(raw[start:start + 37])[0] for start in range(0, len(raw), 37)])
        np.testing.assert_allclose(complete, chunks, atol=1e-12)
        np.testing.assert_array_equal(raw, original)

    def test_default_despike_off_and_optional_spike_is_flagged(self):
        raw = np.random.default_rng(10).normal(loc=[1.0, -1.0], scale=0.01, size=(200, 2))
        raw[-1] = 1000
        untouched, flags = RealtimeFilters(FilterSettings(high_pass_hz=0)).process(raw)
        self.assertEqual(untouched[-1, 0], 1000)
        self.assertFalse(flags.any())
        clean, flags = RealtimeFilters(FilterSettings(high_pass_hz=0, despike=True)).process(raw)
        self.assertTrue(flags[-1])
        self.assertLess(clean[-1, 0], 2)


class AcquisitionTests(unittest.TestCase):
    def test_snapshot_reports_stalled_input_and_sample_ages(self):
        controller = AcquisitionController()
        controller.connected_at = 100
        self.assertIsNone(controller.snapshot()["last_sample_age_s"])
        controller.last_byte_at, controller.last_sample_at = 105, 104
        with patch("neuromotion.acquisition.time.monotonic", return_value=109):
            state = controller.snapshot(include_plot=False)
        self.assertEqual(state["connection_elapsed_s"], 9)
        self.assertEqual(state["last_byte_age_s"], 4)
        self.assertEqual(state["last_sample_age_s"], 5)

    def test_binary_replay_writes_exact_raw_csv_without_ui(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.bin"
            binary = b"noise" + b"".join(frame(index) for index in range(0, 120, 12)) + b"\xa5\xf2"
            source.write_bytes(binary)
            controller = AcquisitionController(display_samples=24)
            controller.connect(source=BinaryReplaySource(source, speed=0))
            controller.start_recording(root / "experiment")
            controller.wait()
            acquisition = root / "experiment/acquisition"
            with (acquisition / "emg.csv").open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 120)
            self.assertEqual((acquisition / "serial.bin").read_bytes(), binary)
            replayed = [sample for group in FrameDecoder().feed(binary) for sample in group]
            for row, sample in zip(rows, replayed):
                self.assertEqual(struct.pack("<f", float(row["ch1_raw_uv"])), struct.pack("<f", sample.ch1))
                self.assertEqual(int(row["sync"]), sample.sync)
            report = json.loads((acquisition / "acquisition_report.json").read_text())
            self.assertEqual(report["recorded_samples"], len(rows))
            self.assertEqual(report["trailing_fragment_bytes"], 2)
            self.assertLessEqual(len(controller.history), 24)
            self.assertFalse(report["hardware_validated"])

    def test_reset_and_gap_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.bin"
            source.write_bytes(frame(10) + frame(30) + frame(0))
            controller = AcquisitionController()
            controller.connect(source=BinaryReplaySource(source, speed=0))
            controller.start_recording(root / "experiment")
            controller.wait()
            with (root / "experiment/acquisition/emg.csv").open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([int(rows[i]["segment_id"]) for i in (0, 12, 24)], [0, 0, 1])
            self.assertEqual(controller.index_gaps, 8)
            events = [json.loads(line) for line in (root / "experiment/acquisition/filter_events.jsonl").read_text().splitlines()]
            self.assertEqual([event["reason"] for event in events], ["recording_start", "sample_gap", "mcu_reset"])

    def test_online_filter_change_is_acknowledged_and_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = AcquisitionController()
            controller.connect(source=SimulatedSource(2, speed=1))
            controller.start_recording(directory + "/experiment")
            time.sleep(0.1)
            revision = controller.update_filters(FilterSettings(high_pass_hz=0, low_pass_hz=150, notch_50hz=True))
            time.sleep(0.1)
            controller.stop_recording()
            controller.disconnect()
            path = Path(directory) / "experiment/acquisition"
            events = [json.loads(line) for line in (path / "filter_events.jsonl").read_text().splitlines()]
            changed = next(event for event in events if event["reason"] == "settings_changed")
            self.assertEqual(changed["filter_revision"], revision)
            with (path / "emg.csv").open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertTrue(any(int(row["sample_index"]) == changed["sample_index"] and int(row["filter_revision"]) == revision for row in rows))

    def test_write_fault_preserves_partial_file_and_incomplete_state(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = AcquisitionController()
            controller.connect(source=SimulatedSource(0.1, speed=0))
            with patch.object(controller, "_process_frame", side_effect=OSError("disk full")):
                controller.start_recording(directory + "/experiment")
                with self.assertRaisesRegex(RuntimeError, "disk full"):
                    controller.wait()
            root = Path(directory) / "experiment"
            self.assertTrue((root / "acquisition/emg.partial.csv").exists())
            self.assertFalse((root / "acquisition/emg.csv").exists())
            self.assertEqual(json.loads((root / "session.json").read_text())["state"], "incomplete")

    def test_new_recording_does_not_inherit_preview_start_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = AcquisitionController()
            controller.connect(source=SimulatedSource(20, speed=1))
            controller.start_marker = "verified"
            controller.start_cache = [(0, 1)] * 4500
            controller.start_recording(Path(directory) / "experiment")
            self.assertNotEqual(controller.start_marker, "verified")
            controller.stop_recording()
            controller.disconnect()


if __name__ == "__main__":
    unittest.main()
