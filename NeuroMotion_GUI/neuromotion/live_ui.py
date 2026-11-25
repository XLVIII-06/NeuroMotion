"""Real-time acquisition page; its timer never writes experimental samples."""

from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np

from .acquisition import AcquisitionController, BinaryReplaySource, SimulatedSource
from .axes import LiveAxes, YAxisSettings, plot_window, validate_window
from .simulated_v2 import SimulatedFourChannelSource
from .axis_dialog import AxisSettingsDialog
from .filters import FilterSettings
from .plotting import TraceCanvas  # Also kept here for existing replay imports.
from .paths import app_root


@dataclass
class DragGesture:
    channel: int
    vertical: bool
    x: float
    y: float
    x_limits: tuple[float, float]
    y_limits: tuple[float, float] | None
    moved: bool = False


class LiveAcquisitionPage(ttk.Frame):
    def __init__(self, parent, host):
        super().__init__(parent, padding=(14, 10))
        self.host = host
        self.controller = AcquisitionController()
        self.events = queue.Queue()
        self.operating = False
        self.notified_directory = None
        self.kind = tk.StringVar(value="真实串口")
        self.port = tk.StringVar()
        self.replay_path = tk.StringVar()
        self.speed = tk.StringVar(value="1")
        self.root_dir = tk.StringVar(value=str(app_root() / "sessions"))
        self.high_pass = tk.StringVar(value="20")
        self.low_pass = tk.StringVar(value="0")
        self.notch = tk.BooleanVar(value=False)
        self.despike = tk.BooleanVar(value=False)
        self.mode = tk.StringVar(value="滤波")
        self.window = tk.StringVar(value="10")
        self.pause = tk.BooleanVar(value=False)
        self.axes = LiveAxes()
        self.channel_count = 2
        self.x_unit = tk.StringVar(value="s")
        self.view_status = tk.StringVar(value="实时跟随")
        self.frozen_data = None
        self.frozen_domain = (0.0, 10.0)
        self.plot_epoch = None
        self.last_y_ranges = [(-1.0, 1.0), (-1.0, 1.0)]
        self.drag = None
        self.axis_dialog = None
        self._last_plot_stamp = None
        self.summary = tk.StringVar(value="未连接 · 自动识别双通道 v1 / 四通道 v2 · 1000 Hz、921600 baud")
        self.clock_info = tk.StringVar()
        self.data_status = tk.StringVar(value="连接后等待 MCU 输出；实验开始时请手动复位 MCU。")
        self.file_status = tk.StringVar(value="记录后自动把本次 CSV 交给时间对齐页。实验开始前请先记录，再手动复位 MCU。")
        self.error_seen = ""
        self._build()
        self.refresh_ports()
        self.timer = self.after(50, self._tick)

    @property
    def active(self):
        return self.controller.thread is not None and self.controller.thread.is_alive()

    def _build(self):
        source = ttk.LabelFrame(self, text="输入与实验记录", padding=10)
        source.pack(fill="x", pady=(0, 8))
        ttk.Combobox(source, textvariable=self.kind, values=["真实串口", "四通道模拟", "模拟输入", "二进制回放"], state="readonly", width=13).grid(row=0, column=0, padx=4, pady=4)
        self.ports = ttk.Combobox(source, textvariable=self.port, width=13)
        self.ports.grid(row=0, column=1, padx=4)
        ttk.Button(source, text="刷新串口", command=self.refresh_ports).grid(row=0, column=2, padx=4)
        ttk.Label(source, text="回放倍率").grid(row=0, column=3, padx=(15, 4))
        ttk.Entry(source, textvariable=self.speed, width=6).grid(row=0, column=4)
        self.connect_button = ttk.Button(source, text="连接 / 预览", command=self.connect)
        self.connect_button.grid(row=0, column=5, padx=12)
        self.disconnect_button = ttk.Button(source, text="断开", command=lambda: self._operate(self.controller.disconnect))
        self.disconnect_button.grid(row=0, column=6, padx=4)
        ttk.Label(source, text="二进制文件").grid(row=1, column=0, padx=4, pady=4)
        ttk.Entry(source, textvariable=self.replay_path).grid(row=1, column=1, columnspan=5, sticky="ew")
        ttk.Button(source, text="选择 .bin", command=self._browse_binary).grid(row=1, column=6, padx=4)
        ttk.Label(source, text="实验保存目录").grid(row=2, column=0, padx=4, pady=4)
        ttk.Entry(source, textvariable=self.root_dir).grid(row=2, column=1, columnspan=5, sticky="ew")
        ttk.Button(source, text="选择目录", command=self._browse_dir).grid(row=2, column=6, padx=4)
        source.columnconfigure(1, weight=1)
        settings = ttk.LabelFrame(self, text="滤波支路 · 原始值始终保存", padding=8)
        settings.pack(fill="x", pady=(0, 8))
        for col, (label, variable) in enumerate((("高通 Hz", self.high_pass), ("低通 Hz", self.low_pass))):
            ttk.Label(settings, text=label).grid(row=0, column=col * 2, padx=4)
            ttk.Entry(settings, textvariable=variable, width=7).grid(row=0, column=col * 2 + 1, padx=4)
        ttk.Checkbutton(settings, text="50 Hz 陷波", variable=self.notch).grid(row=0, column=4, padx=8)
        ttk.Checkbutton(settings, text="MAD 去尖峰", variable=self.despike).grid(row=0, column=5, padx=8)
        self.apply_button = ttk.Button(settings, text="应用滤波设置", command=self.apply_filters)
        self.apply_button.grid(row=0, column=6, padx=8)
        ttk.Button(settings, text="EMG 20–200 Hz", command=self._emg_preset).grid(row=0, column=7, padx=4)
        controls = ttk.Frame(self)
        controls.pack(fill="x", pady=(0, 8))
        self.start_button = ttk.Button(controls, text="开始记录", command=self.start_recording, style="Accent.TButton")
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="停止记录", command=lambda: self._operate(self.controller.stop_recording))
        self.stop_button.pack(side="left", padx=8)
        ttk.Checkbutton(controls, text="暂停画面", variable=self.pause, command=self._pause_changed).pack(side="left", padx=10)
        mode = ttk.Combobox(controls, textvariable=self.mode, values=["原始", "滤波"], state="readonly", width=6)
        mode.pack(side="left", padx=6)
        self.mode_box = mode
        mode.bind("<<ComboboxSelected>>", lambda _: self.refresh_plot())
        view = ttk.Frame(self)
        view.pack(fill="x", pady=(0, 7))
        ttk.Label(view, text="时间窗口").pack(side="left")
        self.window_box = ttk.Combobox(view, textvariable=self.window, values=["1", "5", "10", "30", "60"], state="readonly", width=7)
        self.window_box.pack(side="left", padx=5)
        self.window_box.bind("<<ComboboxSelected>>", self._window_changed)
        unit_box = ttk.Combobox(view, textvariable=self.x_unit, values=["s", "ms"], state="readonly", width=4)
        unit_box.pack(side="left")
        unit_box.bind("<<ComboboxSelected>>", self._unit_changed)
        ttk.Button(view, text="坐标设置…", command=self.open_axis_settings).pack(side="left", padx=8)
        ttk.Button(view, text="恢复实时", command=self.restore_live).pack(side="left")
        ttk.Button(view, text="重置坐标", command=self.reset_axes).pack(side="left", padx=8)
        ttk.Label(view, textvariable=self.view_status, foreground="#2563eb").pack(side="left", padx=8)
        ttk.Label(self, text="图内滚轮缩放时间，拖拽平移；按住 Shift 调整纵轴；双击 EMG 恢复自动量程及实时跟随。", foreground="#64748b").pack(anchor="w", pady=(0, 5))
        ttk.Label(self, textvariable=self.summary, wraplength=1040, foreground="#2563eb").pack(anchor="w", pady=(0, 5))
        ttk.Label(self, textvariable=self.clock_info, wraplength=1040, foreground="#475569").pack(anchor="w", pady=(0, 5))
        self.data_status_label = ttk.Label(self, textvariable=self.data_status, wraplength=1040, foreground="#64748b")
        self.data_status_label.pack(anchor="w", pady=(0, 5))
        ttk.Label(self, textvariable=self.file_status, wraplength=1040, foreground="#64748b").pack(anchor="w", pady=(0, 8))
        self.traces = []
        self.trace_host = ttk.Frame(self)
        self.trace_host.pack(fill="both", expand=True)
        self._set_channels(2)

    def _set_channels(self, count):
        if len(self.traces) == count + 1:
            return
        for trace in self.traces:
            trace.destroy()
        self.traces = []
        self.channel_count = count
        self.axes.channels = (self.axes.channels + [YAxisSettings() for _ in range(count)])[:count]
        self.last_y_ranges = (self.last_y_ranges + [(-1., 1.)] * count)[:count]
        raw = self.mode.get() in ("原始", "未滤波（已对齐）")
        modes = ["未滤波（已对齐）", "滤波（已对齐）"] if count == 4 else ["原始", "滤波"]
        self.mode_box.configure(values=modes, width=20 if count == 4 else 6)
        self.mode.set(modes[0] if raw else modes[1])
        if self.axis_dialog is not None and self.axis_dialog.winfo_exists():
            self.axis_dialog.destroy()
            self.axis_dialog = None
        colors = ("#2563eb", "#0f9e82", "#9333ea", "#dc4775")
        specifications = [(f"CH{ch+1} · μV", colors[ch], False) for ch in range(count)] + [("MCU TTL", "#d97706", True)]
        for title, color, digital in specifications:
            canvas = TraceCanvas(self.trace_host, title, color, digital)
            canvas.pack(fill="both", expand=True, pady=3)
            self.traces.append(canvas)
            channel = len(self.traces) - 1
            for widget in (canvas, canvas.surface):
                widget.bind("<MouseWheel>", lambda event, ch=channel: self._wheel(event, ch))
                widget.bind("<ButtonPress-1>", lambda event, ch=channel: self._drag_start(event, ch))
                widget.bind("<B1-Motion>", self._drag_move)
                widget.bind("<ButtonRelease-1>", self._drag_end)
                widget.bind("<Double-Button-1>", lambda event, ch=channel: self._double_click(event, ch))
        for canvas in self.traces:
            canvas.margin_group = self.traces
        self._clear_history_view()

    def _emg_preset(self):
        self.high_pass.set("20")
        self.low_pass.set("200")
        if self.active:
            self.apply_filters()

    def _sync_view_controls(self):
        scale = 1000 if self.axes.x_unit == "ms" else 1
        self.x_unit.set(self.axes.x_unit)
        self.window.set(f"{self.axes.window_s * scale:g}")
        self.window_box.configure(values=[f"{value * scale:g}" for value in (1, 5, 10, 30, 60)])
        self.view_status.set("实时跟随" if self.axes.follow else "历史视图 · 后台采集与保存继续")

    def _clear_history_view(self):
        self.frozen_data = None
        self.drag = None
        self.axes.follow = True
        self.pause.set(False)
        self._last_plot_stamp = None

    def _accept_epoch(self, state):
        self._set_channels(state.get("channel_count", 2))
        if state["plot_epoch"] != self.plot_epoch:
            self.plot_epoch = state["plot_epoch"]
            self._clear_history_view()

    def _freeze(self):
        if self.frozen_data is None:
            state = self.controller.snapshot(max_points=60000)
            self._accept_epoch(state)
            bounds = state["history_bounds_s"]
            if not len(state["plot"]):
                # Before the first MCU frame there is no history to inspect.
                # Freezing an empty array here would hide every later frame.
                self._clear_history_view()
                self._sync_view_controls()
                return False
            if self.axes.follow:
                self.axes.x_limits = self.axes.follow_range(bounds[1] if bounds else None)
            # Index gaps can make 60,000 retained samples span more than a
            # minute. Limit history by elapsed device time as well as count.
            cutoff = bounds[1] - 60 if bounds else 0
            self.frozen_data = state["plot"][state["plot"][:, 1] >= cutoff].copy()
            low = min(bounds[0], self.axes.x_limits[0]) if bounds else self.axes.x_limits[0]
            low = max(low, cutoff)
            high = max(bounds[1], self.axes.x_limits[1]) if bounds else self.axes.x_limits[1]
            self.frozen_domain = (low, max(low + .001, high))
        self.axes.follow = False
        self.pause.set(True)
        self._sync_view_controls()
        return True

    def _pause_changed(self):
        if self.pause.get():
            self._freeze()
            self.refresh_plot()
        else:
            self.restore_live()

    def restore_live(self):
        self._clear_history_view()
        self.refresh_plot()

    def reset_axes(self):
        self.axes = LiveAxes(channels=[YAxisSettings() for _ in range(self.channel_count)])
        self.restore_live()

    def _window_changed(self, _event=None):
        self.axes.window_s = validate_window(float(self.window.get()) / (1000 if self.axes.x_unit == "ms" else 1))
        self.restore_live()

    def _unit_changed(self, _event=None):
        self.axes.x_unit = self.x_unit.get()
        self.refresh_plot()

    def open_axis_settings(self):
        if self.axis_dialog is not None and self.axis_dialog.winfo_exists():
            self.axis_dialog.lift()
        else:
            self.axis_dialog = AxisSettingsDialog(self)

    def apply_axes(self, candidate):
        candidate.validate()
        if candidate.follow:
            self._clear_history_view()
        else:
            if not self._freeze():
                raise ValueError("尚未收到采样数据，不能进入历史视图。请先等待 MCU 输出。")
            candidate.set_manual_x(candidate.x_limits, self.frozen_domain)
        self.axes = candidate
        self.pause.set(not candidate.follow)
        self.refresh_plot()

    def refresh_plot(self, state=None):
        if state is None:
            state = self.controller.snapshot(max_points=2000, include_plot=self.frozen_data is None,
                                             plot_window_s=self.axes.window_s)
        self._accept_epoch(state)
        if self.pause.get() and self.axes.follow:
            self._freeze()
        if self.axes.follow:
            bounds = state["history_bounds_s"]
            self.axes.x_limits = self.axes.follow_range(bounds[1] if bounds else None)
            if not len(state["plot"]) and bounds:
                state = self.controller.snapshot(max_points=2000, plot_window_s=self.axes.window_s)
            data = state["plot"]
        else:
            data = self.frozen_data if self.frozen_data is not None else np.empty((0, 12 if self.channel_count == 4 else 8))
        data = plot_window(data, self.axes.x_limits)
        raw = self.mode.get() in ("原始", "未滤波（已对齐）")
        analog = (2, 3, 8, 9) if raw else (4, 5, 10, 11)
        analog = analog[:self.channel_count]
        self.last_y_ranges = self.axes.y_ranges(data, analog)
        columns = (*analog, 6)
        margin = max(canvas.required_margin(data[:, col], self.last_y_ranges[ch] if ch < self.channel_count else None,
                                            self.axes.y_settings(ch).unit if ch < self.channel_count else "auto")
                     for ch, (canvas, col) in enumerate(zip(self.traces, columns)))
        for ch, (canvas, col) in enumerate(zip(self.traces, columns)):
            canvas.draw(data[:, 1], data[:, col], self.axes.window_s, x_limits=self.axes.x_limits,
                        y_limits=self.last_y_ranges[ch] if ch < self.channel_count else None, x_unit=self.axes.x_unit,
                        unit=self.axes.y_settings(ch).unit if ch < self.channel_count else "auto", left_margin=margin)
        self._sync_view_controls()

    def _wheel(self, event, channel):
        factor = 1.2 ** float(np.clip(event.delta / 120, -10, 10))
        x_fraction, y_fraction = self.traces[channel].fractions(event)
        if event.state & 1:
            if channel < self.channel_count:
                self.axes.zoom_y(channel, self.last_y_ranges[channel], factor, y_fraction)
        else:
            if self._freeze():
                self.axes.zoom_x(factor, x_fraction, self.frozen_domain)
                self.axes.window_s = self.axes.x_limits[1] - self.axes.x_limits[0]
            else:
                self.axes.window_s = float(np.clip(self.axes.window_s / factor, .001, 60))
        self.refresh_plot()
        return "break"

    def _drag_start(self, event, channel):
        vertical = bool(event.state & 1)
        if vertical and channel == self.channel_count:
            return "break"
        self.drag = DragGesture(channel, vertical, event.x_root, event.y_root, self.axes.x_limits,
                                self.last_y_ranges[channel] if channel < self.channel_count else None)
        return "break"

    def _drag_move(self, event):
        if self.drag is None:
            return "break"
        drag = self.drag
        delta_pixels = event.y_root - drag.y if drag.vertical else event.x_root - drag.x
        if not drag.moved and abs(delta_pixels) < 4:
            return "break"
        if not drag.moved and not drag.vertical:
            if not self._freeze():
                self.drag = None
                return "break"
            drag.x_limits = self.axes.x_limits
            self.drag = drag
        drag.moved = True
        left, top, right, bottom = self.traces[drag.channel].plot_rect
        if drag.vertical:
            self.axes.pan_y(drag.channel, drag.y_limits, (event.y_root - drag.y) / max(1, bottom - top))
        else:
            delta = (event.x_root - drag.x) / max(1, right - left) * (drag.x_limits[1] - drag.x_limits[0])
            self.axes.set_manual_x((drag.x_limits[0] - delta, drag.x_limits[1] - delta), self.frozen_domain)
        self.refresh_plot()
        return "break"

    def _drag_end(self, _event):
        self.drag = None
        return "break"

    def _double_click(self, _event, channel):
        if channel < self.channel_count:
            self.axes.reset_y(channel)
            self.restore_live()
        self.drag = None
        return "break"

    def refresh_ports(self):
        ports = self.controller.available_ports()
        self.ports.configure(values=ports)
        if ports and not self.port.get():
            self.port.set(ports[0])

    def _browse_binary(self):
        path = filedialog.askopenfilename(parent=self, filetypes=[("串口二进制", "*.bin")])
        if path:
            self.replay_path.set(path)

    def _browse_dir(self):
        path = filedialog.askdirectory(parent=self)
        if path:
            self.root_dir.set(path)

    def _settings(self):
        value = FilterSettings(high_pass_hz=float(self.high_pass.get()), low_pass_hz=float(self.low_pass.get()),
                               notch_50hz=self.notch.get(), despike=self.despike.get())
        value.validate()
        return value

    def _operate(self, operation):
        if self.operating or self.host.busy:
            return
        self.operating = True

        def worker():
            try:
                self.events.put((True, operation()))
            except Exception as exc:
                self.events.put((False, f"{type(exc).__name__}: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def connect(self):
        if self.host.replay_page.playing:
            self.host.replay_page.pause()
        try:
            settings = self._settings()
            kind, port, path, speed = self.kind.get(), self.port.get(), self.replay_path.get(), float(self.speed.get())
            if kind == "真实串口" and not port:
                raise ValueError("请选择设备串口。")
            if kind == "二进制回放" and not Path(path).is_file():
                raise ValueError("请选择可用的串口 .bin 文件。")
            self.controller.settings = settings
        except Exception as exc:
            messagebox.showerror("输入错误", str(exc), parent=self)
            return

        def operation():
            source = SimulatedFourChannelSource(1800, speed) if kind == "四通道模拟" else SimulatedSource(1800, speed) if kind == "模拟输入" else BinaryReplaySource(path, speed) if kind == "二进制回放" else None
            self.controller.connect(port=port, source=source)
            return "连接成功；模拟/二进制回放在点击开始记录后推进。" if source else "串口已连接，预览中。"

        self._operate(operation)

    def apply_filters(self):
        try:
            settings = self._settings()
        except Exception as exc:
            messagebox.showerror("滤波设置错误", str(exc), parent=self)
            return
        self._operate(lambda: f"滤波设置已提交，修订号 {self.controller.update_filters(settings)}")

    def start_recording(self):
        directory = Path(self.root_dir.get()) / f"session_{datetime.now():%Y%m%d_%H%M%S_%f}"
        self.file_status.set(str(directory))
        self._operate(lambda: self.controller.start_recording(directory))

    def _update_stream_status(self, state):
        age = state["last_sample_age_s"]
        if state["state"] not in ("preview", "recording", "finalizing"):
            text, color = "设备未采集中；图表可浏览本次缓存。", "#64748b"
        elif state["samples"] == 0:
            text = "尚未收到有效样本。连接后请手动复位 MCU；固件启动约需 5 秒。"
            if state["source"].get("kind") in ("simulated", "simulated_four_channel", "binary_replay") and not state["raw_bytes"]:
                text = "模拟/二进制回放输入在点击“开始记录”后推进；当前尚无样本。"
            elif state["raw_bytes"]:
                text = "已收到串口字节，但尚无有效样本；请核对固件协议、串口参数和解析计数。"
            color = "#b45309"
        elif age is not None and age >= 2:
            if state["last_byte_age_s"] is not None and state["last_byte_age_s"] < 2:
                text = f"已 {age:.1f} 秒没有新有效样本，但仍有串口字节；请检查帧错误、重复序号与固件协议。"
            else:
                text = f"数据输入已停滞 {age:.1f} 秒（累计 {state['samples']} 样本）。请检查 MCU 是否停止、复位及 ADS1292R 连接；恢复实时不会启动 MCU。"
            color = "#b45309"
        else:
            text = "正在接收新样本。" + ("画面为冻结历史，后台采集继续。" if not self.axes.follow else "")
            color = "#0f766e"
        clock = state.get("clock")
        if clock:
            meta = clock.get("metadata") or {}
            ppm = clock.get("relative_ppm")
            devices = clock["devices"]
            rates = [f"{d['fit']['rate_hz']:.3f} Hz" if d["fit"] else "测量中" for d in devices]
            self.clock_info.set(f"四通道为软件对齐/重采样值 · 原生 A/B {clock['device_counts']} · DRDY {rates[0]} / {rates[1]} · "
                                f"相对频差 {ppm:.2f} ppm · 状态 {devices[0]['state']} / {devices[1]['state']}" if ppm is not None else
                                f"四通道时钟预热中 · 原生 A/B {clock['device_counts']} · 原生记录即时保存，四通道约延后 0.5 s。")
            reference=clock.get("fixed_reference")
            if reference:
                self.clock_info.set(self.clock_info.get()+f" · 20 s参考：同序号Δt={reference['offset_us_at_reference']/1000:.3f} ms（n={reference['native_reference_index']:.0f}），Δf={reference['relative_ppm']:.2f} ppm")
            self.clock_info.set(self.clock_info.get()+f" · 处理缓存 {clock.get('buffer_lag_s',0):.3f} s")
            hardware = clock.get("hardware") or {}
            if hardware:
                counters = []
                for field, label in (("capture_overrun", "捕获溢出"), ("dropped", "无效/缺样"),
                                     ("spi_errors", "SPI 错误"), ("status_errors", "ADC 状态错误"), ("late", "迟读")):
                    counters.append(f"{label} A/B {hardware.get(field+'_a',0)}/{hardware.get(field+'_b',0)}")
                self.clock_info.set(self.clock_info.get()+" · "+" · ".join(counters))
                if any(hardware.get(field, 0) for field in ("capture_overrun_a", "capture_overrun_b", "dropped_a", "dropped_b",
                       "spi_errors_a", "spi_errors_b", "status_errors_a", "status_errors_b", "late_a", "late_b",
                       "edge_dropped", "record_dropped", "tx_errors")):
                    text = "MCU 已报告采集或传输异常；请检查 A/B 错误计数及通道缺口，已有原生记录继续保留。"
                    color = "#b45309"
            if clock.get("ttl_start_us") is None and state["samples"]>4000:
                text="EMG 已接收，TTL 起始锚点尚未捕获；请核对 PA8 原始数字输出到 PB10 的回接。"
                color="#b45309"
            if meta and meta.get("ready_mask") != 3:
                text = f"ADS 初始化未完成：A ID={meta.get('id_a')} / error={meta.get('error_a')}，B ID={meta.get('id_b')} / error={meta.get('error_b')}。"
                color = "#b45309"
        else:
            self.clock_info.set("")
        self.data_status.set(text)
        self.data_status_label.configure(foreground=color)

    def _tick(self):
        try:
            while True:
                success, result = self.events.get_nowait()
                self.operating = False
                if success:
                    if result:
                        self.host._log(str(result))
                else:
                    messagebox.showerror("采集操作失败", str(result), parent=self)
        except queue.Empty:
            pass
        state = self.controller.snapshot(max_points=2000, include_plot=self.frozen_data is None and self.winfo_ismapped(),
                                         plot_window_s=self.axes.window_s)
        self._accept_epoch(state)
        active, recording = self.active, state["state"] == "recording"
        disabled = self.operating or self.host.busy
        for button, allowed in ((self.connect_button, not active), (self.disconnect_button, active),
                                (self.start_button, active and not recording), (self.stop_button, recording), (self.apply_button, active)):
            button.state(["!disabled"] if allowed and not disabled else ["disabled"])
        source_label = {"serial": "真实串口", "simulated": "模拟输入", "simulated_four_channel": "四通道模拟", "binary_replay": "二进制回放"}.get(state["source"].get("kind"), "未选择输入")
        self.summary.set(f"{source_label} · {state['state']} · 接收 {state['samples']} · 已保存 {state['recorded_samples']} · 序号缺口 {state['index_gaps']} · 帧错误 {state['frame_errors']} · 固件丢样 {state.get('firmware_dropped_latest', 0)} · 起始标志 {state['start_marker']}")
        if state.get("channel_count")==4:
            self.summary.set(f"{source_label} · {state['state']} · 原生接收 {state['samples']} · 原生已存 {state.get('native_recorded_samples',0)} · 四通道网格已存 {state['recorded_samples']} · 缺口 {state['index_gaps']} · 帧错误 {state['frame_errors']} · 起始标志 {state['start_marker']}")
        self._update_stream_status(state)
        if state["error"] and state["error"] != self.error_seen:
            self.error_seen = state["error"]
            self.host._log(state["error"])
            messagebox.showerror("采集故障，已保留记录", state["error"], parent=self)
        if self.controller.last_record_dir and self.controller.last_record_dir != self.notified_directory:
            self.notified_directory = self.controller.last_record_dir
            path = self.notified_directory / "acquisition" / "emg.csv"
            if path.is_file():
                self.host.emg.set(str(path))
                self.host.output.set(str(self.notified_directory / "analysis" / f"run_{datetime.now():%Y%m%d_%H%M%S}"))
                self.file_status.set(f"已保存：{path}")
                self.host._log(f"本次 EMG 已交给时间对齐页：{path}")
        stamp = (state["plot_epoch"], state["history_bounds_s"] if self.frozen_data is None else "frozen",
                 self.pause.get(), self.axes.follow, self.mode.get())
        if self.winfo_ismapped() and stamp != self._last_plot_stamp:
            self.refresh_plot(state)
            self._last_plot_stamp = stamp
        self.host._update_controls()
        self.timer = self.after(50, self._tick)

    def shutdown(self):
        self.after_cancel(self.timer)
        self.controller.disconnect()
