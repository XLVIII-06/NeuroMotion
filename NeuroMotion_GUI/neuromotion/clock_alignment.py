"""Continuous DRDY measurement and bounded-latency four-channel reconstruction.

The interface accepts native samples/TTL edges and returns immutable grid blocks.
No waveform correlation drives the clock model. Model changes preserve phase;
native holes invalidate the complete FIR support rather than being bridged.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math

import numpy as np

from .protocol_v2 import NativeSample, SyncEdge, SPI_ERROR, STATUS_ERROR, READ_LATE, CAPTURE_ERROR

TAPS = 65
HALF = TAPS // 2
PHASES = 1024
offsets = np.arange(-HALF, HALF + 1)
_kernels = .9 * np.sinc(.9 * (offsets[None, :] - np.arange(PHASES + 1)[:, None] / PHASES)) * np.kaiser(TAPS, 8.6)
_kernels /= _kernels.sum(axis=1, keepdims=True)


@dataclass(frozen=True)
class GridBlock:
    time_us: np.ndarray
    values: np.ndarray
    quality: np.ndarray
    revisions: np.ndarray
    sync: np.ndarray
    sync_valid: np.ndarray


@dataclass(frozen=True)
class ClockPiece:
    time_us: float
    index: float
    period_us: float
    revision: int


class DeviceClock:
    def __init__(self):
        self.points = deque(maxlen=21000)
        self.pieces = deque(maxlen=90)
        self.reference = None
        self.last_fit = None
        self.next_update = None
        self.revision = 0
        self.resets = self.duplicates = self.gaps = 0
        self.last = None

    def will_reset(self, sample):
        if self.last is None or sample.index == self.last.index:
            return False
        step = sample.index - self.last.index
        interval = (sample.time_us - self.last.time_us) / step if step > 0 else 0
        return sample.index < self.last.index or sample.time_us <= self.last.time_us or not 900 < interval < 1100

    def observe(self, sample: NativeSample):
        reset = False
        if self.last is not None:
            if sample.index == self.last.index:
                self.duplicates += 1
                return [], False, False
            if self.will_reset(sample):
                self.points.clear()
                self.pieces.clear()
                self.reference = self.last_fit = self.next_update = None
                self.resets += 1
                reset = True
            else:
                self.gaps += max(0, sample.index - self.last.index - 1)
        self.last = sample
        if not sample.valid:
            return [], reset, True
        self.points.append((sample.index, sample.time_us))
        if self.next_update is None:
            self.next_update = sample.time_us + 250000
        changes = []
        if sample.time_us >= self.next_update and len(self.points) >= 100:
            fit = self._fit(sample.time_us)
            if fit is not None:
                self.revision += 1
                boundary = float(sample.time_us)
                if self.pieces:
                    previous = self.pieces[-1]
                    anchor = previous.index + (boundary - previous.time_us) / previous.period_us
                    error = fit["reference_time_us"] + (anchor - fit["reference_index"]) * fit["period_us"] - boundary
                    # Repay phase error over the next second, without a time jump.
                    period = fit["period_us"] * (1 + error / 1000000)
                else:
                    anchor = fit["reference_index"]
                    boundary = fit["reference_time_us"]
                    period = fit["period_us"]
                if 900 < period < 1100:
                    self.pieces.append(ClockPiece(boundary, anchor, period, self.revision))
                    self.last_fit = fit
                    changes.append({**fit, "revision": self.revision, "effective_time_us": boundary,
                                    "effective_index": anchor, "effective_period_us": period})
                    if self.reference is None and sample.time_us - self.points[0][1] >= 19999000:
                        self.reference = dict(fit)
            self.next_update = sample.time_us + 1000000
        return changes, reset, True

    def _fit(self, now):
        a = np.asarray(self.points, dtype=float)
        a = a[a[:, 1] >= now - 20000000]
        n0, t0 = a[-1]
        x, y = a[:, 0] - n0, a[:, 1] - t0
        keep = np.ones(len(a), dtype=bool)
        for _ in range(3):
            xx, yy = x[keep], y[keep]
            xm, ym = xx.mean(), yy.mean()
            denominator = np.sum((xx - xm) ** 2)
            if denominator == 0:
                return None
            period = np.sum((xx - xm) * (yy - ym)) / denominator
            intercept = ym - period * xm
            residual = y - (period * x + intercept)
            sigma = 1.4826 * np.median(np.abs(residual - np.median(residual)))
            keep = np.abs(residual) <= max(3.5 * sigma, 3.)
            if keep.sum() < 100:
                return None
        if not 900 < period < 1100:
            return None
        return {"reference_index": float(n0), "reference_time_us": float(t0 + intercept),
                "period_us": float(period), "rate_hz": float(1e6 / period),
                "window_start_us": float(a[0,1]), "window_end_us": float(a[-1,1]),
                "points": int(keep.sum()), "coverage_s": float((a[-1, 1] - a[0, 1]) / 1e6),
                "rms_us": float(np.sqrt(np.mean(residual[keep] ** 2))),
                "max_us": float(np.max(np.abs(residual[keep])))}

    def positions(self, times):
        return self.piece_positions(list(self.pieces), times)

    @staticmethod
    def piece_positions(pieces, times):
        if not pieces:
            return np.full(len(times), np.nan), np.zeros(len(times), dtype=int)
        starts = np.array([p.time_us for p in pieces])
        ids = np.clip(np.searchsorted(starts, times, side="right") - 1, 0, len(pieces) - 1)
        ns = np.array([p.index for p in pieces])[ids]
        ts = starts[ids]
        bs = np.array([p.period_us for p in pieces])[ids]
        return ns + (times - ts) / bs, np.array([p.revision for p in pieces])[ids]


class NativeBuffer:
    def __init__(self, capacity=4096):
        self.capacity = capacity
        self.n = np.zeros(capacity, dtype=np.int64)
        self.t = np.zeros(capacity, dtype=np.int64)
        self.v = np.zeros((capacity, 2))
        self.good = np.zeros(capacity, dtype=bool)
        self.flags = np.zeros(capacity, dtype=np.uint8)
        self.count = self.head = 0
        self.first_time = None

    def append(self, sample, values):
        if self.first_time is None:
            self.first_time = sample.time_us
        pos = self.head % self.capacity
        self.n[pos], self.t[pos], self.v[pos], self.good[pos] = sample.index, sample.time_us, values, sample.valid
        self.flags[pos] = sample.flags
        self.head += 1
        self.count = min(self.count + 1, self.capacity)

    def resample(self, positions):
        result = np.full((len(positions), 2), np.nan)
        quality = np.full(len(positions), "support_gap", dtype="U20")
        if not self.count:
            return result, quality
        order = (np.arange(self.count) + self.head - self.count) % self.capacity
        ns, vals, good = self.n[order], self.v[order], self.good[order]
        finite = np.isfinite(positions)
        integer = np.floor(np.nan_to_num(positions, nan=-100000)).astype(np.int64)
        centers = np.searchsorted(ns, integer)
        idx = centers[:, None] + offsets
        safe = (idx[:, 0] >= 0) & (idx[:, -1] < len(ns)) & finite
        clip = np.clip(idx, 0, len(ns) - 1)
        contiguous = np.all(ns[clip] == integer[:, None] + offsets, axis=1)
        quality[safe & ~contiguous] = "native_gap"
        quality[safe & contiguous & ~good[clip].all(axis=1)] = "invalid_native"
        for bit, reason in ((STATUS_ERROR,"status_error"),(SPI_ERROR,"spi_error"),(READ_LATE,"read_late"),(CAPTURE_ERROR,"capture_error")):
            quality[safe & contiguous & ((self.flags[order][clip] & bit) != 0).any(axis=1)] = reason
        safe &= contiguous & good[clip].all(axis=1)
        if safe.any():
            phase = np.clip(np.rint((positions[safe] - integer[safe]) * PHASES).astype(int), 0, PHASES)
            result[safe] = np.einsum("ij,ijk->ik", _kernels[phase], vals[clip[safe]])
            quality[safe] = "valid"
        quality[~finite] = "warming"
        return result, quality


class ClockAlignedStream:
    """Single interface for native events -> already aligned four-channel blocks."""
    def __init__(self, vref_uv=2420000, gains=(6, 6, 6, 6), delay_us=500000):
        if delay_us < 100000 or delay_us > 2000000:
            raise ValueError("Alignment delay must be 0.1–2 seconds")
        self.vref_uv, self.gains, self.delay_us = vref_uv, tuple(gains), delay_us
        self.scales = np.asarray(gains, dtype=float).reshape(2, 2)
        self.scales = vref_uv / (8388608 * self.scales)
        self.clocks = [DeviceClock(), DeviceClock()]
        self.buffers = [NativeBuffer(), NativeBuffer()]
        self.retired = [deque(maxlen=8), deque(maxlen=8)]
        self.edges = deque(maxlen=256)
        self.next_time = self.last_time = None
        self.ttl_start_us = None
        self.model_events = []
        self.native_count = self.output_count = self.late_samples = 0
        self._next_emit = 0
        self._last_edge = None
        self.ttl_fault = False
        self.fixed_reference = None
        self.reference_start_us = None

    def feed(self, events):
        blocks = []
        for event in events:
            if isinstance(event, SyncEdge):
                if self._last_edge is not None and (event.index != self._last_edge.index + 1 or event.time_us <= self._last_edge.time_us):
                    self.ttl_fault = True
                self._last_edge = event
                self.edges.append(event)
                if event.index == 0 and event.level == 1:
                    self.ttl_start_us = event.time_us
                continue
            clock = self.clocks[event.device]
            old_pieces = list(clock.pieces) if clock.will_reset(event) else None
            changes, reset, accepted = clock.observe(event)
            if not accepted:
                continue
            if reset:
                self.fixed_reference = None
                self.reference_start_us = event.time_us
                old = self.buffers[event.device]
                if old.first_time is not None:
                    self.retired[event.device].append((old.first_time, event.time_us, old, old_pieces))
                self.buffers[event.device] = NativeBuffer()
            self.model_events.extend({"device": event.device, **change} for change in changes)
            self.buffers[event.device].append(event, np.array([event.code1, event.code2]) * self.scales[event.device])
            self.native_count += 1
            if self.reference_start_us is None:
                self.reference_start_us = event.time_us
            if self.next_time is None:
                self.next_time = int(math.ceil(event.time_us / 1000)) * 1000
            if event.time_us < self.next_time - 100000:
                self.late_samples += 1
            self.last_time = max(self.last_time or event.time_us, event.time_us)
            if changes and self.fixed_reference is None:
                self._lock_reference()
            if self.last_time >= self._next_emit:
                blocks.extend(self._drain(self.last_time - self.delay_us))
                self._next_emit = self.last_time + 20000
        return blocks

    def _lock_reference(self):
        fits=[c.last_fit for c in self.clocks]
        if not all(fits) or any(f['coverage_s']<19.9 or f['window_start_us']<self.reference_start_us-1000 for f in fits):
            return
        if abs(fits[0]['window_end_us']-fits[1]['window_end_us'])>1000000:
            return
        a,b=fits
        n=max(a['reference_index'],b['reference_index'])
        def at(f,index):return f['reference_time_us']+(index-f['reference_index'])*f['period_us']
        self.fixed_reference={'a':dict(a),'b':dict(b),'native_reference_index':n,
                              'offset_us_at_reference':at(b,n)-at(a,n),
                              'extrapolated_offset_n0_us':at(b,0)-at(a,0),
                              'relative_ppm':(b['rate_hz']/a['rate_hz']-1)*1e6,
                              'offset_definition':'T_B(n)-T_A(n); positive means B is later at that native index'}

    def _drain(self, end):
        if self.next_time is None or end < self.next_time:
            return []
        blocks = []
        while self.next_time <= end:
            times = np.arange(self.next_time, min(end + 1, self.next_time + 250000), 1000, dtype=np.int64)
            values = np.full((len(times), 4), np.nan)
            quality = np.full((len(times), 4), "support_gap", dtype="U20")
            revisions = np.zeros((len(times), 2), dtype=np.int64)
            for device in (0, 1):
                pos, rev = self.clocks[device].positions(times)
                v, q = self.buffers[device].resample(pos)
                last = self.clocks[device].last
                if last is not None:
                    q[(times > last.time_us + 100000) & ~np.isfinite(v[:,0])] = "source_stalled"
                for begin, end_time, previous_buffer, previous_pieces in self.retired[device]:
                    mask = (times >= begin) & (times < end_time)
                    if mask.any():
                        old_pos, old_rev = DeviceClock.piece_positions(previous_pieces, times[mask])
                        v[mask], q[mask] = previous_buffer.resample(old_pos)
                        rev[mask] = old_rev
                values[:, device * 2:device * 2 + 2] = v
                quality[:, device * 2:device * 2 + 2] = q[:, None]
                revisions[:, device] = rev
            sync = np.zeros(len(times), dtype=np.int8)
            valid = np.zeros(len(times), dtype=bool)
            if self.edges:
                edges = list(self.edges)
                ids = np.searchsorted([e.time_us for e in edges], times, side="right") - 1
                valid = ids >= 0
                sync = np.array([e.level for e in edges], dtype=np.int8)[np.maximum(ids, 0)]
                if self.ttl_fault:
                    valid[:] = False
            blocks.append(GridBlock(times, values, quality, revisions, sync, valid))
            self.next_time = int(times[-1]) + 1000
            self.output_count += len(times)
            for previous in self.retired:
                while previous and previous[0][1] <= self.next_time:
                    previous.popleft()
        return blocks

    def finish(self):
        return self._drain(self.last_time) if self.last_time is not None else []

    def pop_model_events(self):
        events, self.model_events = self.model_events, []
        return events

    def status(self):
        fits = [c.last_fit for c in self.clocks]
        ppm = None if not all(fits) else (fits[1]["rate_hz"] / fits[0]["rate_hz"] - 1) * 1e6
        return {"native_samples": self.native_count, "aligned_samples": self.output_count, "late_samples": self.late_samples,
                "relative_ppm": ppm, "delay_s": self.delay_us / 1e6, "ttl_fault": self.ttl_fault,
                "buffer_lag_s": max(0., ((self.last_time or 0) - (self.next_time or 0) + 1000) / 1e6),
                "fixed_reference": self.fixed_reference, "ttl_start_us": self.ttl_start_us,
                "devices": [{"state": "waiting" if c.last is None else "degraded" if (self.last_time or 0) - c.last.time_us > 1000000 else "warming" if not c.reference else "tracking",
                             "fit": c.last_fit, "reference": c.reference, "gaps": c.gaps, "duplicates": c.duplicates,
                             "resets": c.resets} for c in self.clocks]}
