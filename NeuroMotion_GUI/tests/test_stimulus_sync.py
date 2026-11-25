from pathlib import Path
import csv
import tempfile
import unittest

import numpy as np

from neuromotion.bl422 import load_bl422
from neuromotion.stimulus_sync import TTLTrace, StimulusSyncError, analog_ttl, protocol, fit_bl_clock


def edge_trace(end=24):
    stable = np.arange(4., end, .5)
    return TTLTrace(np.r_[.5, 2., 3., 3.5, stable], np.r_[1, 0, 1, 0, (np.arange(len(stable)) % 2 == 0).astype(int)])


class StimulusClockTests(unittest.TestCase):
    def test_measured_anchor_does_not_use_nominal_symbol_subtraction(self):
        trace = edge_trace()
        trace.times = (trace.times-.5)*.994+88.494
        result = protocol(trace)
        self.assertAlmostEqual(result['start_s'], 88.494)
        self.assertAlmostEqual(result['symbol_s'], .497)

    def test_affine_and_inverse_polarity_no_extrapolation(self):
        source = edge_trace()
        master = TTLTrace(source.times*1.006+12.3, source.levels.copy())
        source.levels = 1-source.levels
        result = fit_bl_clock(source, master)
        self.assertEqual(result.mode, 'affine')
        self.assertTrue(result.report['source_inverted'])
        np.testing.assert_allclose(result.map([5, 10]), np.array([5, 10])*1.006+12.3, atol=1e-10)
        self.assertTrue(np.isnan(result.map([0, 100])).all())

    def test_piecewise_uses_held_out_edges_and_recovers_smooth_drift(self):
        source = edge_trace(120)
        transform = lambda x: x*1.006+3+.004*np.sin(x/7)
        master = TTLTrace(transform(source.times), source.levels.copy())
        result = fit_bl_clock(source, master)
        self.assertEqual(result.mode, 'piecewise')
        self.assertGreater(result.report['affine_leave_one_out']['max_s'], .002)
        self.assertLess(result.report['validation']['max_s'], .0001)
        points = np.arange(5.25, 118, .5)
        self.assertLess(np.max(abs(result.map(points)-transform(points))), .00002)

    def test_missing_start_missing_edges_and_clock_step_rejected(self):
        for kind in ('no_start', 'gap', 'step'):
            source, master = edge_trace(), edge_trace()
            if kind == 'no_start':
                source.times = source.times[1:]; source.levels = source.levels[1:]
            elif kind == 'gap':
                source.times = np.delete(source.times, 15); source.levels = np.delete(source.levels, 15)
            else:
                master.times[20:] += .03
            with self.subTest(kind=kind), self.assertRaises(StimulusSyncError):
                fit_bl_clock(source, master)

    def test_analog_crossing_uses_half_level_and_retains_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'bl.csv'
            edge = edge_trace(18)
            with path.open('w', encoding='utf-8', newline='') as f:
                f.write('导出数据的原始文件:synthetic.tmen\nTTL，总共19000个数据点，采样率：1000Hz\n')
                writer = csv.writer(f)
                writer.writerow(['序号','时间','2通道数据(mV)','事件'])
                for i in range(19000):
                    state = np.searchsorted(edge.times, i/1000, side='right') % 2
                    writer.writerow([i+1, f'00:{i/1000:06.3f}', 17+721*state, ''])
            with load_bl422(path) as recording:
                observed, report = analog_ttl(recording)
                np.testing.assert_allclose(observed.times, edge.times-.0005, atol=1e-9)
                self.assertEqual(report['low_mv'],17)
                self.assertEqual(report['high_mv'],738)


if __name__ == '__main__':
    unittest.main()
