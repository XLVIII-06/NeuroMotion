"""Bounded real dual-ADC recording and streaming conservation checks.

An optional reset verifies the supplied image first. No firmware is flashed.
This validates capture/transport/files, never analog timing or animal signals.
"""
from pathlib import Path
import argparse
import csv
import json
import math
import sys
import time
import traceback

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neuromotion.acquisition import AcquisitionController
from neuromotion.filters import FilterSettings
from neuromotion.protocol_v2 import SAMPLES, V2Decoder
from neuromotion.session import file_info, write_json


def receiver_counters(state, baseline=None):
    baseline = baseline or {}
    return {key: state.get(key, 0)-baseline.get(key, 0) for key in
            ('frame_errors', 'invalid_records', 'index_gaps', 'packet_gaps')}


def require_healthy_stream(state, baseline=None):
    """Check receiver counters as well as the MCU; neither implies the other."""
    if state['error']:
        raise RuntimeError(state['error'])
    receiver = receiver_counters(state, baseline)
    if any(receiver.values()):
        raise RuntimeError(f'Receiver data loss: {receiver}')
    hardware = (state.get('clock') or {}).get('hardware') or {}
    if any(v for k, v in hardware.items() if k != 'max_read_delay_us'):
        raise RuntimeError(f'MCU error counters: {hardware}')


def validate(args):
    if args.reset_hex:
        from scripts.reset_mcu_diag import reset_diagnostic
        # All Python imports are complete before reset; arm serial/recording
        # during the firmware's five-second delay, before the TTL start code.
        reset_diagnostic(args.reset_hex.resolve(), args.output/'verified_reset')
    controller = AcquisitionController()
    started = time.monotonic()
    active_started = None
    last_progress = 0
    history_peak = checks = 0
    changed = False
    try:
        controller.connect(port=args.port)
        # Lock the wire format before opening the recording. A port may begin
        # with the tail of a frame sent before reset. Preserve that diagnostic,
        # but never call pre-recording resynchronization recorded sample loss.
        while controller.thread.is_alive():
            initial = controller.snapshot(include_plot=False)
            if initial['error']:
                raise RuntimeError(initial['error'])
            if initial['valid_frames'] and initial['channel_count'] == 4:
                if (initial.get('clock') or {}).get('metadata', {}).get('ready_mask') != 3:
                    raise RuntimeError('Both ADS devices must initialize before recording')
                break
            if time.monotonic()-started > 4:
                raise TimeoutError('Four-channel startup metadata not received before sampling')
            controller.thread.join(.01)
        else:
            raise RuntimeError('Acquisition ended before startup metadata')
        if args.reset_hex and initial['samples']:
            raise RuntimeError('Startup handshake missed the pre-sampling recording window')
        controller.start_recording(args.output/'session')
        baseline = dict(controller.record_baseline)
        while controller.thread.is_alive():
            state = controller.snapshot(max_points=2000, plot_window_s=.05)
            now = time.monotonic()
            checks += 1
            history_peak = max(history_peak, len(controller.history))
            clock = state.get('clock') or {}
            hardware = clock.get('hardware') or {}
            require_healthy_stream(state, baseline)
            if state['history_bounds_s']:
                if active_started is None:
                    active_started = now
                active_elapsed = now-active_started
                if active_elapsed > 2 and (state['last_sample_age_s'] > 2 or
                        any(d['state'] in ('waiting', 'degraded') for d in clock['devices'])):
                    raise RuntimeError('One or both native streams stalled')
                if active_elapsed >= args.seconds and state['history_bounds_s'][1] >= args.seconds:
                    break
            if state.get('native_recorded_samples', 0) > 100000 and not changed:
                controller.update_filters(FilterSettings(high_pass_hz=20, low_pass_hz=200, notch_50hz=True))
                changed = True
            if now-last_progress >= 5:
                progress = dict(wall_s=round(now-started, 2), native=state['samples'], grid=state['recorded_samples'],
                                relative_ppm=clock.get('relative_ppm'), hardware=hardware,
                                receiver=receiver_counters(state, baseline),
                                transport=state.get('transport', {}))
                write_json(args.output/'progress.json', progress)
                print(json.dumps(progress), flush=True)
                frozen = controller.snapshot(max_points=60000)['plot'].copy()
                assert len(frozen) <= 60000
                last_progress = now
            if now-started > args.seconds+60:
                raise TimeoutError('Required continuous capture duration was not reached')
            controller.thread.join(.1)
    finally:
        controller.disconnect()
    controller.wait()
    state = controller.snapshot(include_plot=False)
    folder = args.output/'session/acquisition'
    decoder = V2Decoder()
    count = 0
    device_counts = [0, 0]
    last = [None, None]
    longest_interval = [0, 0]
    with (folder/'serial.bin').open('rb') as binary, (folder/'native_samples.csv').open(newline='') as stream:
        rows = csv.DictReader(stream)
        while data := binary.read(65536):
            for packet in decoder.feed(data):
                if packet.kind != SAMPLES:
                    continue
                for sample in packet.value:
                    row = next(rows)
                    actual = tuple(int(row[k]) for k in ('device', 'native_index', 'drdy_time_us', 'ch1_code',
                                                         'ch2_code', 'adc_status', 'flags', 'known_dropped'))
                    expected = (sample.device, sample.index, sample.time_us, sample.code1, sample.code2,
                                sample.status, sample.flags, sample.dropped)
                    assert actual == expected and sample.valid, (count, actual, expected)
                    previous = last[sample.device]
                    if previous:
                        interval = sample.time_us-previous.time_us
                        assert sample.index == previous.index+1 and 900 <= interval <= 1100
                        longest_interval[sample.device] = max(longest_interval[sample.device], interval)
                    last[sample.device] = sample
                    count += 1
                    device_counts[sample.device] += 1
        assert next(rows, None) is None
    grid_count = 0
    previous = None
    nan_counts = np.zeros(4, dtype=np.int64)
    revisions = set()
    columns = ['sample_index', 'mcu_time_us', 'filter_revision']+[f'ch{i}_aligned_uv' for i in range(1, 5)]
    for block in pd.read_csv(folder/'emg.csv', usecols=columns, chunksize=8192):
        ids = block.sample_index.to_numpy(dtype=np.int64)
        ticks = block.mcu_time_us.to_numpy(dtype=np.int64)
        assert (np.diff(ids) == 1).all() and (np.diff(ticks) == 1000).all()
        if previous:
            assert (ids[0], ticks[0]) == (previous[0]+1, previous[1]+1000)
        previous = int(ids[-1]), int(ticks[-1])
        grid_count += len(block)
        nan_counts += np.isnan(block[[f'ch{i}_aligned_uv' for i in range(1, 5)]].to_numpy()).sum(axis=0)
        revisions.update(int(x) for x in block.filter_revision.unique())
    assert count == state['native_recorded_samples']
    assert grid_count == state['recorded_samples'] and grid_count >= args.seconds*1000-10
    assert min(device_counts) >= args.seconds*990
    assert not decoder.frame_errors and not decoder.invalid_records and not decoder.sequence_gaps
    assert not state['error'] and not state['index_gaps'] and max(nan_counts) < 100 and history_peak <= 60000
    assert state['start_marker'] == 'verified' if args.reset_hex else True
    if changed:
        assert len(revisions) >= 2
    clock = state['clock']
    if args.seconds >= 25:
        assert clock['fixed_reference'] is not None
    return dict(status='passed', evidence_kind='real_dual_adc_production_recording', port=args.port,
                requested_active_seconds=args.seconds, wall_seconds=time.monotonic()-started,
                native_records=count, device_counts=device_counts, aligned_rows=grid_count,
                binary_native_csv_exact=True, maximum_drdy_interval_us=longest_interval,
                nan_rows_by_channel=nan_counts.tolist(), history_peak=history_peak, snapshot_checks=checks,
                filter_revisions=sorted(revisions), frame_errors=decoder.frame_errors, packet_gaps=decoder.sequence_gaps,
                start_marker=state['start_marker'], clock=clock, binary=file_info(folder/'serial.bin'),
                startup_receiver_counters=receiver_counters(initial),
                receiver_counters=receiver_counters(state, baseline), transport=state.get('transport', {}),
                capture_transport_validated=True, analog_timing_validated=False, physiological_quality_validated=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default='COM5')
    parser.add_argument('--seconds', type=float, default=300)
    parser.add_argument('--reset-hex', type=Path, help='Explicitly verify and reset this image; never flash')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds < 10:
        parser.error('Use at least ten active seconds')
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        result = validate(args)
    except Exception:
        result = dict(status='failed', error=traceback.format_exc(), capture_transport_validated=False,
                      analog_timing_validated=False)
    write_json(args.output/'report.json', result)
    print(json.dumps(result, indent=2), flush=True)
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
