"""Synthetic three-source truth, portable derived replay and source independence."""
import csv
from dataclasses import replace
import gc
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from neuromotion.acquisition import AcquisitionController, BinaryReplaySource
from neuromotion.bl422 import export_parameter_template, load_bl422
from neuromotion.filters import FilterSettings
from neuromotion.pipeline import run_pipeline
from neuromotion.replay import ReplayEngine
from neuromotion.result_store import export_replay_package, load_result_manifest, relocate_video
from neuromotion.session import file_info
from neuromotion.validation import make_synthetic_inputs, ttl_state
from neuromotion.video_analysis import VideoAnalysisConfig, analyze_video
from test_four_channel_integration import make_wire


def make_bl(path):
    # Physical time = BL time * 1.006 - 2.5. The sampled step crosses halfway
    # between observations, so clock truth has at most half one BL sample error.
    events = {100: 'Stimulate: 8mA', 7500: 'Stimulate: 0.00mA',
              7501: 'Stimulate: -0.50mA', 8500: 'Stimulate: -2.0mA',
              17000: 'unrecognized note'}
    with path.open('w', encoding='gb18030', newline='') as stream:
        stream.write('导出数据的原始文件:synthetic.tmen\nTTL，总共18000个数据点，采样率：1000Hz\n')
        writer = csv.writer(stream)
        writer.writerow(['序号', '时间', '2通道数据(mV)', '事件'])
        for n in range(18000):
            writer.writerow([n+1, f'00:{n/1000:06.3f}', 17+721*ttl_state(n/1000*1.006-2.5), events.get(n, '')])


class StimulusIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        persistent = os.environ.get('NEUROMOTION_BL_FIXTURE')
        cls.temp = None if persistent else tempfile.TemporaryDirectory()
        cls.root = Path(persistent) if persistent else Path(cls.temp.name)
        cls.root.mkdir(parents=True, exist_ok=True)
        config = make_synthetic_inputs(cls.root/'fixture', 30, 1000)
        cls.bl = cls.root/'BL TTL.csv'; make_bl(cls.bl)
        cls.params = cls.root/'纸面参数.csv'
        with load_bl422(cls.bl) as record:
            export_parameter_template(record, cls.params)
        with cls.params.open(encoding='utf-8-sig') as stream:
            rows = list(csv.DictReader(stream))
        rows[1].update(pulse_width_ms='.25', notes='handwritten test parameter')
        with cls.params.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=rows[0].keys()); writer.writeheader(); writer.writerows(rows)
        binary = cls.root/'native.bin'; make_wire(binary)
        c = AcquisitionController(FilterSettings(high_pass_hz=20, low_pass_hz=200))
        c.connect(source=BinaryReplaySource(binary, speed=0)); c.start_recording(cls.root/'session'); c.wait()
        cls.emg = cls.root/'session/acquisition/emg.csv'
        cls.config = replace(config, emg_csv=cls.emg, bl_csv=cls.bl,
                             stimulus_parameters_path=cls.params, output_dir=cls.root/'result')
        cls.report = run_pipeline(cls.config)

    @classmethod
    def tearDownClass(cls):
        gc.collect()
        if cls.temp:
            cls.temp.cleanup()

    def test_events_fractional_truth_signed_values_unknown_and_all_retained(self):
        table = pd.read_csv(self.config.output_dir/'stimulus_events.csv')
        self.assertEqual(len(table), 5)
        np.testing.assert_array_equal(table.in_common_coverage, [False, True, True, True, False])
        self.assertEqual(table.commanded_current_ma.tolist()[:4], [8, 0, -.5, -2])
        self.assertTrue(np.isnan(table.commanded_current_ma.iloc[4]))
        self.assertTrue(table.common_time_s.iloc[[0, 4]].isna().all())
        # Four-channel master zero is the measured PA8 start at physical .5s.
        truth = table.bl_time_s.iloc[1:4]*1.006-3
        np.testing.assert_allclose(table.common_time_s.iloc[1:4], truth, atol=.0006)
        self.assertEqual(table.pulse_width_ms.iloc[1], .25)
        self.assertTrue(np.isnan(table.pulse_width_ms.iloc[2]))
        self.assertEqual(table.raw_event_text.iloc[1], 'Stimulate: 0.00mA')
        self.assertFalse(table.absolute_onset_validated.any())
        self.assertLess(self.report['stimulus']['clock']['validation']['max_s'], .002)

    def test_fusion_bins_multiple_events_preserve_gaps_and_trim_coverage(self):
        table = pd.read_csv(self.config.output_dir/'aligned_by_frame.csv')
        two = table[table.stim_event_count == 2]
        self.assertGreater(len(two), 0)
        self.assertEqual(len(json.loads(two.stim_event_ids.iloc[0])), 2)
        coverage = self.report['stimulus']['common_coverage_s']
        self.assertGreaterEqual(table.common_time_s.min(), coverage[0])
        self.assertLessEqual(table.common_time_s.max(), coverage[1])
        samples = pd.read_csv(self.config.output_dir/'aligned_by_emg_sample.csv')
        self.assertTrue(samples.ch3_aligned_uv.isna().any())
        self.assertTrue((samples.quality_ch3 == 'native_gap').any())
        events = pd.read_csv(self.config.output_dir/'stimulus_events.csv')
        for _, e in events[events.in_common_coverage].iterrows():
            rows = samples[samples.stim_event_ids.str.contains(e.event_id, regex=False)]
            self.assertEqual(rows.sample_index.nunique(), 1)
            self.assertLessEqual(rows.common_time_s.iloc[0], e.common_time_s+1e-8)
            self.assertLess(e.common_time_s-rows.common_time_s.iloc[0], .00101)

    def test_derived_replay_never_reads_original_csv_and_rebuilds_missing_cache(self):
        with patch('neuromotion.signals.load_signal_sessions', side_effect=AssertionError('Original EMG was read')):
            engine = ReplayEngine(self.config.output_dir)
        try:
            self.assertEqual(engine.manifest['schema_version'], 5)
            self.assertEqual(len(engine.events), 5)
            self.assertEqual(len(engine.seek(5)['positions']), 5)
            data = engine.root/'replay_index'/engine.manifest['data_dir']
        finally:
            engine.close()
        (data/'emg_time.npy').unlink()
        engine = ReplayEngine(self.config.output_dir)
        try:
            self.assertNotEqual(engine.manifest['data_dir'], data.name)
        finally:
            engine.close()

    def test_portable_moved_chinese_path_without_original_sources(self):
        destination = self.root/'便携包 含空格'
        export_replay_package(self.config.output_dir, destination)
        moved = self.root/'移动后的目录'
        destination.rename(moved)
        unavailable = [(p, p.with_suffix(p.suffix+'.hidden')) for p in (self.emg, self.bl, self.config.video_path, self.config.intrinsics_path, self.config.homography_path)]
        try:
            for p, hidden in unavailable:
                p.rename(hidden)
            engine = ReplayEngine(moved)
            try:
                snap = engine.seek(5)
                self.assertEqual(snap['channel_count'], 4)
                self.assertEqual(len(snap['positions']), 5)
                self.assertEqual(len(engine.events), 5)
                self.assertTrue((moved/engine.report['input_files']['video']['path']).is_file())
            finally:
                engine.close()
        finally:
            for p, hidden in unavailable:
                hidden.rename(p)

    def test_relocation_hash_cancellation_and_original_archive_integrity(self):
        destination = self.root/'relocation'; export_replay_package(self.config.output_dir, destination)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            relocate_video(destination, self.bl)
        relocate_video(destination, self.config.video_path)
        load_result_manifest(destination)
        cancel = threading.Event(); cancel.set()
        partial = self.root/'cancelled'
        with self.assertRaises(InterruptedError):
            export_replay_package(self.config.output_dir, partial, cancel_event=cancel)
        self.assertFalse((partial/'result_manifest.json').exists())
        archive = self.root/'with_raw'; export_replay_package(self.config.output_dir, archive, include_raw=True)
        self.assertTrue((archive/'originals/acquisition/native_samples.csv').is_file())
        self.assertEqual(file_info(archive/'originals/bl_csv'/self.bl.name)['sha256'], file_info(self.bl)['sha256'])

    def test_changed_derived_event_data_rejected_instead_of_stale_cache(self):
        destination = self.root/'tampered'; export_replay_package(self.config.output_dir, destination)
        with (destination/'stimulus_events.csv').open('a', encoding='utf-8') as stream:
            stream.write('\n')
        with self.assertRaisesRegex(ValueError, 'changed or is missing'):
            ReplayEngine(destination)

    def test_old_result_without_new_manifest_uses_v4_loader(self):
        destination = self.root/'legacy_result'
        shutil.copytree(self.config.output_dir, destination, ignore=shutil.ignore_patterns('replay_index', 'result_manifest.json'))
        engine = ReplayEngine(destination)
        try:
            self.assertEqual(engine.manifest['schema_version'], 4)
            self.assertEqual(engine.seek(5)['channel_count'], 4)
            self.assertEqual(len(engine.seek(5)['positions']), 5)
        finally:
            engine.close()

    def test_pixel_only_and_legacy_two_channel_path(self):
        config = replace(self.config, emg_csv=self.root/'fixture/emg.csv',
                         output_dir=self.root/'pixel', coordinate_mode='pixel', intrinsics_path=None, homography_path=None)
        run_pipeline(config)
        tracking = pd.read_csv(config.output_dir/'tracking.csv')
        self.assertTrue(tracking.X_mm.isna().all())
        self.assertTrue(tracking.u_px.notna().all())
        engine = ReplayEngine(config.output_dir)
        try:
            self.assertEqual(engine.manifest['channel_count'], 2)
            self.assertIsNone(engine.undistorter)
            self.assertTrue(np.isnan(engine.seek(5)['positions'][0]['X_mm']))
            self.assertEqual(len(engine.events), 5)
        finally:
            engine.close()

    def test_pixel_mode_clears_cached_metric_columns_without_changing_cache(self):
        directory = self.root/'cached_metric'
        c = self.config
        analyze_video(VideoAnalysisConfig(c.video_path, directory, c.led_roi, c.marker_roi, 5,
            c.intrinsics_path, c.homography_path, led_metric='mean', marker_threshold=180), progress=lambda _: None)
        cache_hash = file_info(directory/'tracking.csv')['sha256']
        report = run_pipeline(replace(c, output_dir=self.root/'cached_pixel', video_analysis_dir=directory, coordinate_mode='pixel'))
        self.assertFalse(report['millimeter_coordinates'])
        self.assertTrue(pd.read_csv(self.root/'cached_pixel/tracking.csv').X_mm.isna().all())
        self.assertEqual(file_info(directory/'tracking.csv')['sha256'], cache_hash)


if __name__ == '__main__':
    unittest.main()
