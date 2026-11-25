"""Observe a common analog bench stimulus; never align physiological signals.

Reads unchanged acquisition files. Each edge uses local plateaus and a unique
50% crossing with linear sub-sample interpolation. Noise-only intervals are
explicitly distinguished from complete timing/metrology uncertainty.
"""
from pathlib import Path
import argparse
import json
import math
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neuromotion.session import write_json
from neuromotion.signals import load_signal_sessions

PAIRS = [(0, 2), (0, 3), (1, 2), (1, 3)]


def require_clean_capture(capture):
    hardware = (capture.get('clock') or {}).get('hardware')
    if (capture.get('state') != 'complete' or capture.get('error') or not hardware or
            any(v for k, v in hardware.items() if k != 'max_read_delay_us') or
            any(capture.get(k, 0) for k in ('frame_errors', 'invalid_records', 'observed_index_gaps'))):
        raise ValueError('Complete, error-free capture required before analog observation')


def measure_step(times_us, values_uv, event_us, level, *, full_scale_uv=2420000/6):
    """Return a crossing observation or an explicit rejection, without shifts."""
    t = np.asarray(times_us, dtype=float) - event_us
    v = np.asarray(values_uv, dtype=float)
    if len(t) != len(v) or len(t) < 30 or np.any(np.diff(t) <= 0):
        return dict(valid=False, reason='invalid_time_window')
    if not np.isfinite(t).all() or not np.isfinite(v).all():
        return dict(valid=False, reason='missing_or_nonfinite')
    dt = float(np.median(np.diff(t)))
    if dt <= 0 or np.max(np.diff(t)) > 1.5*dt:
        return dict(valid=False, reason='sample_gap')
    before = v[(t >= -160000) & (t <= -60000)]
    after = v[(t >= 60000) & (t <= 160000)]
    if min(len(before), len(after)) < 30:
        return dict(valid=False, reason='insufficient_plateau')
    a, b = float(np.median(before)), float(np.median(after))
    step = b-a
    if not 5000 <= abs(step) <= 20000:
        return dict(valid=False, reason='unexpected_step_amplitude', step_uv=step)
    if step*(1 if level else -1) <= 0:
        return dict(valid=False, reason='wrong_polarity', step_uv=step)
    if np.max(abs(v)) >= .98*full_scale_uv:
        return dict(valid=False, reason='saturated')
    s1 = 1.4826*float(np.median(abs(before-a)))
    s2 = 1.4826*float(np.median(abs(after-b)))
    noise = max(s1, s2, full_scale_uv/8388608/math.sqrt(12))
    if abs(step) < 20*noise:
        return dict(valid=False, reason='poor_plateau_snr', step_uv=step)
    mask = (t >= -35000) & (t <= 55000)
    x, y = t[mask], v[mask]
    z = (y-a)/step
    hits = np.flatnonzero((z[:-1] <= .5) & (z[1:] > .5))
    if len(hits) != 1:
        return dict(valid=False, reason='ambiguous_or_missing_crossing', crossings=int(len(hits)))
    k = int(hits[0])
    fraction = float((.5-z[k])/(z[k+1]-z[k]))
    latency = float(x[k]+fraction*(x[k+1]-x[k]))
    slope = abs(float((y[k+1]-y[k])/(x[k+1]-x[k])))
    # Approximate Gaussian/MAD noise propagation; adjacent FIR samples and
    # channels may be correlated. No claim of a complete uncertainty budget.
    threshold_var = .25*(math.pi/2)*(s1*s1/len(before)+s2*s2/len(after))
    sample_var = ((1-fraction)**2+fraction**2)*noise**2
    sigma_us = math.sqrt(sample_var+threshold_var)/slope
    return dict(valid=True, reason='valid', before_uv=a, after_uv=b, step_uv=step,
                plateau_noise_uv=noise, latency_us=latency,
                crossing_time_us=float(event_us+latency),
                bracket_start_us=float(event_us+x[k]), bracket_end_us=float(event_us+x[k+1]),
                fraction=fraction, interval_index=int(np.flatnonzero(mask)[k]),
                noise_sigma_us=sigma_us, noise_only_95_halfwidth_us=1.96*sigma_us)


def compare_crossings(measurements, limit_us=1000.):
    result = []
    for a, b in PAIRS:
        left, right = measurements[a], measurements[b]
        pair = dict(pair=f'CH{b+1}-CH{a+1}', valid=bool(left['valid'] and right['valid']))
        if pair['valid']:
            delay = right['latency_us']-left['latency_us']
            half = 1.96*math.hypot(left['noise_sigma_us'], right['noise_sigma_us'])
            pair.update(delay_us=delay, noise_only_95_low_us=delay-half,
                        noise_only_95_high_us=delay+half, noise_only_95_halfwidth_us=half,
                        observed_within_limit=abs(delay) <= limit_us,
                        noise_margin_within_limit=abs(delay)+half <= limit_us)
        result.append(pair)
    return result


def native_observations(path, edges, full_scales):
    """Stream two native traces, retaining only a chunk and edge support."""
    buffers = [np.empty((0, 5)), np.empty((0, 5))]
    cursor = 0
    columns = ['segment_id', 'device', 'native_index', 'drdy_time_us',
               'ch1_native_uv', 'ch2_native_uv', 'valid']
    with pd.read_csv(path, usecols=columns, chunksize=4096) as chunks:
        for chunk in chunks:
            if chunk.segment_id.nunique() != 1 or int(chunk.segment_id.iloc[0]) != edges[0]['segment_id']:
                raise ValueError('Native file must contain the same single MCU epoch')
            for d in (0, 1):
                c = chunk[chunk.device == d]
                block = c[['drdy_time_us', 'native_index', 'ch1_native_uv', 'ch2_native_uv', 'valid']].to_numpy(dtype=float)
                buffers[d] = np.concatenate([buffers[d], block])
            while cursor < len(edges) and all(len(b) and b[-1, 0] >= edges[cursor]['time_us']+180000 for b in buffers):
                edge = edges[cursor]
                measurements = []
                for d in (0, 1):
                    b = buffers[d]
                    window = b[(b[:, 0] >= edge['time_us']-180000) & (b[:, 0] <= edge['time_us']+180000)]
                    for local in (0, 1):
                        m = measure_step(window[:, 0], window[:, local+2], edge['time_us'], edge['level'],
                                         full_scale_uv=full_scales[2*d+local])
                        if not np.all(window[:, 4] == 1):
                            m = dict(valid=False, reason='invalid_native_record')
                        if m['valid']:
                            k, f = m['interval_index'], m['fraction']
                            # At nominal 1000 Hz, one native index is one nominal ms.
                            index = window[k, 1]+f*(window[k+1, 1]-window[k, 1])
                            m['uncorrected_nominal_index_ms'] = float(index)
                        measurements.append(m)
                yield measurements
                cursor += 1
            if cursor == len(edges):
                return
            for d in (0, 1):
                buffers[d] = buffers[d][buffers[d][:, 0] >= edges[cursor]['time_us']-180000]
    raise ValueError('Native file ends before all eligible edge windows')


def validate(acquisition, output, min_duration_s=0., limit_us=1000.):
    acquisition, output = Path(acquisition), Path(output)
    output.mkdir(parents=True, exist_ok=False)
    capture = json.loads((acquisition/'acquisition_report.json').read_text(encoding='utf-8'))
    metadata = json.loads((acquisition/'metadata.json').read_text(encoding='utf-8'))
    require_clean_capture(capture)
    sessions = load_signal_sessions(acquisition/'emg.csv')
    native = None
    try:
        if len(sessions) != 1 or sessions[0].channel_count != 4:
            raise ValueError('Use a single four-channel MCU epoch')
        session = sessions[0]
        anchor = session.metadata.get('ttl_start_us')
        if anchor is None or not session.metadata['sync_valid']:
            raise ValueError('Continuous hardware TTL ledger and start anchor required')
        times = session.mcu_time_us
        duration = float((times[-1]-times[0])/1e6)
        if duration < min_duration_s:
            raise ValueError('Recording shorter than requested acceptance duration')
        source = pd.read_csv(acquisition/'sync_edges.csv')
        if not np.isin(source.level, [0, 1]).all() or not (np.diff(source.edge_index) == 1).all():
            raise ValueError('Invalid hardware edge ledger')
        stable = source[source.mcu_time_us >= anchor+5000000]
        edges = [dict(index=int(e.edge_index), time_us=int(e.mcu_time_us), level=int(e.level),
                      segment_id=int(e.segment_id)) for e in stable.itertuples()
                 if e.mcu_time_us >= times[0]+180000 and e.mcu_time_us <= times[-1]-180000]
        if len(edges) < 10:
            raise ValueError('At least ten stable complete edges required')
        full_scales = [metadata['vref_uv']/gain for gain in metadata['gains']]
        reference_path = acquisition/'clock_reference.json'
        references = json.loads(reference_path.read_text(encoding='utf-8')) if reference_path.is_file() else {}
        fixed = next((r.get('fixed_reference') for r in references.get('segments', [])
                      if r['segment_id'] == edges[0]['segment_id']), None)
        native = native_observations(acquisition/'native_samples.csv', edges, full_scales)
        observations, pair_rows, channel_rows = [], [], []
        for edge in edges:
            start, end = np.searchsorted(times, [edge['time_us']-180000, edge['time_us']+180000], side='left')
            t = times[start:end]
            current = [measure_step(t, session.channel(c)[start:end], edge['time_us'], edge['level'],
                                    full_scale_uv=full_scales[c]) for c in range(4)]
            for c, m in enumerate(current):
                if np.any(session.channel_quality[start:end, c] != 0):
                    current[c] = dict(valid=False, reason='nonvalid_grid_quality')
            original = next(native)
            pairs = compare_crossings(current, limit_us)
            before = compare_crossings(original, limit_us)
            for c, m in enumerate(current):
                channel_rows.append(dict(edge_index=edge['index'], common_time_s=(edge['time_us']-anchor)/1e6,
                                         level=edge['level'], channel=c+1, **m))
            for (a, b), pair, native_pair in zip(PAIRS, pairs, before):
                row = dict(edge_index=edge['index'], common_time_s=(edge['time_us']-anchor)/1e6,
                           level=edge['level'], **pair)
                row['native_timestamp_pair_valid'] = native_pair['valid']
                if native_pair['valid']:
                    row['native_timestamp_delay_us'] = native_pair['delay_us']
                    row['uncorrected_nominal_index_delay_ms'] = (original[b]['uncorrected_nominal_index_ms']-
                                                               original[a]['uncorrected_nominal_index_ms'])
                    if fixed:
                        ta, tb = fixed['a'], fixed['b']
                        na, nb = original[a]['uncorrected_nominal_index_ms'], original[b]['uncorrected_nominal_index_ms']
                        row['fixed_reference_delay_us'] = (tb['reference_time_us']+(nb-tb['reference_index'])*tb['period_us']-
                                                           ta['reference_time_us']-(na-ta['reference_index'])*ta['period_us'])
                pair_rows.append(row)
            observations.append(dict(edge=edge, channels=current, native_channels=original, pairs=pairs))
        pd.DataFrame(channel_rows).to_csv(output/'channel_edges.csv', index=False)
        pd.DataFrame(pair_rows).to_csv(output/'pair_delays.csv', index=False)
        summaries = []
        for name in [f'CH{b+1}-CH{a+1}' for a, b in PAIRS]:
            rows = [r for r in pair_rows if r['pair'] == name]
            good = [r for r in rows if r['valid']]
            values = np.array([r['delay_us'] for r in good])
            original = [r['uncorrected_nominal_index_delay_ms'] for r in rows if r['native_timestamp_pair_valid']]
            fixed_values = [r['fixed_reference_delay_us'] for r in rows if 'fixed_reference_delay_us' in r]
            summaries.append(dict(pair=name, edges=len(rows), valid_edges=len(good),
                                  max_abs_us=float(np.max(abs(values))) if len(values) else None,
                                  rms_us=float(np.sqrt(np.mean(values**2))) if len(values) else None,
                                  first_delay_us=float(values[0]) if len(values) else None,
                                  last_delay_us=float(values[-1]) if len(values) else None,
                                  max_abs_plus_noise95_us=max((abs(r['delay_us'])+r['noise_only_95_halfwidth_us'] for r in good), default=None),
                                  nominal_index_first_ms=original[0] if original else None,
                                  nominal_index_last_ms=original[-1] if original else None,
                                  nominal_index_max_abs_ms=max(map(abs, original), default=None),
                                  fixed_reference_max_abs_us=max(map(abs, fixed_values), default=None),
                                  observed_failures=sum(not r['observed_within_limit'] for r in good),
                                  noise_margin_failures=sum(not r['noise_margin_within_limit'] for r in good)))
        all_valid = all(m['valid'] for o in observations for m in o['channels'])
        source_valid = all(m['valid'] for o in observations for m in o['native_channels'])
        observed_pass = all_valid and source_valid and all(s['observed_failures'] == 0 for s in summaries)
        margin_pass = observed_pass and all(s['noise_margin_failures'] == 0 for s in summaries)
        result = dict(status='passed_observed_edge_check' if margin_pass else 'failed_or_inconclusive',
                      evidence_kind='common_analog_1hz_bench', criterion_us=limit_us,
                      measurement_version='edge50-v1', capture_source=capture.get('source', {}),
                      real_serial_capture=capture.get('source', {}).get('kind') == 'serial',
                      duration_mcu_s=duration, required_duration_s=min_duration_s,
                      eligible_edges=len(edges), rising_edges=sum(e['level'] for e in edges),
                      falling_edges=sum(1-e['level'] for e in edges),
                      fixed_reference_available=bool(fixed),
                      boundary_or_startup_edges_excluded=len(source)-len(edges),
                      all_eligible_grid_edges_valid=all_valid, all_eligible_native_edges_valid=source_valid,
                      observed_metric_passed=observed_pass, noise_margin_metric_passed=margin_pass,
                      pairs=summaries, method='local plateaus; unique linear 50% crossing; all four cross-board pairs',
                      noise_interval_scope='Approximate noise-only 95% propagation, not complete metrology uncertainty; FIR samples/channels may be correlated.',
                      scope='Observed common-input edge alignment at 1 Hz; not calibrated microsecond hardware precision, full 20-200Hz analog response, or physiological timing.',
                      analysis_does_not_write_source_files=True, no_waveform_driven_clock_calibration=True,
                      timebase='nominal MCU microseconds; not calibrated SI seconds')
        plot_result(output, pair_rows, observations, session, anchor)
        write_json(output/'report.json', result)
        return result
    finally:
        if native is not None:
            native.close()
        for owner in {s.storage_owner for s in sessions}:
            if owner is not None:
                owner.close()


def plot_result(output, pairs, observations, session, anchor):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    frame = pd.DataFrame(pairs)
    fig, axes = plt.subplots(3, 1, figsize=(11, 10), constrained_layout=True)
    for name, block in frame.groupby('pair', sort=False):
        good = block[block.valid]
        if len(good):
            axes[0].plot(good.common_time_s, good.delay_us/1000, label=name, lw=.8)
        if len(good) and 'uncorrected_nominal_index_delay_ms' in good:
            axes[1].plot(good.common_time_s, good.uncorrected_nominal_index_delay_ms, label=name, lw=.8)
        if name == 'CH3-CH1' and len(good) and 'fixed_reference_delay_us' in good:
            axes[1].plot(good.common_time_s, good.fixed_reference_delay_us/1000,
                         label='20s fixed reference CH3-CH1', ls='--', color='black', lw=.9)
    axes[0].axhline(1, color='gray', ls='--');axes[0].axhline(-1, color='gray', ls='--')
    axes[0].set(ylabel='Aligned edge delay (ms)', title='Common analog source: observed 50% edge differences')
    axes[1].set(ylabel='Uncorrected / fixed-ref delay (ms)', xlabel='Common nominal MCU time (s)')
    edge = observations[len(observations)//2]['edge'];times=session.mcu_time_us
    first,last=np.searchsorted(times,[edge['time_us']-8000,edge['time_us']+12000])
    for c in range(4):
        axes[2].plot((times[first:last]-edge['time_us'])/1000,session.channel(c)[first:last]/1000,
                     marker='.',label=f'CH{c+1}',lw=.8)
    axes[2].axvline(0,color='gray',ls='--')
    axes[2].set(xlabel='Time relative to captured TTL edge (ms)',ylabel='Aligned input (mV)',
                title='Recorded grid samples around one edge; common latency not corrected')
    for ax in axes:
        ax.grid(alpha=.2)
        if ax.get_legend_handles_labels()[0]:
            ax.legend(loc='best',fontsize=8)
    fig.savefig(output/'analog_alignment.png',dpi=150);plt.close(fig)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--acquisition',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--min-duration',type=float,default=0.)
    args=parser.parse_args()
    if not math.isfinite(args.min_duration) or args.min_duration<0:
        parser.error('Duration must be finite and nonnegative')
    result=validate(args.acquisition,args.output,args.min_duration)
    print(json.dumps(result,indent=2))
    return 0 if result['status']=='passed_observed_edge_check' else 1


if __name__=='__main__':
    raise SystemExit(main())
