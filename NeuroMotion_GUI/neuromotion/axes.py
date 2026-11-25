"""Pure display ranges, engineering units and bounded plot selection."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math

import numpy as np


MIN_WINDOW_S = 0.001
MAX_WINDOW_S = 60.0


def validate_limits(limits: tuple[float, float]) -> tuple[float, float]:
    low, high = map(float, limits)
    if not math.isfinite(low) or not math.isfinite(high) or not math.isfinite(high - low) or low >= high:
        raise ValueError("上下限必须是有限数值，且下限小于上限。")
    return low, high


def validate_window(seconds: float) -> float:
    value = float(seconds)
    if not math.isfinite(value) or not MIN_WINDOW_S - 1e-10 <= value <= MAX_WINDOW_S + 1e-10:
        raise ValueError("时间跨度须在 1 ms 到 60 s 之间。")
    return float(np.clip(value, MIN_WINDOW_S, MAX_WINDOW_S))


def auto_limits(values) -> tuple[float, float]:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if not len(values):
        return -1.0, 1.0
    low, high = float(values.min()), float(values.max())
    if high == low:
        padding = max(abs(low) * 0.1, 1.0)
    else:
        padding = (high - low) * 0.1
    return low - padding, high + padding


def nice_ticks(limits: tuple[float, float], target: int = 5) -> tuple[np.ndarray, float]:
    low, high = validate_limits(limits)
    raw = (high - low) / max(1, target - 1)
    power = 10.0 ** math.floor(math.log10(raw))
    step = next(multiplier * power for multiplier in (1, 2, 5, 10) if multiplier * power >= raw * (1 - 1e-12))
    first = math.ceil(low / step - 1e-10)
    last = math.floor(high / step + 1e-10)
    ticks = np.arange(first, last + 1, dtype=float) * step
    if len(ticks) < 2:
        # Arbitrary endpoints may round to the same label, particularly when
        # milliseconds straddle a half-integer. Prefer a finer aligned grid.
        raw = (high - low) / 2
        power = 10.0 ** math.floor(math.log10(raw))
        step = max(multiplier * power for multiplier in (1, 2, 5, 10) if multiplier * power <= raw * (1 + 1e-12))
        first, last = math.ceil(low / step - 1e-10), math.floor(high / step + 1e-10)
        ticks = np.arange(first, last + 1, dtype=float) * step
    ticks[np.abs(ticks) < step * 1e-10] = 0
    return ticks, step


def tick_text(value: float, step: float) -> str:
    if abs(value) < abs(step) * 1e-9:
        value = 0.0
    decimals = max(0, int(math.ceil(-math.log10(abs(step)) - 1e-10))) if step else 0
    if decimals > 9 or abs(value) >= 1e9:
        return f"{value:.6g}"
    return f"{value:.{decimals}f}"


def value_unit(limits, unit: str = "auto") -> tuple[str, float]:
    if unit not in ("auto", "uV", "mV"):
        raise ValueError("纵轴单位只能是自动、µV 或 mV。")
    millivolts = unit == "mV" or (unit == "auto" and max(abs(limits[0]), abs(limits[1])) >= 1000)
    return ("mV", 1000.0) if millivolts else ("µV", 1.0)


def plot_window(values: np.ndarray, limits=None, max_points: int = 2000) -> np.ndarray:
    """Crop first, then retain analog extrema and exact TTL transitions."""
    if max_points < 2:
        raise ValueError("max_points must be at least 2")
    if not len(values):
        return values
    if limits is not None:
        low, high = validate_limits(limits)
        times = values[:, 1]
        first = max(0, int(np.searchsorted(times, low, side="left")) - 1)
        last = min(len(values), int(np.searchsorted(times, high, side="right")) + 1)
        values = values[first:last]
    if len(values) > max_points:
        columns = [2, 3, 4, 5] + list(range(8, values.shape[1]))
        bins = max(1, max_points // (2 * len(columns) + 2))
        size = max(1, math.ceil(len(values) / bins))
        selected = {0, len(values) - 1}
        ttl = values[:, 6]
        finite = np.isfinite(ttl)
        edges = np.flatnonzero((finite[1:] != finite[:-1]) | (finite[1:] & finite[:-1] & (ttl[1:] != ttl[:-1]))) + 1
        gaps = np.flatnonzero(np.any(np.diff(np.isfinite(values[:, columns]), axis=0), axis=1)) + 1
        edges = np.union1d(edges, gaps)
        selected.update(edges.tolist())
        selected.update((edges - 1).tolist())
        for start in range(0, len(values), size):
            block = values[start:start + size, columns]
            valid_columns = np.isfinite(block).any(axis=0)
            if valid_columns.any():
                block = block[:, valid_columns]
                selected.update((start + np.argmin(np.where(np.isfinite(block), block, np.inf), axis=0)).tolist())
                selected.update((start + np.argmax(np.where(np.isfinite(block), block, -np.inf), axis=0)).tolist())
        values = values[sorted(selected)]
    return values


@dataclass(frozen=True)
class YAxisSettings:
    automatic: bool = True
    minimum_uv: float = -1.0
    maximum_uv: float = 1.0
    unit: str = "auto"

    def validate(self):
        validate_limits((self.minimum_uv, self.maximum_uv))
        value_unit((self.minimum_uv, self.maximum_uv), self.unit)


@dataclass
class LiveAxes:
    window_s: float = 10.0
    x_unit: str = "s"
    follow: bool = True
    x_limits: tuple[float, float] = (0.0, 10.0)
    channels: list[YAxisSettings] = field(default_factory=lambda: [YAxisSettings(), YAxisSettings()])
    linked: bool = False
    common_y: YAxisSettings = field(default_factory=YAxisSettings)

    def validate(self):
        validate_window(self.window_s)
        if self.x_unit not in ("s", "ms"):
            raise ValueError("横轴单位只能是 s 或 ms。")
        validate_limits(self.x_limits)
        if not self.follow:
            validate_window(self.x_limits[1] - self.x_limits[0])
        for settings in [*self.channels, self.common_y]:
            settings.validate()

    def follow_range(self, latest: float | None) -> tuple[float, float]:
        end = max(self.window_s, latest if latest is not None else 0.0)
        return end - self.window_s, end

    def y_settings(self, channel: int) -> YAxisSettings:
        return self.common_y if self.linked else self.channels[channel]

    def y_ranges(self, data: np.ndarray, columns=(4, 5)) -> list[tuple[float, float]]:
        visible = data[(data[:, 1] >= self.x_limits[0]) & (data[:, 1] <= self.x_limits[1])] if len(data) else data
        if self.linked:
            limits = auto_limits(visible[:, list(columns)]) if self.common_y.automatic else (self.common_y.minimum_uv, self.common_y.maximum_uv)
            return [limits] * len(columns)
        return [auto_limits(visible[:, column]) if settings.automatic else (settings.minimum_uv, settings.maximum_uv)
                for column, settings in zip(columns, self.channels)]

    def set_y(self, channel: int, settings: YAxisSettings):
        settings.validate()
        if self.linked:
            self.common_y = settings
        else:
            self.channels[channel] = settings

    def set_linked(self, linked: bool, ranges):
        if linked and not self.linked:
            self.common_y = YAxisSettings(all(item.automatic for item in self.channels),
                                         min(item[0] for item in ranges), max(item[1] for item in ranges), self.channels[0].unit)
        self.linked = linked

    def reset_y(self, channel: int):
        self.set_y(channel, replace(self.y_settings(channel), automatic=True))

    def zoom_y(self, channel: int, limits, factor: float, fraction: float):
        low, high = limits
        anchor = low + float(np.clip(fraction, 0, 1)) * (high - low)
        self.set_y(channel, replace(self.y_settings(channel), automatic=False,
                                    minimum_uv=anchor - (anchor - low) / factor,
                                    maximum_uv=anchor + (high - anchor) / factor))

    def pan_y(self, channel: int, limits, fraction: float):
        delta = (limits[1] - limits[0]) * fraction
        self.set_y(channel, replace(self.y_settings(channel), automatic=False,
                                    minimum_uv=limits[0] + delta, maximum_uv=limits[1] + delta))

    def set_manual_x(self, limits, domain):
        low, high = validate_limits(limits)
        span = validate_window(high - low)
        domain_low, domain_high = validate_limits(domain)
        span = min(span, domain_high - domain_low)
        low = float(np.clip(low, domain_low, domain_high - span))
        self.x_limits = low, low + span
        self.follow = False

    def zoom_x(self, factor: float, fraction: float, domain):
        low, high = self.x_limits
        span = float(np.clip((high - low) / factor, MIN_WINDOW_S, MAX_WINDOW_S))
        anchor = low + float(np.clip(fraction, 0, 1)) * (high - low)
        self.set_manual_x((anchor - fraction * span, anchor + (1 - fraction) * span), domain)
