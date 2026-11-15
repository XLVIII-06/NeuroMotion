"""Barker-aware LED/ADS timeline alignment."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from collections import deque
from itertools import zip_longest

import numpy as np


START_BITS = (1, 1, 1, 0, 0, 1, 0)
END_BITS = (0, 0, 0, 1, 1, 0, 1)
SYMBOL_SECONDS = 0.5


class AlignmentError(ValueError):
    pass


@dataclass(frozen=True)
class Edge:
    position: float
    rising: bool


@dataclass(frozen=True)
class Marker:
    start: float
    kind: str


@dataclass(frozen=True)
class Alignment:
    session_number: int
    slope_samples_per_second: float
    intercept_samples: float
    video_start_s: float
    emg_start_sample_inferred: float
    fitted_start_sample: float
    video_end_s: float | None
    emg_end_sample: float | None
    matched_edges: int
    rms_residual_s: float
    max_residual_s: float
    frame_interval_s: float
    end_marker_verified: bool
    end_residual_s: float | None = None
    unmatched_tail_video_edges: int = 0
    unmatched_tail_emg_edges: int = 0
    residual_limit_frames: float = 1.0

    def sample_at_video_time(self, time_s: float | np.ndarray) -> float | np.ndarray:
        return self.slope_samples_per_second * time_s + self.intercept_samples

    def common_time_from_video(self, time_s: float | np.ndarray) -> float | np.ndarray:
        return time_s - self.video_start_s

    def common_time_from_sample(self, sample_index: float | np.ndarray) -> float | np.ndarray:
        return (sample_index - self.fitted_start_sample) / self.slope_samples_per_second

    def as_dict(self) -> dict:
        return asdict(self)


def iter_edges(states: np.ndarray, positions: np.ndarray):
    states = np.asarray(states)
    positions = np.asarray(positions, dtype=float)
    if len(states) != len(positions) or len(states) < 2:
        raise AlignmentError("TTL positions/states are empty or differ in length")
    for first in range(0, len(states), 4096):
        start = max(0, first - 1)
        stop = min(len(states), first + 4096)
        if not np.isfinite(positions[start:stop]).all() or not np.all(np.diff(positions[start:stop]) > 0):
            raise AlignmentError("TTL positions must be finite and strictly increasing")
        if not np.isin(states[start:stop], [0, 1]).all():
            raise AlignmentError("TTL states must contain only 0 and 1")
        changes = np.flatnonzero(states[start + 1:stop] != states[start:stop - 1]) + start + 1
        for index in changes:
            yield Edge(float(positions[index]), bool(states[index]))


def detect_edges(states: np.ndarray, positions: np.ndarray) -> list[Edge]:
    return list(iter_edges(states, positions))


def _edge_windows(states: np.ndarray, positions: np.ndarray):
    window = deque(maxlen=5)
    for edge in iter_edges(states, positions):
        window.append(edge)
        if len(window) >= 4:
            items = list(window)
            yield (items[0] if len(items) == 5 else None), *items[-4:]


def _states_match(states: np.ndarray, positions: np.ndarray, start: float, bits: tuple[int, ...], symbol: float) -> bool:
    centers = start + (np.arange(len(bits)) + 0.5) * symbol
    indices = np.searchsorted(positions, centers, side="right") - 1
    if np.any(indices < 0) or np.any(indices >= len(states)):
        return False
    # Missing portions of a recording must not be treated as a constant state.
    if np.any(centers - positions[indices] > symbol * 0.4):
        return False
    return bool(np.array_equal(states[indices].astype(int), np.asarray(bits)))


def _near(value: float, target: float, tolerance: float) -> bool:
    return abs(value - target) <= tolerance


def find_start_marker(states: np.ndarray, positions: np.ndarray, *, is_emg: bool, nominal_rate: float = 1000.0) -> Marker:
    """Find 1110010. EMG may begin high because firmware starts ADC afterward."""
    states = np.asarray(states, dtype=np.int8)
    positions = np.asarray(positions, dtype=float)
    if len(states) < 2:
        raise AlignmentError("TTL sequence is too short")
    symbol = SYMBOL_SECONDS * (nominal_rate if is_emg else 1.0)
    tolerance = symbol * 0.23
    candidates: list[float] = []
    for earlier, a, b, c, d in _edge_windows(states, positions):
        if [a.rising, b.rising, c.rising, d.rising] != [False, True, False, True]:
            continue
        if not (_near(b.position - a.position, 2 * symbol, tolerance)
                and _near(c.position - b.position, symbol, tolerance)
                and _near(d.position - c.position, symbol, tolerance)):
            continue
        start = a.position - 3 * symbol
        if not _states_match(states, positions, start, START_BITS, symbol):
            continue
        initial_rise = earlier is not None and earlier.rising and _near(earlier.position, start, tolerance)
        if is_emg:
            # A missing initial rise is valid only when recording begins at the
            # first high symbol, near firmware sample index zero.
            startup_high = states[0] == 1 and positions[0] <= symbol * 0.3 and abs(positions[0] - start) <= symbol * 0.3
            if not (initial_rise or startup_high):
                continue
        elif not initial_rise:
            continue
        candidates.append(start)
        if len(candidates) > 1:
            raise AlignmentError("Expected one start Barker marker, found multiple")
    if len(candidates) != 1:
        raise AlignmentError(f"Expected one start Barker marker, found {len(candidates)}")
    return Marker(float(candidates[0]), "start")


def find_end_marker(states: np.ndarray, positions: np.ndarray, *, nominal_rate: float = 1000.0, is_emg: bool = False, after: float = 0.0) -> Marker | None:
    states = np.asarray(states, dtype=np.int8)
    positions = np.asarray(positions, dtype=float)
    symbol = SYMBOL_SECONDS * (nominal_rate if is_emg else 1.0)
    tolerance = symbol * 0.23
    candidates: list[float] = []
    for _, a, b, c, d in _edge_windows(states, positions):
        if [a.rising, b.rising, c.rising, d.rising] != [True, False, True, False]:
            continue
        if not (_near(b.position - a.position, 2 * symbol, tolerance)
                and _near(c.position - b.position, symbol, tolerance)
                and _near(d.position - c.position, symbol, tolerance)):
            continue
        start = a.position - 3 * symbol
        if start <= after + 7 * symbol:
            continue
        if _states_match(states, positions, start, END_BITS, symbol):
            candidates.append(start)
    if len(candidates) > 1:
        raise AlignmentError("End Barker marker is ambiguous")
    return Marker(float(candidates[0]), "end") if candidates else None


def _stable_edges(edges, start: float, end: float | None, unit: float):
    lower = start + 7 * SYMBOL_SECONDS * unit - 0.12 * unit
    upper = end - 0.05 * unit if end is not None else float("inf")
    return (edge for edge in edges if lower <= edge.position < upper)


def iter_matched_edges(video_states, video_times_s, emg_states, emg_indices, alignment: Alignment, nominal_rate_hz: float = 1000.0):
    video = _stable_edges(iter_edges(video_states, video_times_s), alignment.video_start_s, alignment.video_end_s, 1.0)
    emg = _stable_edges(iter_edges(emg_states, emg_indices), alignment.emg_start_sample_inferred, alignment.emg_end_sample, nominal_rate_hz)
    for number, (left, right) in enumerate(zip(video, emg)):
        yield {"edge_number": number, "video_time_s": left.position, "emg_sample_index": right.position,
               "rising": left.rising, "common_time_s": left.position - alignment.video_start_s,
               "residual_s": (right.position - alignment.sample_at_video_time(left.position)) / alignment.slope_samples_per_second}


def fit_session_alignment(
    video_states: np.ndarray,
    video_times_s: np.ndarray,
    emg_states: np.ndarray,
    emg_indices: np.ndarray,
    *,
    session_number: int,
    nominal_rate_hz: float = 1000.0,
    min_edges: int = 10,
    max_residual_frames: float = 1.0,
) -> Alignment:
    video_times_s = np.asarray(video_times_s, dtype=float)
    emg_indices = np.asarray(emg_indices, dtype=float)
    if nominal_rate_hz <= 0 or not np.isfinite(max_residual_frames) or max_residual_frames <= 0:
        raise AlignmentError("Nominal EMG sample rate must be positive")
    if len(video_times_s) < 2:
        raise AlignmentError("Video timestamps must be strictly increasing")
    indices = np.linspace(0, len(video_times_s) - 2, min(20000, len(video_times_s) - 1), dtype=int)
    frame_interval = float(np.median(video_times_s[indices + 1] - video_times_s[indices]))
    video_start = find_start_marker(video_states, video_times_s, is_emg=False)
    emg_start = find_start_marker(emg_states, emg_indices, is_emg=True, nominal_rate=nominal_rate_hz)
    video_end = find_end_marker(video_states, video_times_s, after=video_start.start)
    emg_end = find_end_marker(emg_states, emg_indices, is_emg=True, nominal_rate=nominal_rate_hz, after=emg_start.start)
    left = int(np.searchsorted(video_times_s, video_start.start))
    right = int(np.searchsorted(video_times_s, video_end.start + 3.5)) if video_end else len(video_times_s)
    for start in range(left, right - 1, 4096):
        if np.any(np.diff(video_times_s[start:min(right, start + 4097)]) > 2.5 * frame_interval):
            raise AlignmentError("Video has a large frame timestamp gap during synchronization")
    if (video_end is None) != (emg_end is None):
        raise AlignmentError("End Barker marker appears in only one recording")
    tails = {}
    def pairs():
        tails.clear()
        tails.update(video_first=None, emg_first=None, video_count=0, emg_count=0)
        video = _stable_edges(iter_edges(video_states, video_times_s), video_start.start, video_end.start if video_end else None, 1.0)
        emg = _stable_edges(iter_edges(emg_states, emg_indices), emg_start.start, emg_end.start if emg_end else None, nominal_rate_hz)
        for first, second in zip_longest(video, emg):
            if first is None or second is None:
                if video_end is not None:
                    raise AlignmentError("Stable TTL edge count differs between video and EMG")
                key, edge = ("emg", second) if first is None else ("video", first)
                if tails[key + "_first"] is None:
                    tails[key + "_first"] = edge.position
                tails[key + "_count"] += 1
                continue
            if first.rising != second.rising:
                raise AlignmentError("Stable TTL edge polarity/order differs")
            yield first.position, second.position

    count, mean_x, mean_y, sxx, sxy = 0, 0.0, 0.0, 0.0, 0.0
    previous_x = previous_y = None
    for x, y in pairs():
        if previous_x is not None and (abs(x - previous_x - 0.5) > max(2 * frame_interval, 0.08) or abs((y - previous_y) / nominal_rate_hz - 0.5) > 0.08):
            raise AlignmentError("Stable TTL intervals differ from the expected 1 Hz square wave")
        previous_x, previous_y = x, y
        count += 1
        dx, dy = x - mean_x, y - mean_y
        mean_x += dx / count
        mean_y += dy / count
        sxx += dx * (x - mean_x)
        sxy += dx * (y - mean_y)
    if count < min_edges or sxx <= 0:
        raise AlignmentError(f"Need at least {min_edges} stable TTL edges; matched={count}")
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    if not nominal_rate_hz * 0.95 <= slope <= nominal_rate_hz * 1.05:
        raise AlignmentError(f"Fitted sample rate {slope:.3f} is implausible")
    if tails["video_first"] is not None and slope * tails["video_first"] + intercept <= emg_indices[-1] - frame_interval * slope:
        raise AlignmentError("Unmatched video TTL edges fall inside EMG coverage")
    if tails["emg_first"] is not None and (tails["emg_first"] - intercept) / slope <= video_times_s[-1] - frame_interval:
        raise AlignmentError("Unmatched EMG TTL edges fall inside video coverage")
    squared, max_s = 0.0, 0.0
    for x, y in pairs():
        residual = (y - (slope * x + intercept)) / slope
        squared += residual * residual
        max_s = max(max_s, abs(residual))
    rms_s = float(np.sqrt(squared / count))
    if rms_s > max_residual_frames * frame_interval or max_s > max_residual_frames * frame_interval:
        raise AlignmentError(
            f"TTL fit exceeds frame accuracy: RMS={rms_s:.4f}s, max={max_s:.4f}s, frame={frame_interval:.4f}s"
        )
    fitted_start = float(slope * video_start.start + intercept)
    if abs(fitted_start - emg_start.start) / slope > max_residual_frames * frame_interval:
        raise AlignmentError("Start marker and stable TTL fit disagree")
    end_residual_s = None
    if video_end is not None and emg_end is not None:
        end_residual_s = float((emg_end.start - (slope * video_end.start + intercept)) / slope)
        if abs(end_residual_s) > max_residual_frames * frame_interval:
            raise AlignmentError("End marker and stable TTL fit disagree")
    return Alignment(
        session_number=session_number,
        slope_samples_per_second=float(slope),
        intercept_samples=float(intercept),
        video_start_s=video_start.start,
        emg_start_sample_inferred=emg_start.start,
        fitted_start_sample=fitted_start,
        video_end_s=video_end.start if video_end else None,
        emg_end_sample=emg_end.start if emg_end else None,
        matched_edges=count,
        rms_residual_s=rms_s,
        max_residual_s=max_s,
        frame_interval_s=frame_interval,
        end_marker_verified=video_end is not None,
        end_residual_s=end_residual_s,
        unmatched_tail_video_edges=tails["video_count"],
        unmatched_tail_emg_edges=tails["emg_count"],
        residual_limit_frames=max_residual_frames,
    )
