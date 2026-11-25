"""One ordered native -> clock -> four-channel filter/write implementation."""
from __future__ import annotations

from collections import deque
import csv
import json
import math
from pathlib import Path

import numpy as np

from .clock_alignment import ClockAlignedStream
from .filters import RealtimeFilters
from .protocol_v2 import BOOT, META, SAMPLES, STATUS, SYNC_EDGE, validate_metadata
from .session import write_json

CHANNELS = range(1, 5)
MAIN_FIELDS = ["format_version", "segment_id", "sample_index", "time_sec", "mcu_time_us", "common_time_s", "sync", "sync_valid"]
MAIN_FIELDS += [f"ch{i}_aligned_uv" for i in CHANNELS] + [f"ch{i}_filtered_uv" for i in CHANNELS]
MAIN_FIELDS += [f"quality_ch{i}" for i in CHANNELS] + [f"filter_quality_ch{i}" for i in CHANNELS]
MAIN_FIELDS += ["model_revision_a", "model_revision_b", "filter_revision", "sample_rate_hz", "high_pass_hz", "low_pass_hz", "notch_50hz", "despike_enabled"]
NATIVE_FIELDS = ["segment_id", "device", "native_index", "drdy_time_us", "ch1_code", "ch2_code", "ch1_native_uv", "ch2_native_uv", "adc_status", "flags", "known_dropped", "valid"]


class FourChannelRecorder:
    def __init__(self, settings, bootstrap=None):
        self.settings = settings
        self.metadata = None
        self.segment = 0
        self.aligner = ClockAlignedStream()
        self.origin = None
        self.revision = 0
        self.pending_filter = "initial"
        self.streams = {}
        self.writers = {}
        self.directory = None
        self.native_count = self.native_recorded = self.record_count = self.output_count = 0
        self.device_counts = [0, 0]
        self.hardware_status = {}
        self._boot_seen = False
        self._last_native = [None, None]
        self._edges = deque(maxlen=256)
        self._pending = deque(maxlen=4096)
        self.segments = []
        self.references = []
        self.ttl_marker = "waiting"
        self.start_rows = []
        self._reset_filters()
        if bootstrap:
            self.set_metadata(bootstrap)

    def _reset_filters(self):
        self.filters = [RealtimeFilters(self.settings, channels=1) for _ in CHANNELS]
        self.was_valid = [False] * 4
        self.warm_left = [0] * 4

    def set_metadata(self, metadata):
        validate_metadata(metadata)
        if self.metadata and any(metadata[k] != self.metadata[k] for k in ("vref_uv", "gains", "tick_hz", "sample_rate_hz")):
            raise ValueError("ADC configuration changed without a BOOT/epoch boundary")
        self.metadata = dict(metadata)
        self.aligner.vref_uv = metadata["vref_uv"]
        self.aligner.gains = tuple(metadata["gains"])
        self.aligner.scales = metadata["vref_uv"] / (8388608 * np.asarray(metadata["gains"]).reshape(2, 2))
        anchor = metadata.get("ttl_start_us")
        if anchor is not None:
            if type(anchor) is not int or anchor < 0:
                raise ValueError("Invalid TTL start timestamp")
            self.aligner.ttl_start_us = anchor
        if self.directory:
            write_json(self.directory / "metadata.json", self.metadata)

    def _new_epoch(self):
        self._write_model_events()
        self.references.append(self._reference_snapshot())
        self.segment += 1
        self.aligner = ClockAlignedStream()
        self.metadata = None
        self.origin = None
        self._edges.clear()
        self._pending.clear()
        self._last_native = [None, None]
        self.start_rows = []
        self.ttl_marker = "waiting"
        self._reset_filters()
        self.revision += 1
        self.pending_filter = "mcu_reset"

    def open(self, directory):
        self.directory = Path(directory) / "acquisition"
        self.directory.mkdir(exist_ok=True)
        self.native_recorded = self.record_count = 0
        self.segments = []
        self.references = []
        self._pending.clear()
        previous = self.aligner
        self.aligner = ClockAlignedStream()
        self.origin = None
        if self.metadata:
            self.set_metadata(self.metadata)
        if self._edges:
            self.aligner.feed([self._edges[-1]])
        self._reset_filters()
        self.revision += 1
        self.pending_filter = "recording_start"
        self.start_rows = []
        self.ttl_marker = "waiting"
        try:
            for name, fields in (("emg.partial.csv", MAIN_FIELDS), ("native_samples.csv", NATIVE_FIELDS),
                                 ("sync_edges.csv", ["segment_id", "edge_index", "mcu_time_us", "level"])):
                stream = (self.directory / name).open("w", encoding="utf-8", newline="")
                self.streams[name] = stream
                self.writers[name] = csv.writer(stream)
                self.writers[name].writerow(fields)
            for name in ("clock_models.jsonl", "filter_events.jsonl"):
                self.streams[name] = (self.directory / name).open("w", encoding="utf-8")
            if self._edges:
                e = self._edges[-1]
                self.writers["sync_edges.csv"].writerow([self.segment, e.index, e.time_us, e.level])
        except Exception:
            self.close()
            self.aligner = previous
            raise

    def update_filters(self, settings):
        self.settings = settings
        self._reset_filters()
        self.revision += 1
        self.pending_filter = "settings_changed"
        return self.revision

    def on_packet(self, packet):
        output = []
        reset = False
        if packet.kind == BOOT:
            if self._boot_seen or self.native_count:
                output.extend(self._emit(self.aligner.finish()))
                self._new_epoch()
                reset = True
            self._boot_seen = True
            self.set_metadata(packet.value)
        elif packet.kind == META:
            self.set_metadata(packet.value)
        elif packet.kind == STATUS:
            self.hardware_status = dict(packet.value)
            if packet.value.get("edge_dropped", 0):
                self.aligner.ttl_fault = True
        elif packet.kind == SYNC_EDGE:
            self._edges.append(packet.value)
            self.aligner.feed([packet.value])
            if self.directory:
                e = packet.value
                self.writers["sync_edges.csv"].writerow([self.segment, e.index, e.time_us, e.level])
        elif packet.kind == SAMPLES:
            if not self.metadata:
                if len(self._pending) + len(packet.value) > self._pending.maxlen:
                    raise ValueError("v2 metadata missing for more than the bounded native buffer")
                self._pending.extend(packet.value)
            else:
                events = [*self._pending, *packet.value]; self._pending.clear()
                group = []
                for sample in events:
                    last = self._last_native[sample.device]
                    if last is not None and sample.time_us < last.time_us and sample.index < last.index:
                        output.extend(self._emit(self.aligner.feed(group))); group = []
                        output.extend(self._emit(self.aligner.finish()))
                        meta = self.metadata
                        self._new_epoch(); meta = {**meta, "ttl_start_us": None}; self.set_metadata(meta)
                        reset = True
                    self._last_native[sample.device] = sample
                    group.append(sample)
                    self.native_count += 1; self.device_counts[sample.device] += 1
                    if self.directory:
                        scale = self.aligner.scales[sample.device]
                        u1, u2 = np.array([sample.code1, sample.code2]) * scale
                        self.writers["native_samples.csv"].writerow([self.segment, sample.device, sample.index, sample.time_us,
                            sample.code1, sample.code2, format(u1, ".12g") if sample.valid else "nan", format(u2, ".12g") if sample.valid else "nan",
                            sample.status, sample.flags, sample.dropped, int(sample.valid)])
                        self.native_recorded += 1
                output.extend(self._emit(self.aligner.feed(group)))
        self._write_model_events()
        return output, reset

    def _write_model_events(self):
        for change in self.aligner.pop_model_events():
            if self.directory:
                self.streams["clock_models.jsonl"].write(json.dumps({"segment_id": self.segment, **change}) + "\n")

    def _warm_length(self):
        cutoffs = [v for v in (self.settings.high_pass_hz, self.settings.low_pass_hz) if v > 0]
        warm = math.ceil(5 * 1000 / min(cutoffs)) if cutoffs else 0
        if self.settings.notch_50hz:
            warm = max(warm, 955)
        return warm

    def _filter_block(self, values):
        filtered = np.full_like(values, np.nan)
        quality = np.full(values.shape, "source_gap", dtype="U40")
        for ch in range(4):
            finite = np.isfinite(values[:, ch])
            changes = np.r_[0, np.flatnonzero(finite[1:] != finite[:-1]) + 1, len(values)]
            for first, end in zip(changes[:-1], changes[1:]):
                if not finite[first]:
                    self.was_valid[ch] = False
                    continue
                if not self.was_valid[ch]:
                    self.filters[ch] = RealtimeFilters(self.settings, channels=1)
                    self.warm_left[ch] = self._warm_length()
                clean, changed = self.filters[ch].process(values[first:end, ch:ch + 1])
                filtered[first:end, ch] = clean[:, 0]
                quality[first:end, ch] = np.where(changed, "despike_replaced", "valid")
                warm = min(end - first, self.warm_left[ch])
                quality[first:first + warm, ch] = "filter_warming"
                filtered[first:first + warm, ch] = np.nan
                self.warm_left[ch] -= warm
                self.was_valid[ch] = True
        return filtered, quality

    def _emit(self, blocks):
        plot = []
        for block in blocks:
            if self.origin is None:
                self.origin = int(block.time_us[0])
            filtered, filter_quality = self._filter_block(block.values)
            indices = (block.time_us - self.origin) // 1000
            if self.pending_filter:
                if self.directory:
                    self.streams["filter_events.jsonl"].write(json.dumps({"segment_id": self.segment, "sample_index": int(indices[0]),
                        "mcu_time_us": int(block.time_us[0]), "filter_revision": self.revision,
                        "reason": self.pending_filter, "settings": self.settings.to_dict()}) + "\n")
                self.pending_filter = ""
            anchor = self.aligner.ttl_start_us
            common = (block.time_us - anchor) / 1e6 if anchor is not None else np.full(len(indices), np.nan)
            for j, k in enumerate(indices):
                v, f = block.values[j], filtered[j]
                if self.directory:
                    row = [2, self.segment, int(k), float(k / 1000), int(block.time_us[j]), float(common[j]),
                           int(block.sync[j]), int(block.sync_valid[j]), *v, *f, *block.quality[j], *filter_quality[j],
                           *block.revisions[j], self.revision, 1000, self.settings.high_pass_hz, self.settings.low_pass_hz,
                           int(self.settings.notch_50hz), int(self.settings.despike)]
                    self.writers["emg.partial.csv"].writerow(row)
                    self.record_count += 1
                plot.append((int(k), k / 1000, v[0], v[1], f[0], f[1], int(block.sync[j]) if block.sync_valid[j] else np.nan,
                             self.segment, v[2], v[3], f[2], f[3]))
                if len(self.start_rows) < 4100:
                    self.start_rows.append((int(k), int(block.sync[j]), bool(block.sync_valid[j])))
            self.output_count += len(indices)
            if len(self.start_rows) >= 4000 and self.ttl_marker == "waiting":
                from .timeline import find_start_marker, AlignmentError
                a = np.asarray(self.start_rows)
                try:
                    if not a[:, 2].all(): raise AlignmentError("TTL unknown")
                    find_start_marker(a[:, 1], a[:, 0], is_emg=True)
                    self.ttl_marker = "verified"
                except AlignmentError:
                    self.ttl_marker = "not_found"
            if self.directory:
                if not self.segments or self.segments[-1]["segment_id"] != self.segment:
                    self.segments.append({"segment_id": self.segment, "grid_origin_us": self.origin, "samples": 0})
                self.segments[-1].update(sample_index_end=int(indices[-1]), ttl_start_us=anchor, start_marker=self.ttl_marker)
                self.segments[-1]["samples"] += len(indices)
        return plot

    def finish(self):
        rows = self._emit(self.aligner.finish())
        if self.directory:
            write_json(self.directory / "clock_reference.json", {"segments": [*self.references, self._reference_snapshot()],
                "native_value_type": "signed24_adc_codes", "grid_value_type": "software_aligned_resampled"})
        return rows

    def _reference_snapshot(self):
        return {"segment_id": self.segment, "reference_a": self.aligner.clocks[0].reference,
                "reference_b": self.aligner.clocks[1].reference, "fixed_reference":self.aligner.fixed_reference, "status": self.aligner.status()}

    def flush(self):
        for stream in self.streams.values(): stream.flush()

    def close(self):
        failures = []
        for stream in self.streams.values():
            try: stream.close()
            except OSError as exc: failures.append(str(exc))
        self.streams.clear(); self.writers.clear(); self.directory = None
        if failures: raise OSError("; ".join(failures))

    def status(self):
        return {**self.aligner.status(), "metadata": self.metadata, "device_counts": list(self.device_counts),
                "hardware": self.hardware_status, "native_recorded_samples": self.native_recorded,
                "recorded_samples": self.record_count, "segment_summaries": self.segments,
                "start_marker": self.ttl_marker, "channel_count": 4, "protocol_version": 2}
