"""Versioned derived results, content-checked video relocation and portable replay packages."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from .session import file_info, write_json
from .storage import RecordBuffer, close_mapping


def _atomic_json(path, value):
    temporary = path.with_suffix('.json.tmp')
    write_json(temporary, value)
    temporary.replace(path)


def _local(root, name):
    path = (root/name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Derived result path escapes its result directory')
    return path


def write_result_manifest(root: Path, report, session, coverage=None):
    names = ['alignment_report.json', 'replay_signals.csv', 'frame_alignment.csv', 'tracking.csv',
             'aligned_by_frame.csv', 'aligned_by_emg_sample.csv', 'stimulus_events.csv', 'bl_ttl.csv',
             'bl_ttl_edge_matches.csv', 'bl_clock_map.json', 'bl_import_report.json', 'video_ttl.csv', 'data_gaps.csv',
             'ttl_edge_matches.csv', 'qc_alignment.png']
    calibration = {}
    for key, info in report.get('calibration', {}).items():
        if info:
            directory = root/'calibration'; directory.mkdir(exist_ok=True)
            target = directory/f'{key}.json'
            shutil.copy2(info['path'], target)
            calibration[key] = str(target.relative_to(root))
            names.append(calibration[key])
    files = {}
    for name in names:
        path = root/name
        if path.is_file():
            files[name] = {**file_info(path), 'path': name}
    video = dict(report['input_files']['video'])
    manifest = {'schema_version': 1, 'status': 'complete', 'files': files, 'video': video,
                'channel_count': session.channel_count, 'marker_count': report['config']['marker_count'] if (root/'tracking.csv').is_file() else 0,
                'signal_rows': len(session.sample_index), 'frame_rows': report['video']['frame_count'],
                'raw_available': session.raw_available,
                'filtered_available': session.channel(0, filtered=True) is not None,
                'common_coverage_s': coverage, 'calibration': calibration,
                'coordinate_mode': report['config'].get('coordinate_mode', 'metric'),
                'millimeter_coordinates': bool(report.get('millimeter_coordinates', False)),
                'timebase': 'mcu_nominal' if session.channel_count == 4 else 'legacy_video_common',
                'original_sources': report['input_files'], 'absolute_stimulus_onset_validated': False}
    _atomic_json(root/'result_manifest.json', manifest)
    return manifest


def load_result_manifest(root, *, validate=True):
    root = Path(root).resolve()
    manifest = json.loads((root/'result_manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 1 or manifest.get('status') != 'complete':
        raise ValueError('Unsupported or incomplete replay result')
    for name, info in manifest['files'].items():
        path = _local(root, info['path'])
        if validate and (not path.is_file() or file_info(path)['sha256'] != info['sha256']):
            raise ValueError(f'Derived result changed or is missing: {name}; regenerate the result')
    video = Path(manifest['video']['path'])
    if not video.is_absolute():
        video = _local(root, str(video))
    if validate and (not video.is_file() or file_info(video)['sha256'] != manifest['video']['sha256']):
        raise ValueError('Replay video is missing or changed; relocate it to an identical file')
    return manifest, video


def relocate_video(root, new_video):
    root = Path(root).resolve()
    manifest, _ = load_result_manifest(root, validate=False)
    info = file_info(Path(new_video))
    if info['sha256'] != manifest['video']['sha256']:
        raise ValueError('Selected video content does not match the synchronized recording')
    manifest['video'] = info
    _atomic_json(root/'result_manifest.json', manifest)


def export_replay_package(root, destination, *, include_raw=False, cancel_event=None, progress=lambda _: None):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    manifest, video = load_result_manifest(root)
    if destination == root or root.is_relative_to(destination) or destination.is_relative_to(root):
        raise ValueError('Choose a new package directory outside the source result')
    destination.mkdir(parents=True, exist_ok=False)
    def copy(source, relative):
        target = _local(destination, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        progress(f'复制：{relative}')
        with Path(source).open('rb') as src, target.open('xb') as dst:
            while block := src.read(1024*1024):
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError('Replay package cancelled; no complete manifest was written')
                dst.write(block)
        return target
    for name, info in manifest['files'].items():
        copy(_local(root, info['path']), info['path'])
    relative_video = 'video/source'+video.suffix.lower()
    copy(video, relative_video)
    manifest['video'] = {**manifest['video'], 'path': relative_video}
    if include_raw:
        for key, info in manifest['original_sources'].items():
            if file_info(Path(info['path']))['sha256'] != info['sha256']:
                raise ValueError(f'Original source changed: {key}; not archiving a different recording')
            copy(info['path'], f'originals/{key}/{Path(info["path"]).name}')
        emg_dir = Path(manifest['original_sources']['emg_csv']['path']).parent
        for name in ('native_samples.csv', 'serial.bin', 'metadata.json', 'acquisition_report.json',
                     'sync_edges.csv', 'clock_models.jsonl', 'clock_reference.json', 'filter_events.jsonl'):
            if (emg_dir/name).is_file():
                copy(emg_dir/name, f'originals/acquisition/{name}')
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError('Replay package cancelled')
    _atomic_json(destination/'result_manifest.json', manifest)
    load_result_manifest(destination)
    return {'status': 'replay_package_complete', 'directory': str(destination), 'include_raw': include_raw}


def build_derived_index(root, progress=lambda _: None, cancel_event=None):
    from .replay import EMG_DTYPE, EMG4_DTYPE, FRAME_DTYPE
    from .video import TRACK_DTYPE, QUALITY_NAMES
    root = Path(root)
    manifest, video = load_result_manifest(root)
    fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    directory = root/'replay_index'; directory.mkdir(exist_ok=True)
    cache_file = directory/'manifest.json'
    if cache_file.is_file():
        cache = json.loads(cache_file.read_text(encoding='utf-8'))
        required = ['emg.npy', 'emg_time.npy', 'frames.npy', 'frames_time.npy']
        if manifest['marker_count']:
            required.append('tracking.npy')
        if cache.get('schema_version') == 5 and cache['fingerprint'] == fingerprint and all((directory/cache['data_dir']/name).is_file() for name in required):
            return cache
    from uuid import uuid4
    data = directory/('data_'+uuid4().hex); data.mkdir()
    channels = manifest['channel_count']
    def make(name, csv_name, dtype, convert=None):
        progress(f'建立产物索引：{name}')
        buffer = RecordBuffer(dtype, data/f'{name}.bin')
        count = 0
        try:
            for block in pd.read_csv(root/csv_name, chunksize=4096):
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError('Replay indexing cancelled')
                if convert:
                    for row in block.itertuples(index=False):
                        buffer.append(convert(row)); count += 1
                else:
                    missing = set(dtype.names)-set(block.columns)
                    if missing:
                        raise ValueError(f'Replay product is missing {missing}')
                    values = block[list(dtype.names)]
                    for row in values.itertuples(index=False, name=None):
                        buffer.append(row); count += 1
            array = buffer.finish()
            try:
                np.save(data/f'{name}.npy', array)
                if name in ('emg', 'frames'):
                    times = np.lib.format.open_memmap(data/f'{name}_time.npy', mode='w+', dtype='f8', shape=(count,))
                    try:
                        for start in range(0, count, 4096):
                            times[start:start+4096] = array['common_time_s'][start:start+4096]
                    finally:
                        close_mapping(times)
            finally:
                close_mapping(array)
            (data/f'{name}.bin').unlink()
            return count
        finally:
            buffer.close()
    emg_count = make('emg', 'replay_signals.csv', EMG4_DTYPE if channels == 4 else EMG_DTYPE)
    frame_count = make('frames', 'frame_alignment.csv', FRAME_DTYPE)
    marker_count = manifest['marker_count']
    if marker_count:
        count = make('tracking', 'tracking.csv', TRACK_DTYPE,
                     lambda r: (r.frame_index, r.particle_id, r.u_px, r.v_px, r.X_mm, r.Y_mm,
                                r.detected, QUALITY_NAMES.index(r.quality_flag)))
        if count != frame_count*marker_count:
            raise ValueError('Tracking frame/marker count mismatch')
    cache = {'schema_version': 5, 'fingerprint': fingerprint, 'data_dir': data.name, 'channel_count': channels,
             'emg_count': emg_count, 'frame_count': frame_count, 'marker_count': marker_count,
             'raw_available': manifest['raw_available'], 'filtered_available': manifest['filtered_available'],
             'common_coverage_s': manifest['common_coverage_s'], 'sources': []}
    _atomic_json(cache_file, cache)
    return cache


def replay_report(root):
    root = Path(root)
    manifest, video = load_result_manifest(root, validate=False)
    report = json.loads((root/'alignment_report.json').read_text(encoding='utf-8'))
    report['input_files']['video']['path'] = str(video)
    for key, name in manifest['calibration'].items():
        report.setdefault('calibration', {})[key] = {**manifest['files'][name], 'path': str(_local(root, name))}
    return report


def read_replay_events(root):
    path = Path(root)/'stimulus_events.csv'
    if not path.is_file():
        return []
    with path.open(encoding='utf-8', newline='') as stream:
        events = list(csv.DictReader(stream))
    for event in events:
        for key in ('common_time_s', 'bl_time_s', 'commanded_current_ma', 'pulse_width_ms'):
            value = event.get(key)
            event[key] = float(value) if value else None
        event['in_common_coverage'] = event.get('in_common_coverage', '').lower() in ('true', '1')
    return events
