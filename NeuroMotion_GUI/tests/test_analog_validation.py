"""Independent edge-estimator truth, negative controls and streamed windows."""
from pathlib import Path
import csv
import tempfile
import unittest

import numpy as np

from scripts.validate_four_channel_analog import (compare_crossings, measure_step, native_observations,
                                                  plot_result, require_clean_capture)


def sinc3_step(relative_us):
    x = np.asarray(relative_us)/1000+1.5
    value = sum(coefficient*np.maximum(x-offset, 0)**3
                for offset, coefficient in enumerate([1, -3, 3, -1]))/6
    return np.clip(value, 0, 1)


def trace(delay_us=0., phase_us=0., falling=False):
    t = np.arange(-180000, 180001, 1000, dtype=float)+phase_us
    v = -6000+12000*sinc3_step(t-delay_us)
    if falling:
        v = -v
    return t, v


class AnalogValidationTests(unittest.TestCase):
    def test_capture_errors_cannot_be_hidden_by_good_edge_windows(self):
        capture = dict(state='complete', error='', frame_errors=0, invalid_records=0,
                       observed_index_gaps=0, clock=dict(hardware=dict(capture_overrun_a=0, max_read_delay_us=500)))
        require_clean_capture(capture)
        for field in ('frame_errors', 'invalid_records', 'observed_index_gaps', 'error', 'clock'):
            bad = {**capture, field: {} if field == 'clock' else 1}
            with self.subTest(field=field), self.assertRaises(ValueError):
                require_clean_capture(bad)
        with self.assertRaises(ValueError):
            require_clean_capture({**capture, 'clock': dict(hardware=dict(capture_overrun_a=1))})

    def test_known_sinc3_phase_sweep_and_real_delays(self):
        errors = []
        for phase in np.arange(0, 1000, 62.5):
            for delay in [125., 400., 800.]:
                for falling in [False, True]:
                    t, a = trace(1500, phase, falling)
                    _, b = trace(1500+delay, phase, falling)
                    ma = measure_step(t, a, 0, not falling)
                    mb = measure_step(t, b, 0, not falling)
                    self.assertTrue(ma['valid'] and mb['valid'])
                    errors.append(abs(mb['latency_us']-ma['latency_us']-delay))
        # A tenfold margin to the requested 1ms bench criterion. This only
        # bounds the estimator on this known smooth truth, not all hardware.
        self.assertLess(max(errors), 100.)

    def test_over_one_ms_delay_is_not_hidden_by_estimator(self):
        t, a = trace(1500)
        _, b = trace(3000)
        ma, mb = measure_step(t, a, 0, 1), measure_step(t, b, 0, 1)
        pairs = compare_crossings([ma, ma, mb, mb])
        self.assertTrue(all(p['valid'] for p in pairs))
        self.assertTrue(all(not p['observed_within_limit'] for p in pairs))

    def test_good_delay_and_noise_interval_are_separate(self):
        rng = np.random.default_rng(20261118)
        t, a = trace(1500, 240)
        _, b = trace(1800, 240)
        ma = measure_step(t, a+rng.normal(0, 80, len(t)), 0, 1)
        mb = measure_step(t, b+rng.normal(0, 80, len(t)), 0, 1)
        pair = compare_crossings([ma, ma, mb, mb])[0]
        self.assertTrue(pair['observed_within_limit'])
        self.assertTrue(pair['noise_margin_within_limit'])
        self.assertGreater(pair['noise_only_95_halfwidth_us'], 0)

    def test_flat_wrong_polarity_and_saturation_rejected(self):
        t, values = trace()
        for bad, reason in [(np.zeros(len(t)), 'unexpected_step_amplitude'),
                            (-values, 'wrong_polarity'),
                            (values+400000, 'saturated')]:
            with self.subTest(reason=reason):
                result = measure_step(t, bad, 0, 1)
                self.assertFalse(result['valid'])
                self.assertEqual(result['reason'], reason)

    def test_gap_and_ambiguous_crossings_rejected(self):
        t, values = trace()
        result = measure_step(np.delete(t, 180), np.delete(values, 180), 0, 1)
        self.assertEqual(result['reason'], 'sample_gap')
        bad = values.copy()
        bad[(t >= -3000) & (t < -1000)] = 1500
        bad[(t >= -1000) & (t <= 0)] = -1500
        self.assertEqual(measure_step(t, bad, 0, 1)['reason'], 'ambiguous_or_missing_crossing')
        bad[10] = np.nan
        self.assertEqual(measure_step(t, bad, 0, 1)['reason'], 'missing_or_nonfinite')

    def test_native_stream_handles_csv_chunk_boundaries(self):
        events = []
        for device in (0, 1):
            for index in range(4200):
                time = 200*device+index*1000/(1+device*50e-6)
                delay = 1500+device*300
                value = -6000+12000*sinc3_step(time-1000000-delay)-12000*sinc3_step(time-3000000-delay)
                events.append((0, device, index, time, value, value, 1))
        events.sort(key=lambda x:x[3])
        edges = [dict(index=0, time_us=1000000, level=1, segment_id=0),
                 dict(index=1, time_us=3000000, level=0, segment_id=0)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'native.csv'
            with path.open('w', newline='', encoding='utf-8') as stream:
                writer=csv.writer(stream)
                writer.writerow(['segment_id','device','native_index','drdy_time_us',
                                 'ch1_native_uv','ch2_native_uv','valid'])
                writer.writerows(events)
            groups = list(native_observations(path, edges, [403333]*4))
            self.assertEqual(len(groups), 2)
            for group in groups:
                self.assertTrue(all(m['valid'] for m in group))
                self.assertLess(abs(group[2]['latency_us']-group[0]['latency_us']-300), 100)
                self.assertIn('uncorrected_nominal_index_ms', group[2])

    def test_plot_keeps_diagnostic_output_when_all_edges_rejected(self):
        class FlatSession:
            mcu_time_us = np.arange(-180000, 180001, 1000)

            def channel(self, channel):
                return np.zeros(len(self.mcu_time_us))

        pairs = [dict(pair=f'CH{b}-CH{a}', valid=False, common_time_s=0)
                 for a, b in [(1,3), (1,4), (2,3), (2,4)]]
        observations = [dict(edge=dict(time_us=0))]
        with tempfile.TemporaryDirectory() as directory:
            plot_result(Path(directory), pairs, observations, FlatSession(), 0)
            self.assertGreater((Path(directory)/'analog_alignment.png').stat().st_size, 1000)


if __name__ == '__main__':
    unittest.main()
