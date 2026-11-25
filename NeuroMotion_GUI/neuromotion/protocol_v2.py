"""Versioned native ADC events, shared by serial, replay and simulation.

Wire integers are little endian. ADC values are signed 24-bit codes carried
in int32; timestamps are extended MCU timer microseconds, not PC arrival time.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import struct
import zlib

MAGIC = b"NM2!"
VERSION = 2
MAX_PAYLOAD = 1024
META, SAMPLES, SYNC_EDGE, STATUS, BOOT = range(1, 6)
DATA_VALID, SPI_ERROR, STATUS_ERROR, READ_LATE, CAPTURE_ERROR = 4, 8, 16, 32, 64
HEAD = struct.Struct("<4sBBHI")
RECORD = struct.Struct("<4sIQiiI")
EDGE = struct.Struct("<IQB3x")


@dataclass(frozen=True, slots=True)
class NativeSample:
    device: int
    index: int
    time_us: int
    code1: int
    code2: int
    status: int = 0xC00000
    flags: int = DATA_VALID
    dropped: int = 0

    @property
    def valid(self):
        return (bool(self.flags & DATA_VALID) and not self.flags & (SPI_ERROR | STATUS_ERROR | READ_LATE | CAPTURE_ERROR)
                and self.status >> 20 == 12 and -8388608 <= self.code1 <= 8388607 and -8388608 <= self.code2 <= 8388607)


@dataclass(frozen=True, slots=True)
class SyncEdge:
    index: int
    time_us: int
    level: int


@dataclass(frozen=True, slots=True)
class Packet:
    kind: int
    sequence: int
    value: object


def encode_packet(kind: int, value, sequence: int = 0) -> bytes:
    if kind == SAMPLES:
        payload = b"".join(RECORD.pack(bytes([s.device | s.flags]) + s.status.to_bytes(3, "little"),
                                       s.index, s.time_us, s.code1, s.code2, s.dropped) for s in value)
    elif kind == SYNC_EDGE:
        payload = EDGE.pack(value.index, value.time_us, value.level)
    elif kind in (META, BOOT, STATUS):
        payload = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
    else:
        raise ValueError("Unknown v2 packet type")
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("v2 payload exceeds 1024 bytes")
    data = HEAD.pack(MAGIC, VERSION, kind, len(payload), sequence & 0xFFFFFFFF) + payload
    return data + struct.pack("<I", zlib.crc32(data))


def validate_metadata(value):
    if not isinstance(value, dict):
        raise ValueError("v2 metadata must be an object")
    if any(type(value.get(key)) is not int or value[key] != expected for key, expected in
           (("channel_count", 4), ("sample_rate_hz", 1000), ("tick_hz", 1000000))):
        raise ValueError("Unsupported v2 channels/rate/timer")
    gains = value.get("gains", [])
    if not isinstance(gains, (list, tuple)) or len(gains) != 4 or any(type(g) is not int or g not in (1, 2, 3, 4, 6, 8, 12) for g in gains):
        raise ValueError("Invalid v2 ADC gains")
    if type(value.get("vref_uv")) is not int or not 1000000 <= value["vref_uv"] <= 5000000:
        raise ValueError("Invalid v2 reference voltage")
    if "ready_mask" in value and (type(value["ready_mask"]) is not int or not 0 <= value["ready_mask"] <= 3):
        raise ValueError("Invalid v2 readiness mask")
    anchor = value.get("ttl_start_us")
    if anchor is not None and (type(anchor) is not int or not 0 <= anchor < 2**64):
        raise ValueError("Invalid v2 TTL start timestamp")
    return value


def _reject_nonfinite_json(value):
    raise ValueError(f"Non-finite JSON number: {value}")


class V2Decoder:
    def __init__(self):
        self.buffer = bytearray()
        self.frame_errors = self.invalid_records = self.valid_frames = self.sequence_gaps = 0
        self.previous_sequence = None

    def feed(self, data: bytes) -> list[Packet]:
        self.buffer.extend(data)
        packets = []
        while len(self.buffer) >= 4:
            start = self.buffer.find(MAGIC)
            if start < 0:
                keep = next((n for n in (3, 2, 1) if self.buffer[-n:] == MAGIC[:n]), 0)
                del self.buffer[:len(self.buffer) - keep]
                self.frame_errors += 1
                break
            if start:
                del self.buffer[:start]
                self.frame_errors += 1
            if len(self.buffer) < HEAD.size:
                break
            _, version, kind, size, seq = HEAD.unpack_from(self.buffer)
            if version != VERSION or size > MAX_PAYLOAD or kind not in (META, SAMPLES, SYNC_EDGE, STATUS, BOOT):
                del self.buffer[0]
                self.frame_errors += 1
                continue
            total = HEAD.size + size + 4
            if len(self.buffer) < total:
                break
            frame = bytes(self.buffer[:total])
            if zlib.crc32(frame[:-4]) != struct.unpack_from("<I", frame, total - 4)[0]:
                del self.buffer[0]
                self.frame_errors += 1
                continue
            del self.buffer[:total]
            try:
                payload = frame[HEAD.size:-4]
                if kind == SAMPLES:
                    if not payload or len(payload) % RECORD.size:
                        raise ValueError("Invalid native record length")
                    value = []
                    for prefix, n, t, c1, c2, dropped in RECORD.iter_unpack(payload):
                        device = prefix[0] & 3
                        if device > 1 or prefix[0] & 128:
                            raise ValueError("Invalid source flags")
                        value.append(NativeSample(device, n, t, c1, c2, int.from_bytes(prefix[1:], "little"), prefix[0] & 0xFC, dropped))
                elif kind == SYNC_EDGE:
                    n, t, level = EDGE.unpack(payload)
                    if level not in (0, 1):
                        raise ValueError("Invalid TTL")
                    value = SyncEdge(n, t, level)
                else:
                    value = json.loads(payload, parse_constant=_reject_nonfinite_json)
                    if not isinstance(value, dict):
                        raise ValueError("Expected JSON object")
                    if kind in (META, BOOT):
                        validate_metadata(value)
                if kind == BOOT:
                    self.previous_sequence = None
                if self.previous_sequence is not None:
                    delta = (seq - self.previous_sequence) & 0xFFFFFFFF
                    if delta != 1:
                        self.sequence_gaps += max(1, delta - 1)
                self.previous_sequence = seq
                self.valid_frames += 1
                packets.append(Packet(kind, seq, value))
            except (ValueError, TypeError, KeyError, struct.error, UnicodeError):
                self.invalid_records += 1
        return packets
