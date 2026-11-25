"""Load WinForms and Python recorder CSVs without losing session boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd


class SignalInputError(ValueError):
    pass


@dataclass(frozen=True)
class SignalSession:
    session_number: int
    source_row_start: int
    source_row_end: int
    sample_index: np.ndarray
    ch1_uv: np.ndarray
    ch2_uv: np.ndarray
    sync: np.ndarray
    dropped_samples: np.ndarray
    pc_missing_samples: np.ndarray
    signal_kind: str
    metadata: dict = field(default_factory=dict)
    boundary_reason: str = "file_start"
    ch1_filtered_uv: np.ndarray | None = None
    ch2_filtered_uv: np.ndarray | None = None
    filter_revision: np.ndarray | None = None
    aligned_channels_uv: np.ndarray | None = None
    filtered_channels_uv: np.ndarray | None = None
    channel_quality: np.ndarray | None = None
    filter_quality: np.ndarray | None = None
    mcu_time_us: np.ndarray | None = None
    common_time_s: np.ndarray | None = None
    sync_positions: np.ndarray | None = None
    storage_owner: object = field(default=None, repr=False, compare=False)

    @property
    def channel_count(self):
        return 4 if self.aligned_channels_uv is not None else 2

    def channel(self, index, filtered=False):
        matrix = self.filtered_channels_uv if filtered else self.aligned_channels_uv
        if matrix is not None:
            return matrix[:, index]
        return (self.ch1_filtered_uv, self.ch2_filtered_uv)[index] if filtered else (self.ch1_uv, self.ch2_uv)[index]

    @property
    def raw_available(self) -> bool:
        return self.signal_kind == "raw"

    @property
    def missing_index_samples(self) -> int:
        differences = np.diff(self.sample_index)
        return int(np.maximum(differences - 1, 0).sum())


def load_signal_sessions(path: str | Path) -> list[SignalSession]:
    path = Path(path)
    header = pd.read_csv(path, encoding="utf-8-sig", nrows=0)
    if "ch4_aligned_uv" in header:
        from .signals_v2 import load_four_sessions
        return load_four_sessions(path)
    frame = pd.read_csv(path, encoding="utf-8-sig")
    if frame.empty:
        raise SignalInputError("EMG CSV is empty")
    index_key = next((name for name in ("sample_index", "sampleIndex") if name in frame), None)
    if index_key is None or "sync" not in frame:
        raise SignalInputError("EMG CSV needs sample_index/sampleIndex and sync")
    if {"ch1_raw_uv", "ch2_raw_uv"}.issubset(frame.columns):
        keys = ("ch1_raw_uv", "ch2_raw_uv")
        signal_kind = "raw"
    elif {"ch1_uv", "ch2_uv"}.issubset(frame.columns):
        keys = ("ch1_uv", "ch2_uv")
        signal_kind = "filtered_or_unspecified"
    elif {"ch1", "ch2"}.issubset(frame.columns):
        keys = ("ch1", "ch2")
        signal_kind = "winforms_filtered_or_unspecified"
    else:
        raise SignalInputError("EMG CSV needs ch1/ch2 or ch1_uv/ch2_uv")
    required = [index_key, *keys, "sync"]
    try:
        numerical = frame[required].apply(pd.to_numeric, errors="raise")
    except (ValueError, TypeError) as exc:
        raise SignalInputError(f"Invalid numerical value in EMG CSV: {exc}") from exc
    if numerical.isna().any().any():
        raise SignalInputError("EMG CSV contains missing required values")
    index_values = numerical[index_key].to_numpy(dtype=float)
    if not np.all(np.isfinite(index_values) & (index_values >= 0) & (index_values <= 2**32 - 1) & (index_values == np.floor(index_values))):
        raise SignalInputError("EMG sample indices must be unsigned 32-bit integers")
    indices = index_values.astype(np.int64)
    sync_values = numerical["sync"].to_numpy(dtype=float)
    if not np.isin(sync_values, [0, 1]).all():
        raise SignalInputError("EMG sync must contain only 0 or 1")
    sync = sync_values.astype(np.int8)
    for key in keys:
        if not np.isfinite(numerical[key].to_numpy(dtype=float)).all():
            raise SignalInputError(f"EMG {key} contains non-finite values")
    segment_changes = np.zeros(max(0, len(indices) - 1), dtype=bool)
    if "segment_id" in frame:
        segments = pd.to_numeric(frame["segment_id"], errors="coerce").to_numpy(dtype=float)
        if not np.all(np.isfinite(segments) & (segments >= 0) & (segments == np.floor(segments))):
            raise SignalInputError("segment_id must contain nonnegative integers")
        segment_changes = segments[1:] != segments[:-1]
    if np.any((np.diff(indices) == 0) & ~segment_changes):
        raise SignalInputError("EMG CSV contains duplicate sample indices within a session")
    boundaries = [0, *(np.flatnonzero((np.diff(indices) < 0) | segment_changes) + 1).tolist(), len(frame)]
    def optional(name: str) -> np.ndarray:
        if name not in frame:
            return np.zeros(len(frame), dtype=np.int64)
        values = pd.to_numeric(frame[name], errors="coerce").to_numpy(dtype=float)
        if not np.all(np.isfinite(values) & (values >= 0) & (values <= 2**32 - 1) & (values == np.floor(values))):
            raise SignalInputError(f"EMG {name} must contain nonnegative integer counters")
        return values.astype(np.int64)
    dropped = optional("dropped_samples")
    pc_missing = optional("pc_missing_samples")
    ch1 = numerical[keys[0]].to_numpy(dtype=float)
    ch2 = numerical[keys[1]].to_numpy(dtype=float)
    filtered_keys = ("ch1_uv", "ch2_uv") if {"ch1_uv", "ch2_uv"}.issubset(frame.columns) else ("ch1", "ch2")
    filtered1 = filtered2 = None
    if all(key in frame for key in filtered_keys):
        filtered1 = pd.to_numeric(frame[filtered_keys[0]], errors="raise").to_numpy(dtype=float)
        filtered2 = pd.to_numeric(frame[filtered_keys[1]], errors="raise").to_numpy(dtype=float)
        if not np.isfinite(filtered1).all() or not np.isfinite(filtered2).all():
            raise SignalInputError("Filtered signal values must be finite")
    revisions = optional("filter_revision") if "filter_revision" in frame else None
    def metadata(start: int, end: int) -> dict:
        result = {"csv_columns": frame.columns.tolist(), "selected_signal_columns": list(keys)}
        for name in ("sample_rate_hz", "high_pass_hz", "low_pass_hz", "notch_50hz"):
            if name in frame:
                values = pd.to_numeric(frame[name].iloc[start:end], errors="coerce")
                if values.isna().any() or not np.isfinite(values).all():
                    raise SignalInputError(f"Invalid {name} metadata")
                unique = sorted(float(value) for value in values.unique())
                result[name] = unique if len(unique) <= 32 else {"unique_count": len(unique), "range": [unique[0], unique[-1]]}
        event_path = path.parent / "filter_events.jsonl"
        if event_path.is_file():
            result["filter_events_path"] = str(event_path.resolve())
        acquisition_report = path.parent / "acquisition_report.json"
        if acquisition_report.is_file():
            import json
            report = json.loads(acquisition_report.read_text(encoding="utf-8"))
            result["recording_state"] = report["state"]
            result["recording_source"] = report["source"]
        return result
    return [
        SignalSession(
            session_number=number,
            source_row_start=start,
            source_row_end=end,
            sample_index=indices[start:end],
            ch1_uv=ch1[start:end],
            ch2_uv=ch2[start:end],
            sync=sync[start:end],
            dropped_samples=dropped[start:end],
            pc_missing_samples=pc_missing[start:end],
            signal_kind=signal_kind,
            metadata=metadata(start, end),
            boundary_reason="file_start" if number == 0 else "sample_index_reset",
            ch1_filtered_uv=filtered1[start:end] if filtered1 is not None else None,
            ch2_filtered_uv=filtered2[start:end] if filtered2 is not None else None,
            filter_revision=revisions[start:end] if revisions is not None else None,
        )
        for number, (start, end) in enumerate(zip(boundaries, boundaries[1:]))
        if end > start
    ]
