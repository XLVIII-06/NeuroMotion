"""DPI-aware axes with a separate, physically clipped Canvas for waveform data."""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont, ttk

import numpy as np

from .axes import auto_limits, nice_ticks, tick_text, validate_limits, value_unit


class TraceCanvas(tk.Canvas):
    def __init__(self, parent, title: str, color: str, digital=False):
        self.axis_font = tkfont.Font(root=parent, font=ttk.Style(parent).lookup("TLabel", "font") or "TkDefaultFont")
        height = max(145, self.axis_font.metrics("linespace") * 4 + 52)
        super().__init__(parent, background="white", height=height, highlightthickness=1, highlightbackground="#dce4ee")
        self.label, self.color, self.digital = title, color, digital
        self.surface = tk.Canvas(self, background="white", highlightthickness=0, borderwidth=0)
        self.last = (np.empty(0), np.empty(0), 10.0, None)
        self.options = {}
        self.margin_group = []
        self.plot_rect = (70, 25, 180, 110)
        self.current_y = (-1.0, 1.0)
        self.current_x = (0.0, 10.0)
        self._resize_job = None
        self.bind("<Configure>", self._schedule_redraw)
        self.bind("<Destroy>", self._destroyed, add="+")

    def _destroyed(self, event):
        if event.widget is self and self._resize_job:
            self.after_cancel(self._resize_job)
            self._resize_job = None

    def _schedule_redraw(self, _event=None):
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after_idle(self._redraw)

    def _redraw(self):
        self._resize_job = None
        options = dict(self.options)
        if self.margin_group:
            margins = []
            for peer in self.margin_group:
                times, values = peer.last[:2]
                visible = (times >= peer.current_x[0]) & (times <= peer.current_x[1])
                margins.append(peer.required_margin(values[visible], peer.options.get("y_limits"), peer.options.get("unit", "auto")))
            options["left_margin"] = max(margins)
        self.draw(*self.last, **options)

    def axis_description(self, values, y_limits=None, unit="auto"):
        limits = (-0.15, 1.15) if self.digital else y_limits or auto_limits(values)
        limits = validate_limits(limits)
        name, divisor = ("", 1.0) if self.digital else value_unit(limits, unit)
        height = self.winfo_height() if self.winfo_height() > 1 else self.winfo_reqheight()
        line_height = self.axis_font.metrics("linespace")
        target = max(2, min(7, int((height - 2 * line_height - 29) / (line_height * 1.4)) + 1))
        ticks, step = (np.array([0., 1.]), 1.0) if self.digital else nice_ticks(limits, target)
        if not self.digital:
            minimum_step = (limits[1] - limits[0]) * line_height * 1.2 / max(1, height - 2 * line_height - 29)
            stride = max(1, int(np.ceil(minimum_step / step)))
            ticks, step = ticks[::stride], step * stride
        labels = [tick_text(value / divisor, step / divisor) for value in ticks]
        return limits, name, divisor, ticks, labels

    def required_margin(self, values, y_limits=None, unit="auto"):
        labels = self.axis_description(values, y_limits, unit)[4]
        return max([self.axis_font.measure(label) for label in labels] + [0]) + 16

    def draw(self, times, values, window_s=10.0, cursor=None, *, x_limits=None, y_limits=None, x_unit="s", unit="auto", left_margin=None):
        times, values = np.asarray(times), np.asarray(values)
        self.last = times, values, window_s, cursor
        self.options = dict(x_limits=x_limits, y_limits=y_limits, x_unit=x_unit, unit=unit, left_margin=left_margin)
        self.delete("all")
        self.surface.delete("all")
        width = max(120, self.winfo_width())
        height = max(100, self.winfo_height())
        if self.winfo_width() <= 1:
            width = max(300, self.winfo_reqwidth())
        if self.winfo_height() <= 1:
            height = self.winfo_reqheight()
        if x_limits is None:
            if len(times):
                end = max(float(times[-1]), float(window_s)) if cursor is None and times[0] >= 0 else float(times[-1])
                x_limits = (end - window_s, end)
            else:
                x_limits = (0.0, float(window_s))
        self.current_x = validate_limits(x_limits)
        visible = (times >= self.current_x[0]) & (times <= self.current_x[1]) & np.isfinite(values)
        description = self.axis_description(values[visible], y_limits, unit)
        self.current_y, name, divisor, y_ticks, y_labels = description
        margin = max(16, left_margin or 0, self.required_margin(values[visible], y_limits, unit))
        margin = min(margin, max(30, width - 65))
        line_height = self.axis_font.metrics("linespace")
        top, bottom = line_height + 14, height - line_height - 15
        right_padding = max(18, self.axis_font.measure("0.00") // 2 + 8)
        left, right = margin, max(margin + 20, width - right_padding)
        self.plot_rect = left, top, right, bottom
        self.surface.place(x=left, y=top, width=max(1, right - left), height=max(1, bottom - top))
        title = self.label.split(" · ")[0] + (f" · {name}" if name else "")
        self.create_text(10, 5, text=title, anchor="nw", font=self.axis_font, fill="#18283d", tags="title")
        low, high = self.current_y
        for tick, label in zip(y_ticks, y_labels):
            y = bottom - (tick - low) / (high - low) * (bottom - top)
            self.create_text(left - 9, y, text=label, anchor="e", font=self.axis_font, fill="#475569", tags="y_tick")
            self.create_line(left - 4, y, left, y, fill="#94a3b8", tags="axis")
            self.surface.create_line(0, y - top, right - left, y - top, fill="#e5ebf3" if tick else "#cbd5e1", tags="grid")
        scale_x = 1000.0 if x_unit == "ms" else 1.0
        target = max(2, min(7, int((right - left) / max(70, self.axis_font.measure("0000.00") + 15))))
        x_ticks, step = nice_ticks(self.current_x, target)
        previous_right = -1e9
        for tick in x_ticks:
            x = left + (tick - self.current_x[0]) / (self.current_x[1] - self.current_x[0]) * (right - left)
            label = tick_text(tick * scale_x, step * scale_x)
            half = self.axis_font.measure(label) / 2 + 2
            text_x = min(max(x, half), width - half)
            if text_x - half >= previous_right + 8:
                self.create_text(text_x, bottom + 7, text=label, anchor="n", font=self.axis_font, fill="#475569", tags="x_tick")
                previous_right = text_x + half
            self.surface.create_line(x - left, 0, x - left, bottom - top, fill="#edf1f7", tags="grid")
        self.create_text(width - 6, 5, text=x_unit, anchor="ne", font=self.axis_font, fill="#64748b", tags="x_unit")
        self.create_line(left, top, left, bottom, right, bottom, fill="#94a3b8", tags="axis")
        finite = np.isfinite(times) & np.isfinite(values)
        coordinates = []
        previous_y = None
        for time_value, value, valid in zip(times, values, finite):
            if not valid:
                if len(coordinates) >= 4:
                    self.surface.create_line(*coordinates, fill=self.color, width=1.2, tags="waveform")
                coordinates, previous_y = [], None
                continue
            # Points just outside the range are retained for correct boundary
            # segments, with clipping performed by the child Canvas itself.
            x = (time_value - self.current_x[0]) / (self.current_x[1] - self.current_x[0]) * (right - left)
            y = (high - value) / (high - low) * (bottom - top)
            if self.digital and previous_y is not None:
                coordinates.extend((float(x), float(previous_y)))
            coordinates.extend((float(x), float(y)))
            previous_y = y
        if len(coordinates) >= 4:
            self.surface.create_line(*coordinates, fill=self.color, width=1.2, tags="waveform")
        elif len(coordinates) == 2:
            x, y = coordinates
            self.surface.create_oval(x - 1, y - 1, x + 1, y + 1, fill=self.color, outline="", tags="waveform")
        if cursor is not None and self.current_x[0] <= cursor <= self.current_x[1]:
            x = (cursor - self.current_x[0]) / (self.current_x[1] - self.current_x[0]) * (right - left)
            self.surface.create_line(x, 0, x, bottom - top, fill="#dc2626", dash=(3, 2), tags="cursor")
        if not np.any(visible):
            self.surface.create_text((right - left) / 2, (bottom - top) / 2, text="等待数据" if not len(times) else "此范围内暂无样本", font=self.axis_font, fill="#64748b")

    def fractions(self, event):
        left, top, right, bottom = self.plot_rect
        x = event.x_root - self.winfo_rootx()
        y = event.y_root - self.winfo_rooty()
        return float(np.clip((x - left) / max(1, right - left), 0, 1)), float(np.clip((bottom - y) / max(1, bottom - top), 0, 1))
