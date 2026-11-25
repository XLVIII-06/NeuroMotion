"""Bounded-memory 30-minute BL import/clock fitting and four-channel derived replay benchmark."""
import argparse
import csv
from pathlib import Path
import sys
import time
import tracemalloc
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cv2
import numpy as np
import pandas as pd

from neuromotion.bl422 import load_bl422
from neuromotion.replay import EMG4_DTYPE, ReplayEngine
from neuromotion.result_store import write_result_manifest
from neuromotion.session import file_info, write_json
from neuromotion.stimulus_sync import TTLTrace, analog_ttl, protocol, fit_bl_clock
from neuromotion.validation import ttl_state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=1800)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    root = args.output
    count = args.seconds*1000
    bl = root/'synthetic_bl.csv'
    print('Writing explicitly synthetic BL fixture', flush=True)
    with bl.open('w', encoding='utf-8', newline='') as stream:
        stream.write(f'导出数据的原始文件:synthetic.tmen\nTTL，总共{count}个数据点，采样率：1000Hz\n')
        writer = csv.writer(stream); writer.writerow(['序号', '时间', '2通道数据(mV)', '事件'])
        for n in range(count):
            t = n/1000
            state = ttl_state(t) if t < 4 else int(int((t-4)*2) % 2 == 0)
            writer.writerow([n+1, f'{n//60000:02}:{(n%60000)/1000:06.3f}', 17+721*state,
                             'Stimulate: -1mA' if n > 4000 and n % 10000 == 0 else ''])
    started = time.monotonic(); tracemalloc.start()
    with load_bl422(bl) as record:
        trace, _ = analog_ttl(record); p = protocol(trace)
        clock = fit_bl_clock(trace, TTLTrace(trace.times*1.0001+2, trace.levels.copy()))
        bl_report = {'samples': len(record.samples), 'events': len(record.events),
                     'stable_edges': len(p['stable_indices']), 'disk_backed': isinstance(record.samples, np.memmap),
                     'clock_validation': clock.report['validation']}
    bl_report.update(elapsed_s=time.monotonic()-started, python_peak_bytes=tracemalloc.get_traced_memory()[1])
    tracemalloc.stop(); print(bl_report, flush=True)
    result = root/'result'; result.mkdir()
    movie = root/'synthetic.avi'
    writer = cv2.VideoWriter(str(movie), cv2.VideoWriter_fourcc(*'MJPG'), 1, (96, 64))
    if not writer.isOpened():
        raise RuntimeError('Synthetic video encoder unavailable')
    for i in range(args.seconds):
        writer.write(np.full((64, 96, 3), i % 255, dtype=np.uint8))
    writer.release()
    print('Writing synthetic derived four-channel records', flush=True)
    for first in range(0, count, 4096):
        n = np.arange(first, min(count, first+4096)); t = n/1000
        fields = {key: np.zeros(len(n), dtype=dtype) for key, (dtype, _) in EMG4_DTYPE.fields.items()}
        fields.update(sample_index=n, common_time_s=t, sync=(t % 1 < .5).astype(int))
        for ch in range(1, 5):
            fields[f'primary{ch}'] = 100*ch*np.sin(t*2*np.pi*(20+ch*20))
            fields[f'raw{ch}'] = np.full(len(n), np.nan)
            fields[f'filtered{ch}'] = np.full(len(n), np.nan)
        pd.DataFrame(fields).to_csv(result/'replay_signals.csv', index=False, mode='w' if first == 0 else 'a', header=first == 0)
    times = np.arange(args.seconds)
    pd.DataFrame({'frame_index': times, 'video_time_s': times, 'common_time_s': times,
                  'led_state': times % 2}).to_csv(result/'frame_alignment.csv', index=False)
    with (result/'tracking.csv').open('w', encoding='utf-8', newline='') as stream:
        w = csv.writer(stream); w.writerow(['frame_index','particle_id','u_px','v_px','X_mm','Y_mm','detected','quality_flag'])
        for i in times:
            w.writerows((i, ch, 10+ch*10, 20, '', '', True, 'observed') for ch in range(5))
    report = {'status': 'full_fusion_generated', 'fixture_only': True, 'config': {'marker_count': 5, 'coordinate_mode': 'pixel'},
              'input_files': {'video': file_info(movie)}, 'video': {'frame_count': args.seconds, 'width': 96, 'height': 64},
              'alignment': {'slope_samples_per_second': 1000, 'intercept_samples': 0},
              'emg_sessions': [], 'calibration': {}, 'millimeter_coordinates': False}
    write_json(result/'alignment_report.json', report)
    session = SimpleNamespace(channel_count=4, sample_index=range(count), raw_available=False, channel=lambda *a, **kw: None)
    write_result_manifest(result, report, session)
    started = time.monotonic(); tracemalloc.start()
    engine = ReplayEngine(result, progress=lambda s: print(s, flush=True))
    index_s = time.monotonic()-started
    try:
        self_count = len(engine.emg)
        if self_count != count or not isinstance(engine.emg, np.memmap):
            raise AssertionError('Replay was not a complete disk-backed index')
        latencies = []
        for target in (0, args.seconds*.1, args.seconds*.5, args.seconds*.8, args.seconds-2):
            started = time.monotonic(); snap = engine.seek(target)
            latencies.append(time.monotonic()-started)
            if len(snap['positions']) != 5 or len(snap['emg_window']) > 5000:
                raise AssertionError('Replay did not return bounded windows and all five markers')
        replay_report = {'samples': self_count, 'channel_count': 4, 'index_s': index_s,
                         'random_seek_s': latencies, 'python_peak_bytes': tracemalloc.get_traced_memory()[1]}
    finally:
        engine.close(); tracemalloc.stop()
    # Python allocations only, not the OS page cache or native video decoder RSS.
    if max(bl_report['python_peak_bytes'], replay_report['python_peak_bytes']) > 64*1024**2:
        raise AssertionError('Python metadata heap exceeded 64 MiB budget')
    write_json(root/'scale_validation.json', {'status': 'passed', 'synthetic': True, 'duration_s': args.seconds,
               'memory_metric': 'tracemalloc Python allocations; excludes native decoder/OS mapped pages',
               'bl': bl_report, 'replay': replay_report})
    print(replay_report, flush=True)


if __name__ == '__main__':
    main()
