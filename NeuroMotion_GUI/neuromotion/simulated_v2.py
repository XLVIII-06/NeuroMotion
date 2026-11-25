"""Paced, independent ADC clock adapter for the real v2 acquisition path."""
from __future__ import annotations
import math
import time

from .protocol_v2 import BOOT, META, SAMPLES, SYNC_EDGE, NativeSample, SyncEdge, encode_packet


def simulation_metadata():
    return {"firmware": "SIMULATED-4CH", "channel_count": 4, "sample_rate_hz": 1000, "tick_hz": 1000000,
            "vref_uv": 2420000, "gains": [6, 6, 6, 6], "ready_mask": 3, "ttl_start_us": 0,
            "channel_names": ["A1", "A2", "B1", "B2"], "synthetic_only": True}


def ttl_at(t):
    if t < 3500000:
        return (1, 1, 1, 0, 0, 1, 0)[min(6, int(t // 500000))]
    return int(int((t - 3500000) // 500000) % 2 == 0)


class SimulatedFourChannelSource:
    kind = "simulated_four_channel"

    def __init__(self, duration_s=1800, speed=1., ppm=50.):
        if not math.isfinite(duration_s) or duration_s <= 0 or not math.isfinite(speed) or speed < 0 or not math.isfinite(ppm):
            raise ValueError("Invalid four-channel simulation parameters")
        self.duration_s, self.speed, self.ppm = duration_s, speed, ppm
        self.end_us = round(duration_s * 1e6)
        self.time_us = self.sequence = self.edge_index = 0
        self.next_meta = 1000000
        self.next_symbol = 0
        self.previous_level = None
        self.indices = [0, 0]
        self.pending = bytearray()
        self.eof, self.paused = False, True
        self.deadline = 0.
        self.boot = True

    @property
    def description(self):
        return {"kind": self.kind, "channel_count": 4, "duration_s": self.duration_s, "speed": self.speed,
                "relative_ppm_initial": self.ppm, "hardware_validated": False}

    @property
    def in_waiting(self): return 0

    def resume(self): self.paused = False; self.deadline = time.monotonic()

    def _packet(self, kind, value):
        self.pending.extend(encode_packet(kind, value, self.sequence)); self.sequence += 1

    def _generate(self):
        if self.boot:
            self._packet(BOOT, simulation_metadata()); self.boot = False
        end = min(self.time_us + 20000, self.end_us)
        while self.next_symbol < end:
            level = ttl_at(self.next_symbol)
            if level != self.previous_level:
                self._packet(SYNC_EDGE, SyncEdge(self.edge_index, self.next_symbol, level))
                self.edge_index += 1; self.previous_level = level
            self.next_symbol += 500000
        if end >= self.next_meta:
            self._packet(META, simulation_metadata()); self.next_meta += 1000000
        rows = []
        scale = 2420000 / (8388608 * 6)
        for d in (0, 1):
            while True:
                n = self.indices[d]
                t = 4000 + d * 270 + n * 1000 / (1 + d * self.ppm * 1e-6)
                if t >= end: break
                seconds = t / 1e6
                a = 70 * math.sin(2 * math.pi * (73 if d == 0 else 97) * seconds)
                b = 45 * math.cos(2 * math.pi * (41 if d == 0 else 181) * seconds)
                rows.append(NativeSample(d, n, round(t), round(a / scale), round(b / scale)))
                self.indices[d] += 1
        rows.sort(key=lambda sample: sample.time_us)
        for start in range(0, len(rows), 8): self._packet(SAMPLES, rows[start:start + 8])
        self.time_us = end

    def read(self, size):
        if self.pending:
            data = bytes(self.pending[:size]); del self.pending[:size]; return data
        if self.paused or (self.speed and time.monotonic() < self.deadline):
            time.sleep(.002); return b""
        if self.time_us >= self.end_us:
            self.eof = True; return b""
        self._generate()
        if self.speed: self.deadline += .02 / self.speed
        return self.read(size)

    def close(self): self.eof = True
