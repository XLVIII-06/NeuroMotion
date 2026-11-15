"""Bounded, UI-independent acquisition using the actual STM32 byte protocol."""

from __future__ import annotations

from collections import deque
from concurrent.futures import Future
import csv
from dataclasses import dataclass
import json
import math
from pathlib import Path
import queue
import struct
import threading
import time

import numpy as np

from .filters import FilterSettings, RealtimeFilters
from .session import create_session, write_json
from .timeline import AlignmentError, find_start_marker
from .axes import plot_window, validate_window


FRAME_SIZE = 245
HEADER = bytes([0xA5, 0xF2, 0x11, 0xE3])
RECORD = struct.Struct("<IffB3xI")
CSV_FIELDS = ["segment_id", "sample_index", "time_sec", "ch1_raw_uv", "ch2_raw_uv", "ch1_uv", "ch2_uv", "sync",
              "dropped_samples", "pc_missing_samples", "sample_rate_hz", "filter_revision", "high_pass_hz", "low_pass_hz",
              "notch_50hz", "despike_enabled", "despike_replaced", "quality_flag"]


@dataclass(frozen=True, slots=True)
class Sample:
    sample_index: int
    ch1: float
    ch2: float
    sync: int
    dropped: int


class FrameDecoder:
    def __init__(self):
        self.buffer = bytearray()
        self.frame_errors = self.invalid_records = self.valid_frames = 0

    def feed(self, data: bytes) -> list[list[Sample]]:
        self.buffer.extend(data)
        frames = []
        while self.buffer:
            index = self.buffer.find(HEADER)
            if index < 0:
                # Preserve a possible fragmented header prefix.
                keep = next((size for size in (3, 2, 1) if self.buffer[-size:] == HEADER[:size]), 0)
                discarded = len(self.buffer) - keep
                if discarded:
                    self.frame_errors += 1
                    del self.buffer[:discarded]
                break
            if index:
                self.frame_errors += 1
                del self.buffer[:index]
            if len(self.buffer) < FRAME_SIZE:
                break
            if self.buffer[FRAME_SIZE - 1] != 0x5A:
                self.frame_errors += 1
                del self.buffer[0]
                continue
            payload = bytes(self.buffer[4:244])
            del self.buffer[:FRAME_SIZE]
            self.valid_frames += 1
            samples = []
            for index, ch1, ch2, sync, dropped in RECORD.iter_unpack(payload):
                if sync not in (0, 1) or not math.isfinite(ch1) or not math.isfinite(ch2):
                    self.invalid_records += 1
                    continue
                samples.append(Sample(index, ch1, ch2, sync, dropped))
            frames.append(samples)
        return frames


def encode_frame(samples) -> bytes:
    rows = list(samples)
    if len(rows) != 12:
        raise ValueError("A firmware frame contains exactly 12 samples")
    return HEADER + b"".join(RECORD.pack(*row) for row in rows) + b"\x5a"


class BinaryReplaySource:
    kind = "binary_replay"

    def __init__(self, path: str | Path, speed: float = 1.0):
        self.path = Path(path).resolve()
        self.stream = self.path.open("rb")
        self.speed = speed
        if speed < 0 or not math.isfinite(speed):
            self.stream.close()
            raise ValueError("Replay speed must be nonnegative; zero means unpaced")
        self.eof = False
        self.paused = True
        self.deadline = 0.0

    @property
    def description(self):
        return {"kind": self.kind, "path": str(self.path), "speed": self.speed}

    @property
    def in_waiting(self):
        return 0

    def resume(self):
        self.paused = False
        self.deadline = time.monotonic()

    def read(self, size: int) -> bytes:
        if self.paused or (self.speed and time.monotonic() < self.deadline):
            time.sleep(0.002)
            return b""
        data = self.stream.read(size)
        self.eof = not data
        if self.speed:
            self.deadline += len(data) / (245 / 12 * 1000 * self.speed)
        return data

    def close(self):
        self.stream.close()


class SimulatedSource:
    kind = "simulated"

    def __init__(self, duration_s: float = 1800, speed: float = 1.0):
        if not math.isfinite(duration_s) or duration_s <= 0 or not math.isfinite(speed) or speed < 0:
            raise ValueError("Invalid simulation duration/speed")
        self.total_frames = math.ceil(duration_s * 1000 / 12)
        self.frame = 0
        self.speed = speed
        self.eof, self.paused = False, True
        self.deadline = 0.0

    @property
    def description(self):
        return {"kind": self.kind, "duration_s": self.total_frames * 0.012, "speed": self.speed}

    @property
    def in_waiting(self):
        return 0

    def resume(self):
        self.paused = False
        self.deadline = time.monotonic()

    def read(self, size: int) -> bytes:
        if self.paused or (self.speed and time.monotonic() < self.deadline):
            time.sleep(0.002)
            return b""
        count = min(max(1, size // FRAME_SIZE), self.total_frames - self.frame)
        if count <= 0:
            self.eof = True
            return b""
        output = bytearray()
        for _ in range(count):
            rows = []
            for i in range(12):
                index = self.frame * 12 + i
                t = index / 1000
                if t < 3.5:
                    sync = (1, 1, 1, 0, 0, 1, 0)[min(6, int(t / 0.5))]
                else:
                    sync = int(int((t - 3.5) / 0.5) % 2 == 0)
                rows.append((index, 70 * math.sin(2 * math.pi * 73 * t), 45 * math.cos(2 * math.pi * 41 * t), sync, 0))
            output.extend(encode_frame(rows))
            self.frame += 1
        if self.speed:
            self.deadline += count * 0.012 / self.speed
        return bytes(output)

    def close(self):
        self.eof = True


class SerialSource:
    kind = "serial"

    def __init__(self, port: str, baudrate: int = 921600):
        import serial
        self.serial = serial.Serial(port, baudrate, timeout=0.05)
        if hasattr(self.serial, "set_buffer_size"):
            self.serial.set_buffer_size(rx_size=262144, tx_size=4096)
        self.description = {"kind": self.kind, "port": port, "baudrate": baudrate, "format": "8N1"}
        self.eof = False

    @property
    def in_waiting(self):
        return self.serial.in_waiting

    def read(self, size: int) -> bytes:
        return self.serial.read(min(size, max(1, self.in_waiting)))

    def close(self):
        self.serial.close()


class AcquisitionController:
    """One ordered worker owns parsing/filtering/writing; GUI only reads snapshots."""

    def __init__(self, settings: FilterSettings | None = None, display_samples: int = 60000, max_backlog_bytes: int = 200000):
        self.settings = settings or FilterSettings()
        self.filters = RealtimeFilters(self.settings)
        self.commands: queue.Queue = queue.Queue(maxsize=32)
        self.lock = threading.RLock()
        self.history = deque(maxlen=display_samples)
        self.max_backlog_bytes = max_backlog_bytes
        self.thread = None
        self.state = "disconnected"
        self.connection_id = 0
        self.connected_at = self.last_byte_at = self.last_sample_at = None
        self.error = ""
        self.source = None
        self.decoder = FrameDecoder()
        self.record_dir = None
        self.last_record_dir = None
        self.writer_stream = self.binary = self.events_stream = None
        self.writer = None
        self.record_count = self.received_samples = self.index_gaps = self.duplicates = 0
        self.segment = 0
        self.previous_index = None
        self.revision = 0
        self.pending_event = "initial"
        self.start_cache = []
        self.start_marker = "waiting"
        self.segment_summaries = []
        self._stop = False
        self._last_flush = 0.0
        self.stats = {"raw_bytes": 0, "backlog_peak_bytes": 0}

    @staticmethod
    def available_ports():
        from serial.tools import list_ports
        return [port.device for port in list_ports.comports()]

    def connect(self, port: str | None = None, source=None):
        if self.thread and self.thread.is_alive():
            raise RuntimeError("Acquisition is already connected")
        adapter = source if source is not None else SerialSource(port)
        with self.lock:
            self.source = adapter
            self.connection_id += 1
            self.connected_at = time.monotonic()
            self.last_byte_at = self.last_sample_at = None
            self.decoder = FrameDecoder()
            self.commands = queue.Queue(maxsize=32)
            self.history.clear()
            self.previous_index = None
            self.segment = self.revision = 0
            self.index_gaps = self.duplicates = self.received_samples = 0
            self.stats = {"raw_bytes": 0, "backlog_peak_bytes": 0}
            self.start_cache = []
            self.start_marker = "waiting"
            self.error, self._stop = "", False
            self.filters = RealtimeFilters(self.settings)
            self.state = "preview"
            self.thread = threading.Thread(target=self._run, name="NeuroMotion-acquisition", daemon=True)
        self.thread.start()

    def _command(self, name, value=None, timeout=10):
        if not self.thread or not self.thread.is_alive():
            raise RuntimeError(self.error or "Acquisition is disconnected")
        future = Future()
        self.commands.put_nowait((name, value, future))
        return future.result(timeout=timeout)

    def start_recording(self, directory: str | Path):
        return self._command("start", Path(directory).resolve())

    def update_filters(self, settings: FilterSettings):
        settings.validate()
        if settings.sample_rate_hz != 1000:
            raise ValueError("This firmware operates at 1000 samples/s")
        return self._command("filters", settings)

    def stop_recording(self):
        return self._command("stop")

    def disconnect(self):
        if self.thread and self.thread.is_alive():
            self._command("disconnect")
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                raise RuntimeError("Acquisition worker did not terminate")

    def wait(self, timeout=60):
        if self.thread:
            self.thread.join(timeout)
            if self.thread.is_alive():
                raise TimeoutError("Acquisition has not finished")
        if self.error:
            raise RuntimeError(self.error)

    def snapshot(self, max_points: int = 6000, include_plot: bool = True, *, plot_window_s: float | None = None) -> dict:
        if plot_window_s is not None:
            validate_window(plot_window_s)
        with self.lock:
            now = time.monotonic()
            bounds = (self.history[0][1], self.history[-1][1]) if self.history else None
            rows = []
            if include_plot:
                if plot_window_s is None or not self.history:
                    rows = list(self.history)
                else:
                    cutoff = self.history[-1][1] - plot_window_s
                    for row in reversed(self.history):
                        rows.append(row)
                        if row[1] < cutoff:
                            break
                    rows.reverse()
            stats = dict(self.stats)
            state, error = self.state, self.error
            metadata = {"state": state, "error": error, "samples": self.received_samples, "recorded_samples": self.record_count,
                        "index_gaps": self.index_gaps, "duplicates": self.duplicates, "segment_id": self.segment,
                        "frame_errors": self.decoder.frame_errors, "invalid_records": self.decoder.invalid_records,
                        "valid_frames": self.decoder.valid_frames, "start_marker": self.start_marker,
                        "source": self.source.description if self.source else {}, "history_bounds_s": bounds,
                        "connection_elapsed_s": None if self.connected_at is None else now - self.connected_at,
                        "last_byte_age_s": None if self.last_byte_at is None else now - self.last_byte_at,
                        "last_sample_age_s": None if self.last_sample_at is None else now - self.last_sample_at,
                        "plot_epoch": (self.connection_id, int(self.history[-1][7]) if self.history else self.segment), **stats}
        values = np.asarray(rows, dtype=float).reshape(-1, 8)
        metadata["plot"] = plot_window(values, max_points=max_points)
        return metadata

    def _open_recording(self, directory: Path):
        if self.writer:
            raise RuntimeError("A recording is already active")
        manifest = create_session(directory, self.source.description)
        self.record_dir = directory
        self.record_count = 0
        self.record_baseline = {**self.stats, "index_gaps": self.index_gaps, "invalid_records": self.decoder.invalid_records,
                                "frame_errors": self.decoder.frame_errors, "valid_frames": self.decoder.valid_frames}
        self.segment_summaries = []
        self.start_cache = []
        self.start_marker = "waiting"
        try:
            acquisition = directory / "acquisition"
            self.writer_stream = (acquisition / "emg.partial.csv").open("w", encoding="utf-8", newline="")
            self.writer = csv.writer(self.writer_stream)
            self.writer.writerow(CSV_FIELDS)
            self.binary = (acquisition / "serial.bin").open("wb")
            self.binary.write(bytes(self.decoder.buffer))
            self.events_stream = (acquisition / "filter_events.jsonl").open("w", encoding="utf-8")
            self.filters = RealtimeFilters(self.settings)
            self.revision += 1
            self.pending_event = "recording_start"
            self.state = "recording"
            self._last_flush = time.monotonic()
            if hasattr(self.source, "resume"):
                self.source.resume()
            return str(directory / "acquisition" / "emg.csv")
        except Exception:
            self._finalize("open_error", False)
            raise

    def _finalize(self, reason: str, complete: bool):
        directory = self.record_dir
        if directory is None:
            return self.last_record_dir
        self.state = "finalizing"
        try:
            close_errors = []
            for stream in (self.writer_stream, self.binary, self.events_stream):
                if stream is not None:
                    try:
                        stream.flush()
                    except OSError as exc:
                        close_errors.append(str(exc))
                    finally:
                        stream.close()
            if close_errors:
                complete = False
                self.error += "; ".join(close_errors)
            report = {"schema_version": 2, "state": "complete" if complete else "incomplete", "reason": reason,
                      "error": self.error, "source": self.source.description, "hardware_validated": False,
                      "sample_rate_hz": 1000, "recorded_samples": self.record_count,
                      "segment_summaries": self.segment_summaries, "trailing_fragment_bytes": len(self.decoder.buffer),
                      "observed_index_gaps": self.index_gaps - self.record_baseline["index_gaps"],
                      "invalid_records": self.decoder.invalid_records - self.record_baseline["invalid_records"],
                      "frame_errors": self.decoder.frame_errors - self.record_baseline["frame_errors"],
                      "valid_frames": self.decoder.valid_frames - self.record_baseline["valid_frames"],
                      "backlog_peak_bytes": self.stats["backlog_peak_bytes"], "display_buffer_limit": self.history.maxlen,
                      "note": "Index gaps may include firmware drops; counters are not additive"}
            partial = directory / "acquisition" / "emg.partial.csv"
            if complete and partial.exists():
                partial.replace(directory / "acquisition" / "emg.csv")
            write_json(directory / "acquisition" / "acquisition_report.json", report)
            manifest = json.loads((directory / "session.json").read_text(encoding="utf-8"))
            manifest.update(state=report["state"], recorded_samples=self.record_count,
                            emg_csv="acquisition/emg.csv" if complete else "acquisition/emg.partial.csv")
            write_json(directory / "session.json", manifest)
            self.last_record_dir = directory
            return directory
        finally:
            self.record_dir = None
            self.writer_stream = self.binary = self.events_stream = self.writer = None
            self.state = "preview" if not self._stop else "disconnected"

    def _handle_commands(self):
        while not self.commands.empty():
            name, value, future = self.commands.get_nowait()
            try:
                if name == "start":
                    result = self._open_recording(value)
                elif name == "stop":
                    result = self._finalize("user_stop", True)
                elif name == "filters":
                    self.settings = value
                    self.filters = RealtimeFilters(value)
                    self.revision += 1
                    self.pending_event = "settings_changed"
                    result = self.revision
                elif name == "disconnect":
                    self._stop = True
                    result = self._finalize("user_disconnect", True)
                else:
                    raise ValueError(name)
                future.set_result(result)
            except Exception as exc:
                future.set_exception(exc)
                if name in ("start", "stop", "disconnect") and self.record_dir:
                    raise

    def _process_frame(self, samples: list[Sample]):
        # Split at resets/gaps so filter state never bridges an invalid interval.
        for sample in samples:
            if self.previous_index == sample.sample_index:
                self.duplicates += 1
                continue
            if self.previous_index is not None and sample.sample_index != self.previous_index + 1:
                if sample.sample_index < self.previous_index:
                    self.segment += 1
                    self.start_cache = []
                    self.start_marker = "waiting"
                    with self.lock:
                        self.history.clear()
                    reason = "mcu_reset"
                else:
                    self.index_gaps += sample.sample_index - self.previous_index - 1
                    reason = "sample_gap"
                self.filters = RealtimeFilters(self.settings)
                self.revision += 1
                self.pending_event = reason
            self.previous_index = sample.sample_index
            self.stats["firmware_dropped_latest"] = sample.dropped
            filtered, replaced = self.filters.process(np.array([[sample.ch1, sample.ch2]]))
            flag = "filter_reset" if self.pending_event else "valid"
            if self.pending_event and self.events_stream:
                self.events_stream.write(json.dumps({"segment_id": self.segment, "sample_index": sample.sample_index,
                                                    "filter_revision": self.revision, "reason": self.pending_event,
                                                    "settings": self.settings.to_dict(), "sos": self.filters.sos.tolist()}) + "\n")
            self.pending_event = ""
            if replaced[0]:
                flag += ";despike_replaced"
            t = sample.sample_index / 1000
            with self.lock:
                self.history.append((sample.sample_index, t, sample.ch1, sample.ch2, filtered[0, 0], filtered[0, 1], sample.sync, self.segment))
                self.received_samples += 1
                self.last_sample_at = time.monotonic()
            if len(self.start_cache) < 4500:
                self.start_cache.append((sample.sample_index, sample.sync))
                if len(self.start_cache) == 4000:
                    ids, states = np.asarray(self.start_cache).T
                    try:
                        find_start_marker(states, ids, is_emg=True)
                        self.start_marker = "verified"
                    except AlignmentError:
                        self.start_marker = "not_found"
            if self.writer:
                settings = self.settings
                self.writer.writerow([self.segment, sample.sample_index, format(t, ".9g"), format(sample.ch1, ".9g"), format(sample.ch2, ".9g"),
                                      format(filtered[0, 0], ".12g"), format(filtered[0, 1], ".12g"), sample.sync, sample.dropped,
                                      self.index_gaps, 1000, self.revision, settings.high_pass_hz, settings.low_pass_hz,
                                      int(settings.notch_50hz), int(settings.despike), int(replaced[0]), flag])
                self.record_count += 1
                if not self.segment_summaries or self.segment_summaries[-1]["segment_id"] != self.segment:
                    self.segment_summaries.append({"segment_id": self.segment, "sample_index_start": sample.sample_index, "samples": 0})
                self.segment_summaries[-1].update(sample_index_end=sample.sample_index, start_marker=self.start_marker,
                                                 firmware_dropped_latest=sample.dropped)
                self.segment_summaries[-1]["samples"] += 1

    def _run(self):
        try:
            while not self._stop:
                self._handle_commands()
                if self._stop:
                    break
                backlog = self.source.in_waiting
                self.stats["backlog_peak_bytes"] = max(self.stats["backlog_peak_bytes"], backlog)
                if backlog > self.max_backlog_bytes:
                    raise RuntimeError("Serial backlog exceeds configured limit; recording cannot be guaranteed lossless")
                data = self.source.read(4096)
                if data:
                    with self.lock:
                        self.last_byte_at = time.monotonic()
                        self.stats["raw_bytes"] += len(data)
                    if self.binary:
                        self.binary.write(data)
                    for samples in self.decoder.feed(data):
                        self._process_frame(samples)
                elif self.source.eof:
                    self._finalize("replay_eof", True)
                    break
                if self.writer and time.monotonic() - self._last_flush >= 1:
                    for stream in (self.writer_stream, self.binary, self.events_stream):
                        stream.flush()
                    self._last_flush = time.monotonic()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            try:
                self._finalize("fault", False)
            except Exception as final_error:
                self.error += f"; file finalization: {final_error}"
        finally:
            self.source.close()
            self.state = "fault" if self.error else "disconnected"
            while not self.commands.empty():
                _, _, future = self.commands.get_nowait()
                future.set_exception(RuntimeError(self.error or "Acquisition ended"))
