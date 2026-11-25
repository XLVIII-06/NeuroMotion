from __future__ import annotations

import unittest
import csv
import json
from pathlib import Path
import tempfile
import numpy as np

from neuromotion.clock_alignment import ClockAlignedStream
from neuromotion.protocol_v2 import (BOOT, META, SAMPLES, STATUS, SYNC_EDGE, DATA_VALID, SPI_ERROR,
                                     NativeSample, SyncEdge, Packet, V2Decoder, encode_packet)


def metadata():
    return {"firmware": "test", "channel_count": 4, "sample_rate_hz": 1000, "tick_hz": 1000000,
            "vref_uv": 2420000, "gains": [6, 6, 6, 6], "ready_mask": 3, "ttl_start_us": 0}


def samples(seconds=3, ppm=50, offset=270, missing=(), reset_at=None):
    factor = 2420000 / (8388608 * 6)
    events = [SyncEdge(0, 0, 1)]
    for d in (0, 1):
        for i in range(int(seconds * 1000)):
            t = 4000 + d * offset + i * 1000 / (1 + d * ppm * 1e-6)
            if d == 1 and i in missing:
                continue
            code = int(round((100 * np.sin(2 * np.pi * 73 * t / 1e6) + 30 * np.sin(2 * np.pi * 181 * t / 1e6)) / factor))
            index = i if d == 0 or reset_at is None or i < reset_at else i - reset_at
            events.append(NativeSample(d, index, round(t), code, -code))
    return sorted(events, key=lambda e: e.time_us)


def run_stream(events, chunk=113):
    stream, blocks = ClockAlignedStream(), []
    for start in range(0, len(events), chunk):
        blocks.extend(stream.feed(events[start:start + chunk]))
        stream.pop_model_events()
    blocks.extend(stream.finish())
    return stream, np.concatenate([b.time_us for b in blocks]), np.concatenate([b.values for b in blocks])


class V2Tests(unittest.TestCase):
    def test_crc_splits_and_corrupt_length_recover(self):
        data = encode_packet(BOOT, metadata()) + encode_packet(SAMPLES, [NativeSample(1, 8, 4294967299, -8388608, 8388607)], 1)
        for cut in range(1, len(data)):
            decoder = V2Decoder()
            packets = decoder.feed(data[:cut]) + decoder.feed(data[cut:])
            self.assertEqual(packets[-1].value[0].time_us, 4294967299)
            self.assertEqual(packets[-1].value[0].code1, -8388608)
        corrupt = bytearray(data); corrupt[6:8] = b"\xff\xff"
        decoder = V2Decoder()
        packets = decoder.feed(bytes(corrupt) + data)
        self.assertGreater(decoder.frame_errors, 0)
        self.assertEqual(packets[-1].value[0].code2, 8388607)
        self.assertLess(len(decoder.buffer), 1041)

    def test_native_invalid_data_remain_diagnosable(self):
        s = NativeSample(0, 9, 999, 0, 0, flags=SPI_ERROR)
        packet = V2Decoder().feed(encode_packet(SAMPLES, [s]))[0]
        self.assertFalse(packet.value[0].valid)
        self.assertEqual(packet.value[0].flags, SPI_ERROR)
        self.assertFalse(NativeSample(0, 1, 2, 0, 0, status=0).valid)

    def test_crc_corruption_and_sequence_gap(self):
        bad = bytearray(encode_packet(META, metadata(), 1)); bad[-1] ^= 1
        decoder = V2Decoder()
        packets = decoder.feed(encode_packet(BOOT, metadata(), 0) + bytes(bad) + encode_packet(SYNC_EDGE, SyncEdge(0, 10, 1), 2))
        self.assertEqual(len(packets), 2)
        self.assertEqual(decoder.sequence_gaps, 1)

    def test_crc_valid_invalid_metadata_is_rejected_before_acquisition(self):
        for change in ({"gains": [True, 6, 6, 6]}, {"gains": 6}, {"channel_count": 4.0},
                       {"ttl_start_us": -1}, {"ready_mask": True}):
            decoder = V2Decoder()
            wire = encode_packet(META, {**metadata(), **change}, 0) + encode_packet(META, metadata(), 1)
            packets = decoder.feed(wire)
            self.assertEqual(len(packets), 1)
            self.assertEqual(packets[0].sequence, 1)
            self.assertEqual(decoder.invalid_records, 1)


class FourClockTests(unittest.TestCase):
    def test_frequency_band_rms_and_burst_activation_delay(self):
        scale = 2420000/(8388608*6)
        for frequency in (20, 50, 100, 180, 200):
            events = []
            for device in (0, 1):
                n = np.arange(3000)
                t = 4000 + device*1700 + n*1000/(1+device*120e-6)
                codes = np.rint(100*np.sin(2*np.pi*frequency*t/1e6)/scale).astype(int)
                events.extend(NativeSample(device,int(i),round(x),int(c),int(c)) for i,x,c in zip(n,t,codes))
            _, times, values = run_stream(sorted(events,key=lambda s:s.time_us))
            valid = np.isfinite(values).all(axis=1)
            expected = 100*np.sin(2*np.pi*frequency*times[valid]/1e6)
            relative_rms = np.sqrt(np.mean((values[valid]-expected[:,None])**2,axis=0)/np.mean(expected**2))
            self.assertLess(relative_rms.max(), .01, (frequency, relative_rms))
        delays = (0, 0, .012, .025)
        events = []
        for device in (0, 1):
            n = np.arange(5000)
            t = (4000+device*270+n*1000/(1-device*100e-6))/1e6
            shifted = t[:,None]-np.array(delays[device*2:device*2+2])
            wave = 100*np.exp(-((shifted-2.5)/.15)**2)*np.sin(2*np.pi*100*shifted)
            codes = np.rint(wave/scale).astype(int)
            events.extend(NativeSample(device,int(i),round(x*1e6),int(c[0]),int(c[1])) for i,x,c in zip(n,t,codes))
        _, times, values = run_stream(sorted(events,key=lambda s:s.time_us))
        energy = np.nan_to_num(values)**2
        center = (energy*(times[:,None]/1e6)).sum(axis=0)/energy.sum(axis=0)
        np.testing.assert_allclose(center-center[0],delays,atol=.001)

    def test_offset_frequency_reconstruction_and_chunk_independence(self):
        events = samples(5, ppm=-100, offset=3550)
        a, t, v = run_stream(events, 97)
        b, t2, v2 = run_stream(events, 4096)
        np.testing.assert_array_equal(t, t2)
        np.testing.assert_allclose(v, v2, equal_nan=True, atol=1e-12)
        expected = 100*np.sin(2*np.pi*73*t/1e6)+30*np.sin(2*np.pi*181*t/1e6)
        valid = np.isfinite(v).all(axis=1)
        self.assertLess(np.sqrt(np.mean((v[valid,0]-expected[valid])**2)), .1)
        self.assertLess(np.max(abs(v[valid,0]-v[valid,2])), .2)
        self.assertAlmostEqual(a.status()["relative_ppm"], -100, delta=.1)
        self.assertTrue((np.diff(t)==1000).all())

    def test_gap_invalidates_fir_support_only_on_affected_board(self):
        _, t, v = run_stream(samples(3, missing=range(1000,1010)))
        center = (t > 995000) & (t < 1050000)
        self.assertTrue(np.isfinite(v[center,:2]).all())
        self.assertTrue(np.isnan(v[center,2:]).any())
        self.assertTrue(np.isfinite(v[(t>1200000)&(t<2000000)]).all())

    def test_module_reset_rewarms_and_other_board_continues(self):
        stream,t,v = run_stream(samples(4,reset_at=1500))
        self.assertEqual(stream.clocks[1].resets,1)
        self.assertTrue(np.isfinite(v[(t>1100000)&(t<1450000)]).all())
        self.assertTrue(np.isfinite(v[(t>1700000)&(t<2200000),:2]).all())
        self.assertTrue(np.isnan(v[(t>1500000)&(t<1750000),2:]).any())
        self.assertTrue(np.isfinite(v[(t>2100000)&(t<3000000)]).all())

    def test_timer_wrap_is_extended_not_reset(self):
        shift=2**32-1500000
        events=[NativeSample(e.device,e.index,e.time_us+shift,e.code1,e.code2) if isinstance(e,NativeSample)
                else SyncEdge(e.index,e.time_us+shift,e.level) for e in samples(3)]
        stream,t,v=run_stream(events)
        self.assertGreater(t[-1],2**32)
        self.assertEqual(stream.clocks[0].resets,0)
        self.assertTrue(np.isfinite(v[100:-100]).all())

    def test_missing_board_does_not_block_grid(self):
        events=[e for e in samples(2) if not isinstance(e,NativeSample) or e.device==0]
        _,t,v=run_stream(events)
        self.assertGreater(len(t),1900)
        self.assertTrue(np.isnan(v[:,2:]).all())
        self.assertTrue(np.isfinite(v[100:-100,:2]).all())

    def test_reference_is_automatic_and_raw_values_are_not_modified(self):
        events=samples(22,ppm=50)
        initial=events[100]
        stream,_,_=run_stream(events)
        self.assertIsNotNone(stream.clocks[0].reference)
        self.assertIsNotNone(stream.clocks[1].reference)
        self.assertIsNotNone(stream.fixed_reference)
        self.assertAlmostEqual(stream.fixed_reference['relative_ppm'],50,delta=.1)
        self.assertEqual(events[100],initial)
        self.assertEqual(initial.flags,DATA_VALID)


class FourRecordingTests(unittest.TestCase):
    def test_status_packets_do_not_hide_a_native_sampling_stall(self):
        from unittest.mock import patch
        from neuromotion.acquisition import AcquisitionController
        controller = AcquisitionController()
        with patch("neuromotion.acquisition.time.monotonic", return_value=100):
            controller._decode(encode_packet(BOOT, metadata(), 0) + encode_packet(SAMPLES, [NativeSample(0, 1, 1000, 1, 2)], 1))
        with patch("neuromotion.acquisition.time.monotonic", return_value=110):
            controller._decode(encode_packet(STATUS, {"dropped_a": 0, "dropped_b": 0}, 2))
            self.assertEqual(controller.snapshot(include_plot=False)["last_sample_age_s"], 10)

    def test_fallback_mcu_reset_preserves_previous_model_events(self):
        from neuromotion.recording_v2 import FourChannelRecorder
        from neuromotion.filters import FilterSettings
        with tempfile.TemporaryDirectory() as d:
            recorder = FourChannelRecorder(FilterSettings(high_pass_hz=0), metadata())
            recorder.open(Path(d))
            events = [e for e in samples(1) if isinstance(e, NativeSample)]
            events += [NativeSample(0, 0, 4, 1, 2)]
            _, reset = recorder.on_packet(Packet(SAMPLES, 0, events))
            recorder.finish(); recorder.close()
            changes = [json.loads(line) for line in (Path(d)/"acquisition/clock_models.jsonl").read_text().splitlines()]
            self.assertTrue(reset)
            self.assertTrue(changes)
            self.assertEqual({c["segment_id"] for c in changes}, {0})

    def test_invalid_v2_magic_in_noise_does_not_latch_legacy_connection(self):
        from neuromotion.acquisition import AcquisitionController,BinaryReplaySource,encode_frame
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);wire=root/'noise.bin'
            wire.write_bytes(b'NM2!\x09\x01\x00\x00garbage'+encode_frame([(i,1.,2.,0,0) for i in range(12)]))
            c=AcquisitionController();c.connect(source=BinaryReplaySource(wire,speed=0));c.start_recording(root/'session');c.wait()
            self.assertEqual(c.snapshot()['channel_count'],2)
            self.assertEqual(c.record_count,12)

    def test_simulated_native_binary_csv_and_aligned_output(self):
        from neuromotion.acquisition import AcquisitionController, BinaryReplaySource
        from neuromotion.simulated_v2 import SimulatedFourChannelSource
        from neuromotion.filters import FilterSettings
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);ctl=AcquisitionController(FilterSettings(high_pass_hz=0))
            ctl.connect(source=SimulatedFourChannelSource(4.5,speed=0));ctl.start_recording(root/'first');ctl.wait()
            folder=root/'first/acquisition'
            decoder=V2Decoder(); packets=decoder.feed((folder/'serial.bin').read_bytes())
            native=[s for p in packets if p.kind==SAMPLES for s in p.value]
            with (folder/'native_samples.csv').open() as f: saved=list(csv.DictReader(f))
            self.assertEqual(len(native),len(saved))
            for s,row in zip(native,saved):
                self.assertEqual((s.device,s.index,s.time_us,s.code1,s.code2),tuple(int(row[k]) for k in ('device','native_index','drdy_time_us','ch1_code','ch2_code')))
            with (folder/'emg.csv').open() as f: rows=list(csv.DictReader(f))
            self.assertGreater(len(rows),4400)
            self.assertEqual(ctl.snapshot()['channel_count'],4)
            self.assertEqual(ctl.snapshot()['plot'].shape[1],12)
            self.assertEqual(ctl.snapshot()['start_marker'],'verified')
            inner=rows[100:-100]
            self.assertTrue(all(np.isfinite([float(r[f'ch{i}_aligned_uv']) for i in range(1,5)]).all() for r in inner))
            self.assertEqual(json.loads((folder/'acquisition_report.json').read_text())['native_recorded_samples'],len(native))
            other=AcquisitionController(FilterSettings(high_pass_hz=0))
            other.connect(source=BinaryReplaySource(folder/'serial.bin',speed=0));other.start_recording(root/'replay');other.wait()
            self.assertEqual((root/'replay/acquisition/emg.csv').read_bytes(),(folder/'emg.csv').read_bytes())


if __name__ == "__main__":
    unittest.main()
