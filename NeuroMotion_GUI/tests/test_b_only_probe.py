import unittest

from neuromotion.protocol_v2 import BOOT, SAMPLES, NativeSample, V2Decoder, encode_packet
from scripts.probe_b_only_stream import ProbeDecoder, initialization_ok, summarize


class BOnlyProbeTests(unittest.TestCase):
    def setUp(self):
        self.meta = {'firmware': 'NeuroMotion-B-only-probe', 'diagnostic_mode': 'B_only',
                     'ready_mask': 2, 'id_b': 0x73, 'error_b': 0,
                     'registers_b': '7303a010000000004002030c', 'channel_count': 4,
                     'sample_rate_hz': 1000, 'tick_hz': 1000000, 'gains': [6]*4, 'vref_uv': 2420000}
        self.status = dict.fromkeys(['capture_overrun_b', 'dropped_b', 'spi_errors_b', 'late_b',
                                     'status_errors_b', 'record_dropped', 'tx_errors'], 0)
        self.rows = [NativeSample(1, n, 1000*n, 123, -456) for n in range(5000)]

    def test_valid_native_B_stream(self):
        self.assertTrue(initialization_ok(self.meta))
        self.assertEqual(summarize(self.meta, self.rows, self.status, 5, V2Decoder())['status'], 'passed')

    def test_partial_initial_packet_is_separate_from_stable_errors(self):
        decoder = ProbeDecoder()
        packets = decoder.feed(b'partial-packet' + encode_packet(BOOT, self.meta, 10) +
                               encode_packet(SAMPLES, self.rows[:8], 11))
        self.assertEqual(len(packets), 2)
        self.assertGreater(decoder.initial_resync_errors, 0)
        report = summarize(self.meta, self.rows, self.status, 5, decoder)
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['frame_errors_after_lock'], 0)

    def test_error_after_first_valid_frame_in_same_read_is_not_hidden(self):
        bad = bytearray(encode_packet(SAMPLES, self.rows[:8], 11))
        bad[-1] ^= 1
        decoder = ProbeDecoder()
        decoder.feed(b'partial-packet' + encode_packet(BOOT, self.meta, 10) + bytes(bad))
        report = summarize(self.meta, self.rows, self.status, 5, decoder)
        self.assertGreater(report['frame_errors_after_lock'], 0)
        self.assertEqual(report['status'], 'failed')

    def test_id_or_configuration_failure_is_not_healthy(self):
        for change in [{'id_b': 0}, {'error_b': 5}, {'ready_mask': 0},
                       {'registers_b': 'ff'*12}, {'registers_b': 'bad'}, {'firmware': 'normal-four'}]:
            self.assertFalse(initialization_ok({**self.meta, **change}))

    def test_no_samples_gap_wrong_device_and_hardware_errors_fail(self):
        for rows, status in [([], self.status), (self.rows[1:100]+self.rows[101:], self.status),
                             ([NativeSample(0, n, 1000*n, 0, 0) for n in range(5000)], self.status),
                             (self.rows, {**self.status, 'spi_errors_b': 1}), (self.rows, {})]:
            self.assertEqual(summarize(self.meta, rows, status, 5, V2Decoder())['status'], 'failed')

    def test_frame_errors_and_wrong_sampling_rate_fail(self):
        decoder = V2Decoder()
        decoder.frame_errors = 1
        self.assertEqual(summarize(self.meta, self.rows, self.status, 5, decoder)['status'], 'failed')
        rows = [NativeSample(1, n, n*2000, 0, 0) for n in range(5000)]
        self.assertEqual(summarize(self.meta, rows, self.status, 5, V2Decoder())['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
