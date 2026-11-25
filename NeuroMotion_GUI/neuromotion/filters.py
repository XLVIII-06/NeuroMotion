"""Stateful causal filters with the WinForms coefficients and optional MAD."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math

import numpy as np
from scipy.signal import sosfilt


@dataclass(frozen=True)
class FilterSettings:
    sample_rate_hz: float = 1000.0
    high_pass_hz: float = 20.0
    low_pass_hz: float = 0.0
    notch_50hz: bool = False
    despike: bool = False

    def validate(self) -> None:
        if not np.isfinite([self.sample_rate_hz, self.high_pass_hz, self.low_pass_hz]).all() or self.sample_rate_hz <= 0:
            raise ValueError("Filter settings must be finite and sample rate positive")
        if any(cutoff < 0 or cutoff >= self.sample_rate_hz / 2 for cutoff in (self.high_pass_hz, self.low_pass_hz)):
            raise ValueError("Filter cutoffs must be below Nyquist")
        if self.high_pass_hz and self.low_pass_hz and self.high_pass_hz >= self.low_pass_hz:
            raise ValueError("High-pass cutoff must be lower than low-pass cutoff")
        if self.notch_50hz and self.sample_rate_hz <= 100:
            raise ValueError("50 Hz notch requires a sample rate above 100 Hz")

    def to_dict(self) -> dict:
        return asdict(self)


class RealtimeFilters:
    def __init__(self, settings: FilterSettings, channels: int = 2):
        settings.validate()
        if channels < 1 or channels > 4:
            raise ValueError("Filter channel count must be 1–4")
        self.channels = channels
        self.settings = settings
        stages = []
        for cutoff, high in ((settings.high_pass_hz, True), (settings.low_pass_hz, False)):
            if not cutoff:
                continue
            for quality in (0.541196100146197, 1.306562964876377):
                omega = 2 * math.pi * cutoff / settings.sample_rate_hz
                cosine, alpha = math.cos(omega), math.sin(omega) / (2 * quality)
                b0 = (1 + cosine) / 2 if high else (1 - cosine) / 2
                stages.append([b0 / (1 + alpha), (-(1 + cosine) if high else 1 - cosine) / (1 + alpha),
                               b0 / (1 + alpha), 1, -2 * cosine / (1 + alpha), (1 - alpha) / (1 + alpha)])
        if settings.notch_50hz:
            omega = 2 * math.pi * 50 / settings.sample_rate_hz
            cosine, alpha = math.cos(omega), math.sin(omega) / 60
            notch = [1 / (1 + alpha), -2 * cosine / (1 + alpha), 1 / (1 + alpha), 1, -2 * cosine / (1 + alpha), (1 - alpha) / (1 + alpha)]
            stages.insert(2 if settings.high_pass_hz else 0, notch)
        self.sos = np.asarray(stages, dtype=float).reshape(-1, 6)
        self.zi = np.zeros((len(stages), 2, channels), dtype=float)
        self.history = np.empty((1000, channels), dtype=float)
        self.history_count = 0
        self.history_position = 0

    def process(self, raw: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        values = np.asarray(raw, dtype=float).copy()
        if values.ndim != 2 or values.shape[1] != self.channels or not np.isfinite(values).all():
            raise ValueError("Filter input must have the configured channels and finite values")
        changed = np.zeros(len(values), dtype=bool)
        if self.settings.despike:
            for index, pair in enumerate(values):
                if self.history_count >= 100:
                    history = self.history[:self.history_count]
                    center = np.median(history, axis=0)
                    sigma = 1.4826 * np.median(np.abs(history - center), axis=0)
                    replace = (sigma > 1e-9) & (np.abs(pair - center) > 6 * sigma)
                    values[index] = np.where(replace, center, pair)
                    changed[index] = bool(replace.any())
                self.history[self.history_position] = values[index]
                self.history_position = (self.history_position + 1) % 1000
                self.history_count = min(1000, self.history_count + 1)
        if len(self.sos) and len(values):
            values, self.zi = sosfilt(self.sos, values, axis=0, zi=self.zi)
        return values, changed
