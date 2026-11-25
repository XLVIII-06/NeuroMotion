"""Measured analog TTL crossings and explicit BL-to-master clock maps."""
from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

from .bl422 import BLRecording


class StimulusSyncError(ValueError):
    pass


@dataclass
class TTLTrace:
    times: np.ndarray
    levels: np.ndarray
    initial_level: int = 0
    inferred_start: bool = False


def analog_ttl(record: BLRecording) -> tuple[TTLTrace, dict]:
    data = record.samples
    pool = np.array(data['ttl_mv'][::max(1, len(data)//20000)], copy=True)
    low, high = np.quantile(pool, [.1, .9])
    for _ in range(6):
        boundary = (low+high)/2
        a, b = pool[pool < boundary], pool[pool >= boundary]
        if not len(a) or not len(b):
            raise StimulusSyncError('TTL has no two separated voltage plateaus')
        low, high = float(np.median(a)), float(np.median(b))
    noise = max(float(np.median(abs(a-low))), float(np.median(abs(b-high))))*1.4826
    if high-low < max(1., 10*noise):
        raise StimulusSyncError('TTL voltage separation is insufficient')
    lower, upper = low+.35*(high-low), low+.65*(high-low)
    previous = int(data['ttl_mv'][0] >= boundary)
    initial = previous
    candidates = []
    for first in range(0, len(data), 4096):
        v = data['ttl_mv'][first:first+4096]
        state = np.where(v >= upper, 1, np.where(v <= lower, 0, -1))
        known = np.flatnonzero(state >= 0)
        filled = np.full(len(state), previous, dtype=np.int8)
        if len(known):
            nearest = np.maximum.accumulate(np.where(state >= 0, np.arange(len(state)), -1))
            mask = nearest >= 0
            filled[mask] = state[nearest[mask]]
        changes = np.flatnonzero(np.diff(np.r_[previous, filled]))
        candidates.extend((first+int(i), int(filled[i])) for i in changes)
        previous = int(filled[-1])
    fs = record.metadata['sample_rate_hz']
    times, levels = [], []
    observations = []
    for index, level in candidates:
        before = data['ttl_mv'][max(0, index-round(.12*fs)):max(0, index-round(.04*fs))]
        after = data['ttl_mv'][index+round(.04*fs):min(len(data), index+round(.12*fs))]
        if min(len(before), len(after)) < 3:
            raise StimulusSyncError('TTL crossing has insufficient local plateaus')
        a, b = float(np.median(before)), float(np.median(after))
        if (b-a)*(1 if level else -1) < .5*(high-low):
            raise StimulusSyncError('TTL has an unexpected short pulse or wrong local polarity')
        first, last = max(0, index-max(5, round(.02*fs))), min(len(data), index+max(5, round(.02*fs)))
        z = (data['ttl_mv'][first:last]-a)/(b-a)
        hits = np.flatnonzero((z[:-1] <= .5) & (z[1:] > .5))
        if len(hits) != 1:
            raise StimulusSyncError('TTL crossing is ambiguous; inspect raw voltage before synchronizing')
        k = first+int(hits[0])
        fraction = float((.5-z[hits[0]])/(z[hits[0]+1]-z[hits[0]]))
        t = (float(data['time_us'][k])+fraction*float(data['time_us'][k+1]-data['time_us'][k]))/1e6
        times.append(t); levels.append(level)
        observations.append({'time_s': t, 'level': level, 'source_sequence': int(data['sequence'][k]),
                             'before_mv': a, 'after_mv': b})
    if len(times) < 14 or np.any(np.diff(times) < .005):
        raise StimulusSyncError('Too few TTL edges or unexplained narrow pulses')
    return TTLTrace(np.array(times), np.array(levels, dtype=np.int8), initial), {
        'low_mv': low, 'high_mv': high, 'hysteresis_mv': [lower, upper],
        'crossing_method': 'local_plateau_50_percent_linear_interpolation',
        'edge_count': len(times), 'edges': observations}


def protocol(trace: TTLTrace, *, allow_inversion=True):
    """Recognize edge-duration ratios; the zero is a measured rise, never 1.5s subtraction."""
    times = trace.times
    candidates = []
    for inverted in ((False, True) if allow_inversion else (False,)):
        levels = 1-trace.levels if inverted else trace.levels
        for i in range(len(times)-4):
            if not np.array_equal(levels[i:i+5], [1, 0, 1, 0, 1]):
                continue
            periods = np.diff(times[i:i+5])/np.array([3, 2, 1, 1])
            symbol = float(np.median(periods))
            if not .35 <= symbol <= .65 or np.max(abs(periods-symbol)) > .15*symbol:
                continue
            start = float(times[i])
            ends = []
            for j in range(i+5, len(times)-3):
                if not np.array_equal(levels[j:j+4], [1, 0, 1, 0]):
                    continue
                p = np.diff(times[j:j+4])/np.array([2, 1, 1])
                if np.max(abs(p-symbol)) <= .15*symbol:
                    ends.append(float(times[j]-3*symbol))
            if len(ends) > 1:
                raise StimulusSyncError('Multiple end markers; select a single recording session')
            end = ends[0] if ends else None
            mask = (times >= start+6.9*symbol) & (times < (end-.1*symbol if end else math.inf))
            indices = np.flatnonzero(mask)
            if len(indices) < 10:
                continue
            stable = times[indices]
            if np.any(abs(np.diff(stable)/symbol-1) > .18):
                continue
            candidates.append({'start_s': start, 'end_s': end, 'symbol_s': symbol,
                               'inverted': inverted, 'levels': levels, 'stable_indices': indices,
                               'start_index': i, 'start_inferred': trace.inferred_start})
    if len(candidates) != 1:
        raise StimulusSyncError(f'Expected one complete start marker and continuous stable TTL sequence; found {len(candidates)}')
    return candidates[0]


@dataclass
class ClockMap:
    source: np.ndarray
    master: np.ndarray
    mode: str
    slope: float
    intercept: float
    report: dict

    def map(self, times):
        values = np.asarray(times, dtype=float)
        valid = np.isfinite(values) & (values >= self.source[0]) & (values <= self.source[-1])
        result = self.slope*values+self.intercept if self.mode == 'affine' else np.interp(values, self.source, self.master)
        return np.where(valid, result, np.nan)


def fit_bl_clock(source: TTLTrace, master: TTLTrace, *, rms_limit_s=.001, max_limit_s=.002) -> ClockMap:
    if not np.isfinite([rms_limit_s, max_limit_s]).all() or min(rms_limit_s, max_limit_s) <= 0:
        raise ValueError('Clock residual limits must be positive and finite')
    left, right = protocol(source), protocol(master, allow_inversion=False)
    a, b = left['stable_indices'], right['stable_indices']
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    if not np.array_equal(left['levels'][a], right['levels'][b]):
        raise StimulusSyncError('Matched TTL polarities disagree')
    x, y = source.times[a], master.times[b]
    center = float(np.mean(x)); xc = x-center
    slope, centered_intercept = np.linalg.lstsq(np.c_[xc, np.ones(n)], y, rcond=None)[0]
    intercept = float(centered_intercept-slope*center)
    residual = y-(slope*x+intercept)
    leverage = 1/n+xc*xc/float(xc@xc)
    affine_loo = residual/(1-leverage)
    piece_loo = y[1:-1]-(y[:-2]+(x[1:-1]-x[:-2])/(x[2:]-x[:-2])*(y[2:]-y[:-2]))
    def metrics(errors):
        return {'rms_s': float(np.sqrt(np.mean(errors**2))), 'max_s': float(np.max(abs(errors))), 'count': len(errors)}
    am, pm = metrics(affine_loo), metrics(piece_loo)
    mode = 'affine' if am['rms_s'] <= rms_limit_s and am['max_s'] <= max_limit_s else 'piecewise'
    used = am if mode == 'affine' else pm
    if slope <= 0 or used['rms_s'] > rms_limit_s or used['max_s'] > max_limit_s:
        raise StimulusSyncError(f'TTL independent validation exceeds limits: affine={am}, piecewise={pm}')
    # Include observed coded edges for onset coverage, with matching polarity.
    si, mi = left['start_index'], right['start_index']
    sx = np.r_[source.times[si:si+4], x]
    my = np.r_[master.times[mi:mi+4], y]
    if np.any(np.diff(sx) <= 0) or np.any(np.diff(my) <= 0):
        raise StimulusSyncError('Clock map is not monotonic')
    local_slope = np.diff(my)/np.diff(sx)
    if np.any(abs(local_slope/float(np.median(local_slope))-1) > .08):
        raise StimulusSyncError('Unexplained local clock jump')
    onset_error = master.times[mi]-(master.times[mi+4]+(source.times[si]-source.times[si+4])*np.median(np.diff(y[:5])/np.diff(x[:5])))
    if abs(onset_error) > max_limit_s*2:
        raise StimulusSyncError('Measured start edge disagrees with local stable clock')
    end_error = None
    if left['end_s'] is not None and right['end_s'] is not None and len(a) == len(left['stable_indices']) and len(b) == len(right['stable_indices']):
        end_error = right['end_s']-(y[-1]+(left['end_s']-x[-1])*local_slope[-1])
        if abs(end_error) > max_limit_s*2:
            raise StimulusSyncError('End markers disagree with mapped time')
    report = {'status': 'clock_validated', 'mode': mode, 'matched_stable_edges': n,
              'source_start_s': left['start_s'], 'master_start_s': right['start_s'],
              'source_symbol_s': left['symbol_s'], 'master_symbol_s': right['symbol_s'],
              'source_inverted': left['inverted'], 'master_start_inferred': right['start_inferred'],
              'affine_slope': float(slope), 'affine_intercept_s': intercept,
              'relative_scale_ppm': float((slope-1)*1e6),
              'affine_leave_one_out': am, 'piecewise_leave_one_out': pm,
              'validation': used, 'rms_limit_s': rms_limit_s, 'max_limit_s': max_limit_s,
              'measured_onset_check_s': float(onset_error), 'end_check_s': end_error,
              'unmatched_source_tail_edges': len(left['stable_indices'])-n,
              'unmatched_master_tail_edges': len(right['stable_indices'])-n,
              'edge_validation': [{'source_time_s': float(xx), 'master_time_s': float(yy),
                  'affine_residual_s': float(rr), 'affine_leave_one_out_s': float(aa),
                  'piecewise_leave_one_out_s': float(piece_loo[i-1]) if 0 < i < n-1 else None}
                  for i, (xx, yy, rr, aa) in enumerate(zip(x, y, residual, affine_loo))],
              'absolute_onset_validated': False,
              'timing_scope': 'recorded TTL clocks; stimulus annotation/output and input-filter delay not calibrated'}
    return ClockMap(sx, my, mode, float(slope), intercept, report)


def master_ttl(session, alignment) -> TTLTrace:
    from .timeline import iter_edges
    if session.channel_count == 4:
        import pandas as pd
        edges = pd.read_csv(session.metadata['sync_edges_path'])
        edges = edges[edges.segment_id == session.metadata['segment_id']]
        if not session.metadata['sync_valid'] or np.any(np.diff(edges.edge_index) != 1):
            raise StimulusSyncError('MCU TTL ledger is invalid')
        return TTLTrace((edges.mcu_time_us.to_numpy(dtype=float)-session.metadata['ttl_start_us'])/1e6,
                        edges.level.to_numpy(dtype=np.int8))
    edges = list(iter_edges(session.sync, session.sample_index))
    positions = np.array([e.position for e in edges])
    levels = np.array([int(e.rising) for e in edges], dtype=np.int8)
    inferred = False
    if session.sync[0] and session.sample_index[0] <= 150:
        positions = np.r_[alignment.emg_start_sample_inferred, positions]
        levels = np.r_[1, levels]
        inferred = True
    return TTLTrace(np.asarray(alignment.common_time_from_sample(positions)), levels, int(session.sync[0]), inferred)
