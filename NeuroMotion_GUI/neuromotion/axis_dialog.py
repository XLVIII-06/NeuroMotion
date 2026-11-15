"""An in-memory, transactional editor for the live display only."""

from __future__ import annotations

from copy import deepcopy
import tkinter as tk
from tkinter import ttk

from .axes import YAxisSettings, validate_limits, validate_window


UNIT_LABELS = {"自动": "auto", "µV": "uV", "mV": "mV"}
UNIT_NAMES = {value: key for key, value in UNIT_LABELS.items()}


class AxisSettingsDialog(tk.Toplevel):
    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.title("实时采集 · 坐标设置")
        self.transient(page.winfo_toplevel())
        self.resizable(False, False)
        self.draft = deepcopy(page.axes)
        self.error = tk.StringVar()
        self.follow = tk.BooleanVar(value=self.draft.follow)
        self.x_unit = tk.StringVar(value=self.draft.x_unit)
        self.previous_x_unit = self.x_unit.get()
        scale = 1000 if self.x_unit.get() == "ms" else 1
        self.span = tk.StringVar(value=f"{self.draft.window_s * scale:.12g}")
        self.begin = tk.StringVar(value=f"{self.draft.x_limits[0] * scale:.12g}")
        self.end = tk.StringVar(value=f"{self.draft.x_limits[1] * scale:.12g}")
        self.linked = tk.BooleanVar(value=self.draft.linked)
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        x = ttk.LabelFrame(frame, text="共享横轴 · 自 MCU 本轮会话开始计时", padding=10)
        x.pack(fill="x")
        ttk.Checkbutton(x, text="实时跟随（取消后使用手动起止时间）", variable=self.follow).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(x, text="时间单位").grid(row=1, column=0, sticky="w", pady=5)
        units = ttk.Combobox(x, textvariable=self.x_unit, values=["s", "ms"], state="readonly", width=6)
        units.grid(row=1, column=1, sticky="w")
        units.bind("<<ComboboxSelected>>", self._convert_x)
        for row, (label, variable) in enumerate((("跟随窗口跨度", self.span), ("手动起始", self.begin), ("手动结束", self.end)), 2):
            ttk.Label(x, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(x, textvariable=variable, width=19).grid(row=row, column=1, sticky="w")
        ttk.Label(x, text="跨度 1 ms–60 s；手动范围限制在本次冻结缓存内。", foreground="#64748b").grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Checkbutton(frame, text="联动 CH1 / CH2 纵轴（解除后恢复各自设置）", variable=self.linked, command=self._toggle_link).pack(anchor="w", pady=10)
        self.y_notebook = ttk.Notebook(frame)
        self.y_notebook.pack(fill="x", pady=3)
        self.y_editors = []
        for channel in range(2):
            settings = self.draft.channels[channel]
            limits = page.last_y_ranges[channel] if settings.automatic and not self.draft.linked else (settings.minimum_uv, settings.maximum_uv)
            editor = self._y_editor(self.y_notebook, f"CH{channel + 1} 纵轴", settings, limits)
            self.y_editors.append(editor)
            self.y_notebook.add(editor["frame"], text=f"CH{channel + 1}")
        self.common_editor = self._y_editor(self.y_notebook, "联动纵轴 · 两路共同量程", self.draft.common_y,
                                            page.last_y_ranges[0] if self.draft.linked else (-1, 1))
        self.y_notebook.add(self.common_editor["frame"], text="联动量程")
        self._enable_editors()
        ttk.Label(frame, text="手动输入单位只用于数值换算；显示单位可单独固定或自动切换。\nTTL 固定为 0 / 1。显示设置仅保留在本次程序运行中。", foreground="#64748b").pack(anchor="w", pady=6)
        ttk.Label(frame, textvariable=self.error, foreground="#b91c1c", wraplength=620).pack(fill="x", pady=5)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="应用", command=self.apply).pack(side="right", padx=6)
        ttk.Button(buttons, text="关闭", command=self.destroy).pack(side="right")
        self.bind("<Escape>", lambda _: self.destroy())

    def _y_editor(self, parent, title, settings, limits):
        frame = ttk.LabelFrame(parent, text=title, padding=8)
        input_unit = "mV" if settings.unit == "mV" else "µV"
        divisor = 1000 if input_unit == "mV" else 1
        editor = {"frame": frame, "auto": tk.BooleanVar(value=settings.automatic),
                  "min": tk.StringVar(value=f"{limits[0] / divisor:.12g}"),
                  "max": tk.StringVar(value=f"{limits[1] / divisor:.12g}"),
                  "input_unit": tk.StringVar(value=input_unit), "previous_unit": input_unit,
                  "unit": tk.StringVar(value=UNIT_NAMES[settings.unit])}
        ttk.Checkbutton(frame, text="自动量程", variable=editor["auto"]).grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text="显示单位").grid(row=0, column=1, padx=4)
        ttk.Combobox(frame, textvariable=editor["unit"], values=list(UNIT_LABELS), width=6, state="readonly").grid(row=0, column=2, sticky="w")
        for col, key in enumerate(("min", "max")):
            ttk.Label(frame, text="手动下限" if col == 0 else "手动上限").grid(row=1, column=col * 2, pady=4, padx=4)
            ttk.Entry(frame, textvariable=editor[key], width=15).grid(row=1, column=col * 2 + 1)
        box = ttk.Combobox(frame, textvariable=editor["input_unit"], values=["µV", "mV"], width=5, state="readonly")
        box.grid(row=1, column=4, padx=6)
        box.bind("<<ComboboxSelected>>", lambda _: self._convert_y(editor))
        return editor

    def _convert_x(self, _event=None):
        new = self.x_unit.get()
        factor = (1000 if new == "ms" else 1) / (1000 if self.previous_x_unit == "ms" else 1)
        try:
            values = [float(var.get()) * factor for var in (self.span, self.begin, self.end)]
            validate_window(values[0] / (1000 if new == "ms" else 1))
            validate_limits((values[1], values[2]))
        except ValueError:
            self.x_unit.set(self.previous_x_unit)
            self.error.set("请先修正横轴数值，再切换单位。")
            return
        for variable, value in zip((self.span, self.begin, self.end), values):
            variable.set(f"{value:.12g}")
        self.previous_x_unit = new

    def _convert_y(self, editor):
        new = editor["input_unit"].get()
        factor = (1000 if editor["previous_unit"] == "mV" else 1) / (1000 if new == "mV" else 1)
        try:
            low, high = validate_limits((float(editor["min"].get()) * factor, float(editor["max"].get()) * factor))
        except ValueError:
            editor["input_unit"].set(editor["previous_unit"])
            self.error.set("请先修正纵轴上下限，再切换输入单位。")
            return
        editor["min"].set(f"{low:.12g}")
        editor["max"].set(f"{high:.12g}")
        editor["previous_unit"] = new

    @staticmethod
    def _read_y(editor):
        scale = 1000 if editor["input_unit"].get() == "mV" else 1
        value = YAxisSettings(editor["auto"].get(), float(editor["min"].get()) * scale,
                              float(editor["max"].get()) * scale, UNIT_LABELS[editor["unit"].get()])
        value.validate()
        return value

    def _toggle_link(self):
        if self.linked.get():
            try:
                channels = [self._read_y(editor) for editor in self.y_editors]
            except ValueError as exc:
                self.linked.set(False)
                self.error.set(str(exc))
                return
            ranges = [self.page.last_y_ranges[ch] if settings.automatic else (settings.minimum_uv, settings.maximum_uv)
                      for ch, settings in enumerate(channels)]
            common = self.common_editor
            common["auto"].set(all(settings.automatic for settings in channels))
            common["min"].set(f"{min(limits[0] for limits in ranges):.12g}")
            common["max"].set(f"{max(limits[1] for limits in ranges):.12g}")
            common["unit"].set(UNIT_NAMES[channels[0].unit])
            common["input_unit"].set("µV")
            common["previous_unit"] = "µV"
        self._enable_editors()

    def _enable_editors(self):
        self.y_notebook.select(2 if self.linked.get() else 0)
        for editor, enabled in [(editor, not self.linked.get()) for editor in self.y_editors] + [(self.common_editor, self.linked.get())]:
            for widget in editor["frame"].winfo_children():
                if isinstance(widget, ttk.Combobox):
                    widget.configure(state="readonly" if enabled else "disabled")
                elif not isinstance(widget, ttk.Label):
                    widget.state(["!disabled"] if enabled else ["disabled"])

    def apply(self):
        try:
            candidate = deepcopy(self.draft)
            candidate.follow = self.follow.get()
            candidate.x_unit = self.x_unit.get()
            scale = 1000 if candidate.x_unit == "ms" else 1
            candidate.window_s = validate_window(float(self.span.get()) / scale)
            if not candidate.follow:
                candidate.x_limits = validate_limits((float(self.begin.get()) / scale, float(self.end.get()) / scale))
                candidate.window_s = validate_window(candidate.x_limits[1] - candidate.x_limits[0])
            candidate.channels = [self._read_y(editor) for editor in self.y_editors]
            candidate.linked = self.linked.get()
            if candidate.linked:
                candidate.common_y = self._read_y(self.common_editor)
            candidate.validate()
            self.page.apply_axes(candidate)
        except (ValueError, KeyError) as exc:
            self.error.set(f"未应用：{exc}")
            return False
        self.draft = deepcopy(candidate)
        self.error.set("已应用。")
        return True
