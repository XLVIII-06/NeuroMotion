"""Strict, disk-backed BL-422I CSV import. Stimulus annotations are not current measurements."""
from __future__ import annotations

import csv
import codecs
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
from pathlib import Path
import re
from tempfile import TemporaryDirectory

import numpy as np

from .session import file_info
from .storage import RecordBuffer, close_mapping


SAMPLE_DTYPE = np.dtype([('sequence', 'i8'), ('time_us', 'i8'), ('ttl_mv', 'f8')])
STIMULATE = re.compile(r'^Stimulate\s*:\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*mA\s*$', re.I)


class BLInputError(ValueError):
    pass


def parse_time_us(text: str) -> int:
    parts = text.strip().split(':')
    if len(parts) not in (2, 3) or any(not p.isdigit() for p in parts[:-1]):
        raise BLInputError('Time must be MM:SS.fraction or HH:MM:SS.fraction')
    try:
        second = Decimal(parts[-1])
        if not second.is_finite() or not 0 <= second < 60:
            raise BLInputError('Invalid seconds')
        minutes = int(parts[-2])
        if len(parts) == 3 and minutes >= 60:
            raise BLInputError('Invalid minutes')
        total = (int(parts[0])*3600 if len(parts) == 3 else 0)+minutes*60+second
        us = total*1000000
        if us != us.to_integral_value() or not 0 <= us < 2**63:
            raise BLInputError('Time exceeds supported microsecond range')
        return int(us)
    except InvalidOperation as exc:
        raise BLInputError('Invalid time') from exc


@dataclass
class BLRecording:
    samples: np.ndarray
    events: list[dict]
    metadata: dict
    _owner: TemporaryDirectory

    def close(self):
        close_mapping(self.samples)
        self._owner.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def summary(self):
        return {**self.metadata, 'sample_count': len(self.samples), 'event_count': len(self.events),
                'first_time_s': float(self.samples['time_us'][0])/1e6,
                'last_time_s': float(self.samples['time_us'][-1])/1e6,
                'events': self.events, 'absolute_onset_validated': False,
                'event_semantics': 'single_pulse_start_annotation'}


def load_bl422(path: str | Path, *, parameters_path: str | Path | None = None, cancel_event=None) -> BLRecording:
    path = Path(path)
    with path.open('rb') as stream:
        prefix = stream.read(65536)
    try:
        codecs.getincrementaldecoder('utf-8-sig')().decode(prefix, final=False)
        encoding = 'utf-8-sig'
    except UnicodeDecodeError:
        codecs.getincrementaldecoder('gb18030')().decode(prefix, final=False)
        encoding = 'gb18030'
    owner = TemporaryDirectory(prefix='neuromotion_bl_')
    buffer = RecordBuffer(SAMPLE_DTYPE, Path(owner.name)/'samples.bin')
    samples = None
    events, preamble = [], []
    info = file_info(path)
    try:
        with path.open(encoding=encoding, newline='') as stream:
            reader = csv.reader(stream)
            header = None
            for row in reader:
                cleaned = [c.strip() for c in row]
                if '序号' in cleaned and '时间' in cleaned:
                    header = cleaned
                    break
                preamble.append(','.join(row))
                if reader.line_num > 16:
                    break
            if header is None:
                raise BLInputError('BL header with 序号 and 时间 was not found')
            ttl = next((i for i, s in enumerate(header) if re.fullmatch(r'2通道数据\s*\(mV\)', s)), None)
            if ttl is None or '事件' not in header:
                raise BLInputError('BL import requires 2通道数据(mV) and 事件; ELE is not imported')
            text = '\n'.join(preamble)
            rate = re.search(r'采样率\s*[:：]\s*([\d.]+)\s*Hz', text, re.I)
            count = re.search(r'总共\s*(\d+)\s*个数据点', text)
            if not rate or not count:
                raise BLInputError('BL sample-rate and declared-count metadata are required')
            fs, declared = float(rate[1]), int(count[1])
            if not math.isfinite(fs) or fs <= 0 or declared < 2:
                raise BLInputError('Invalid sample rate/count')
            origin = re.search(r'原始文件\s*[:：]\s*(.+)', text)
            seq_col, time_col, event_col = (header.index(k) for k in ('序号', '时间', '事件'))
            first_seq = first_time = previous = None
            actual = 0
            for row in reader:
                if cancel_event is not None and actual % 4096 == 0 and cancel_event.is_set():
                    raise InterruptedError('BL import cancelled')
                if not row or all(not c.strip() for c in row):
                    continue
                try:
                    if len(row) != len(header):
                        raise BLInputError('Unexpected column count')
                    seq = int(row[seq_col].strip())
                    t = parse_time_us(row[time_col])
                    value = float(row[ttl].strip())
                    if seq < 1 or seq >= 2**63 or not math.isfinite(value):
                        raise BLInputError('Invalid sequence or nonfinite TTL voltage')
                    if first_seq is None:
                        first_seq, first_time = seq, t
                    if previous is not None and (seq != previous[0]+1 or t <= previous[1]):
                        raise BLInputError('Missing/duplicate sequence or non-increasing time')
                    if abs(t-first_time-(seq-first_seq)*1e6/fs) > 1.01:
                        raise BLInputError('Sample time disagrees with sequence and declared sample rate')
                    buffer.append((seq, t, value))
                    annotation = row[event_col].strip()
                    if annotation:
                        match = STIMULATE.fullmatch(annotation)
                        current = float(match[1]) if match else None
                        if current is not None and not math.isfinite(current):
                            raise BLInputError('Nonfinite stimulus setting')
                        events.append({'event_id': f"bl_{info['sha256'][:16]}_{seq}", 'source_row': reader.line_num,
                                       'source_sequence': seq, 'bl_time_s': t/1e6, 'raw_event_text': annotation,
                                       'event_kind': 'single_pulse_start_annotation' if match else 'unparsed_annotation',
                                       'commanded_current_ma': current, 'pulse_width_ms': None, 'notes': '',
                                       'parameters_source': 'not_supplied', 'absolute_onset_validated': False})
                    actual += 1
                    previous = (seq, t)
                except (ValueError, OverflowError) as exc:
                    raise BLInputError(f'BL source line {reader.line_num}: {exc}') from exc
            if actual != declared:
                raise BLInputError(f'Declared {declared} samples but read {actual}')
        samples = buffer.finish()
        metadata = {'source': info, 'encoding': encoding, 'sample_rate_hz': fs, 'declared_samples': declared,
                    'source_recording': origin[1].strip() if origin else None, 'ttl_column': header[ttl],
                    'voltage_unit': 'mV', 'first_sequence': first_seq, 'metadata_lines': preamble,
                    'storage': 'disk_backed_chunked'}
        recording = BLRecording(samples, events, metadata, owner)
        if parameters_path:
            apply_parameters(recording, parameters_path)
        return recording
    except BaseException:
        buffer.close()
        close_mapping(samples)
        owner.cleanup()
        raise


def export_parameter_template(recording: BLRecording, path: str | Path):
    with Path(path).open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['event_id', 'pulse_width_ms', 'notes'])
        writer.writeheader()
        writer.writerows({k: e.get(k) for k in writer.fieldnames} for e in recording.events)


def apply_parameters(recording: BLRecording, path: str | Path):
    by_id = {e['event_id']: e for e in recording.events}
    updates, seen = [], set()
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream):
            key = row.get('event_id', '')
            if key not in by_id or key in seen:
                raise BLInputError('Unknown or duplicate parameter event_id')
            seen.add(key)
            raw = row.get('pulse_width_ms', '').strip()
            width = float(raw) if raw else None
            if width is not None and (not math.isfinite(width) or width <= 0):
                raise BLInputError('Pulse width must be finite and positive, or blank for unknown')
            updates.append((key, width, row.get('notes', '')))
    info = file_info(Path(path))
    for key, width, notes in updates:
        by_id[key].update(pulse_width_ms=width, notes=notes, parameters_source='user_supplied')
    recording.metadata['parameter_file'] = info
