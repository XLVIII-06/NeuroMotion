"""BL annotations and synchronized products; voltage/current/event meanings stay separate."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from .bl422 import load_bl422
from .session import write_json
from .stimulus_sync import analog_ttl, fit_bl_clock, master_ttl


EVENT_COLUMNS = ['event_id', 'source_row', 'source_sequence', 'bl_time_s', 'common_time_s',
                 'commanded_current_ma', 'pulse_width_ms', 'notes', 'event_kind', 'raw_event_text',
                 'parameters_source', 'absolute_onset_validated', 'in_common_coverage', 'timing_quality']


def write_signal_product(path, session, alignment, cancel_event=None):
    channels = session.channel_count
    fields = ['sample_index', 'common_time_s', 'sync', 'revision']
    fields += [f'{branch}{i}' for branch in ('primary', 'raw', 'filtered', 'quality', 'filter_quality') for i in range(1, channels+1)]
    with Path(path).open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for first in range(0, len(session.sample_index), 4096):
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError('Signal export cancelled')
            last = min(first+4096, len(session.sample_index))
            times = alignment.common_time_from_sample(session.sample_index[first:last])
            for offset, j in enumerate(range(first, last)):
                row = dict(sample_index=int(session.sample_index[j]), common_time_s=float(times[offset]),
                           sync=int(session.sync[j]), revision=int(session.filter_revision[j]) if session.filter_revision is not None else -1)
                for c in range(channels):
                    primary, filtered = session.channel(c), session.channel(c, filtered=True)
                    row.update({f'primary{c+1}': float(primary[j]),
                                f'raw{c+1}': float(primary[j]) if session.raw_available else math.nan,
                                f'filtered{c+1}': float(filtered[j]) if filtered is not None else math.nan,
                                f'quality{c+1}': int(session.channel_quality[j, c]) if session.channel_quality is not None else 0,
                                f'filter_quality{c+1}': int(session.filter_quality[j, c]) if session.filter_quality is not None else 0})
                writer.writerow(row)


def _annotate_table(path, key, bins, coverage, cancel_event):
    temporary = path.with_suffix('.events.tmp')
    extra = ['stim_event_count', 'stim_event_ids']
    try:
        with path.open(encoding='utf-8', newline='') as source, temporary.open('w', encoding='utf-8', newline='') as target:
            reader = csv.DictReader(source)
            writer = csv.DictWriter(target, fieldnames=list(reader.fieldnames)+extra)
            writer.writeheader()
            for i, row in enumerate(reader):
                if i % 4096 == 0 and cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError('Event fusion cancelled')
                t = float(row['common_time_s'])
                if not coverage[0] <= t <= coverage[1]:
                    continue
                events = bins.get(int(row[key]), [])
                writer.writerow({**row, 'stim_event_count': len(events), 'stim_event_ids': json.dumps(events)})
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def fuse_stimulus(config, report, session, alignment, trace, cancel_event=None):
    """Called only after EMG/video alignment; clock mapping never uses video as a BL intermediary."""
    root = config.output_dir
    with load_bl422(config.bl_csv, parameters_path=config.stimulus_parameters_path, cancel_event=cancel_event) as recording:
        source, voltage = analog_ttl(recording)
        clock = fit_bl_clock(source, master_ttl(session, alignment),
                             rms_limit_s=config.bl_rms_limit_ms/1000, max_limit_s=config.bl_max_limit_ms/1000)
        frame_times = np.asarray(alignment.common_time_from_video(trace.time_s))
        coverage = [max(float(alignment.common_time_from_sample(session.sample_index[0])), float(frame_times[0]), float(clock.map(clock.source[0]))),
                    min(float(alignment.common_time_from_sample(session.sample_index[-1])), float(frame_times[-1]), float(clock.map(clock.source[-1])))]
        if not coverage[0] < coverage[1]:
            raise ValueError('No trusted common coverage across BL, EMG and video')
        mapped = clock.map([e['bl_time_s'] for e in recording.events])
        events, sample_bins, frame_bins = [], {}, {}
        for original, time in zip(recording.events, mapped):
            inside = bool(np.isfinite(time) and coverage[0] <= time <= coverage[1])
            row = {**original, 'common_time_s': float(time) if np.isfinite(time) else None,
                   'in_common_coverage': inside, 'timing_quality': 'ttl_mapped_annotation' if np.isfinite(time) else 'outside_trusted_clock'}
            events.append(row)
            if not inside:
                continue
            sample_float = (time*alignment.master_rate_hz+alignment.master_start_sample if alignment.master_start_sample is not None
                            else alignment.fitted_start_sample+time*alignment.slope_samples_per_second)
            sample = int(np.floor(sample_float+1e-8))
            sample_bins.setdefault(sample, []).append(row['event_id'])
            frame = int(np.searchsorted(frame_times, time, side='right')-1)
            frame_bins.setdefault(frame, []).append(row['event_id'])
        with (root/'stimulus_events.csv').open('w', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=EVENT_COLUMNS)
            writer.writeheader(); writer.writerows(events)
        with (root/'bl_ttl.csv').open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['source_sequence', 'bl_time_s', 'ttl_mv', 'level', 'common_time_s', 'clock_quality'])
            for first in range(0, len(recording.samples), 4096):
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError('BL TTL export cancelled')
                block = recording.samples[first:first+4096]
                times = block['time_us']/1e6
                common = clock.map(times)
                index = np.searchsorted(source.times, times, side='right')-1
                levels = np.where(index >= 0, source.levels[np.maximum(index, 0)], source.initial_level)
                if clock.report['source_inverted']:
                    levels = 1-levels
                writer.writerows((int(r['sequence']), float(t), float(r['ttl_mv']), int(level), float(c) if np.isfinite(c) else '',
                                 'mapped' if np.isfinite(c) else 'outside_trusted_clock') for r, t, c, level in zip(block, times, common, levels))
        pd.DataFrame(clock.report['edge_validation']).rename(columns={'source_time_s': 'bl_edge_time_s',
            'master_time_s': 'common_time_s'}).to_csv(root/'bl_ttl_edge_matches.csv', index=False)
        write_json(root/'bl_clock_map.json', {**clock.report, 'source_anchors_s': clock.source.tolist(),
                                            'master_anchors_s': clock.master.tolist(), 'voltage_detection': voltage,
                                            'common_coverage_s': coverage})
        write_json(root/'bl_import_report.json', recording.summary())
        for name, key, bins in [('aligned_by_frame.csv', 'frame_index', frame_bins),
                                ('aligned_by_emg_sample.csv', 'sample_index', sample_bins)]:
            if (root/name).is_file():
                _annotate_table(root/name, key, bins, coverage, cancel_event)
        report['input_files']['bl_csv'] = recording.metadata['source']
        if config.stimulus_parameters_path:
            report['input_files']['stimulus_parameters'] = recording.metadata['parameter_file']
        report['stimulus'] = {'event_count': len(events), 'events_in_common_coverage': sum(e['in_common_coverage'] for e in events),
                              'event_semantics': 'single_pulse_start_annotation', 'absolute_onset_validated': False,
                              'common_coverage_s': coverage, 'clock': clock.report,
                              'unknown_pulse_width_count': sum(e['pulse_width_ms'] is None for e in events)}
        report['timing_limits']['absolute_stimulus_emg_latency_calibrated'] = False
        report['warnings'].append('BL events are pulse-start annotations and commanded mA settings, not measured stimulus current or calibrated absolute onset.')
        return coverage
