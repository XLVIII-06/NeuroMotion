"""Shared-time video/EMG replay widgets backed by the indexed ReplayEngine."""

from __future__ import annotations

import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

import cv2
import numpy as np

from .live_ui import TraceCanvas
from .replay import ReplayEngine


class ReplayPage(ttk.Frame):
    def __init__(self, parent, host):
        super().__init__(parent, padding=12)
        self.host = host
        self.engine = None
        self.playing = False
        self.photo = None
        self.snapshot = None
        self.setting_slider = False
        self.pending_seek = None
        self.path = tk.StringVar()
        self.speed = tk.StringVar(value="1")
        self.mode = tk.StringVar(value="原始 / 保存值")
        self.status = tk.StringVar(value="载入成功对齐的结果目录，建立索引后可同步查看视频和两路 EMG。")
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Entry(bar, textvariable=self.path).pack(side="left", fill="x", expand=True)
        host._button(bar, "选择结果", self._browse).pack(side="left", padx=6)
        host._button(bar, "载入当前结果", self.load_current).pack(side="left", padx=6)
        host._button(bar, "载入回放", self.load).pack(side="left")
        controls = ttk.Frame(self)
        controls.pack(fill="x", pady=(0, 8))
        ttk.Button(controls, text="播放 / 暂停", command=self.toggle).pack(side="left")
        ttk.Button(controls, text="上一帧", command=lambda: self.step(-1)).pack(side="left", padx=5)
        ttk.Button(controls, text="下一帧", command=lambda: self.step(1)).pack(side="left")
        ttk.Label(controls, text="倍率").pack(side="left", padx=(20, 5))
        speed = ttk.Combobox(controls, textvariable=self.speed, values=["0.25", "0.5", "1", "2"], state="readonly", width=5)
        speed.pack(side="left")
        speed.bind("<<ComboboxSelected>>", lambda _: self._restart_clock())
        mode = ttk.Combobox(controls, textvariable=self.mode, values=["原始 / 保存值", "滤波"], state="readonly", width=14)
        mode.pack(side="left", padx=12)
        mode.bind("<<ComboboxSelected>>", lambda _: self.render(self.snapshot["common_time_s"]) if self.snapshot else None)
        self.slider = ttk.Scale(self, orient="horizontal", from_=0, to=1, command=self._slider)
        self.slider.pack(fill="x", pady=(0, 6))
        self.slider.bind("<ButtonPress-1>", lambda _: self.pause())
        ttk.Label(self, textvariable=self.status, wraplength=1050, foreground="#64748b").pack(fill="x", pady=(0, 6))
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(body, background="#101b2b", height=420, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.canvas.bind("<Configure>", lambda _: self._paint_image())
        side = ttk.Frame(body)
        side.grid(row=0, column=1, sticky="nsew")
        self.traces = [TraceCanvas(side, "CH1 · 公共时间 / μV", "#2563eb"), TraceCanvas(side, "CH2 · 公共时间 / μV", "#0f9e82")]
        for trace in self.traces:
            trace.pack(fill="both", expand=True, pady=(0, 6))
            trace.margin_group = self.traces
        fields = ("id", "u_px", "v_px", "X_mm", "Y_mm", "state")
        self.positions = ttk.Treeview(side, columns=fields, show="headings", height=6)
        for field in fields:
            self.positions.heading(field, text=field)
            self.positions.column(field, width=60 if field != "state" else 145, stretch=True)
        self.positions.pack(fill="x")
        self.timer = self.after(33, self._tick)

    def _browse(self):
        path = filedialog.askdirectory(parent=self, title="选择成功对齐的结果目录")
        if path:
            self.path.set(path)

    def load_current(self):
        if self.host.current_result is None:
            messagebox.showerror("尚无结果", "请先完成时间对齐或载入已有对齐结果。", parent=self)
            return
        self.path.set(str(self.host.current_result))
        self.load()

    def load(self):
        path = Path(self.path.get())
        if not (path / "alignment_report.json").is_file():
            messagebox.showerror("结果无效", "请选择包含成功 alignment_report.json 的结果目录。", parent=self)
            return
        if self.host.busy or self.host.live_page.active:
            messagebox.showwarning("任务互斥", "请先结束当前处理或断开采集，再载入回放。", parent=self)
            return
        self.pause()
        if self.engine:
            self.engine.close()
            self.engine = None
        self.host._background("正在准备同步回放索引…", lambda: {"status": "replay_ready", "engine": ReplayEngine(path, progress=self.host._processing_progress, cancel_event=self.host.cancel_event)}, cancellable=True)

    def set_engine(self, engine):
        self.engine = engine
        self.path.set(str(engine.root))
        self.slider.configure(from_=engine.start, to=engine.end)
        self.render(engine.start)
        self.host.book.select(self)

    def _slider(self, value):
        if self.setting_slider or not self.engine or self.host.live_page.active or self.host.busy:
            return
        if self.pending_seek:
            self.after_cancel(self.pending_seek)
        self.pending_seek = self.after(70, lambda: self.render(float(value)))

    def render(self, t):
        self.pending_seek = None
        if self.engine is None or self.host.live_page.active:
            return
        try:
            self.snapshot = self.engine.seek(t)
            current = self.snapshot["common_time_s"]
            self.setting_slider = True
            self.slider.set(current)
            self.setting_slider = False
            window = self.snapshot["emg_window"]
            fields = ("filtered1", "filtered2") if self.mode.get() == "滤波" else ("primary1", "primary2")
            margin = max(canvas.required_margin(window[field][np.isfinite(window[field])]) for canvas, field in zip(self.traces, fields))
            times = window["common_time_s"]
            end = float(times[-1]) if len(times) else current + 2.5
            x_limits = (end - 5, end)
            for canvas, field in zip(self.traces, fields):
                values = window[field]
                keep = np.isfinite(values)
                canvas.draw(times[keep], values[keep], 5, current, x_limits=x_limits, left_margin=margin)
            self.positions.delete(*self.positions.get_children())
            for point in self.snapshot["positions"]:
                coordinates = [f"{point[key]:.2f}" if np.isfinite(point[key]) else "—" for key in ("u_px", "v_px", "X_mm", "Y_mm")]
                self.positions.insert("", "end", values=[point["particle_id"], *coordinates, point["quality_flag"]])
            raw_note = "原始值可用" if self.engine.manifest["raw_available"] else "旧记录原始属性未知，显示保存值"
            segment = next(item for item in self.engine.report["emg_sessions"] if item["session_number"] == self.engine.report["alignment"]["session_number"])
            kind = segment.get("metadata", {}).get("recording_source", {}).get("kind")
            source_note = {"serial": "串口记录", "simulated": "模拟输入", "binary_replay": "二进制回放源"}.get(kind, "导入记录")
            if self.engine.report.get("fixture_only"):
                source_note = "合成性能基准"
            self.status.set(f"公共时间 {current:.4f} s · 原始视频帧 {self.snapshot['frame_index']} · EMG 样本 {self.snapshot['emg_sample_float']:.2f} · TTL {self.snapshot['led_state']} · {raw_note} · {source_note}")
            self._paint_image()
        except Exception as exc:
            self.pause()
            self.status.set(f"回放失败：{exc}")

    def _paint_image(self):
        if self.snapshot is None:
            return
        image = self.snapshot["image"]
        width, height = max(200, self.canvas.winfo_width()), max(150, self.canvas.winfo_height())
        scale = min(width / image.shape[1], height / image.shape[0])
        image = cv2.resize(image, (max(1, round(image.shape[1] * scale)), max(1, round(image.shape[0] * scale))))
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        data = f"P6\n{image.shape[1]} {image.shape[0]}\n255\n".encode() + image.tobytes()
        self.photo = tk.PhotoImage(master=self, data=data, format="PPM")
        self.canvas.delete("all")
        self.canvas.create_image(width / 2, height / 2, image=self.photo)

    def _restart_clock(self):
        self.anchor_clock = time.monotonic()
        self.anchor_time = self.snapshot["common_time_s"] if self.snapshot else 0

    def toggle(self):
        if not self.engine or self.host.live_page.active or self.host.busy:
            return
        self.playing = not self.playing
        self._restart_clock()

    def pause(self):
        self.playing = False

    def step(self, direction):
        if not self.engine or self.host.live_page.active or self.host.busy:
            return
        self.pause()
        frame = int(np.clip(self.engine.current_frame + direction, self.engine.first_frame, self.engine.last_frame))
        self.render(float(self.engine.frames[frame]["common_time_s"]))

    def _tick(self):
        if self.playing and self.engine and not self.host.busy:
            target = self.anchor_time + (time.monotonic() - self.anchor_clock) * float(self.speed.get())
            self.render(min(target, self.engine.end))
            if target >= self.engine.end:
                self.pause()
        self.timer = self.after(33, self._tick)

    def shutdown(self):
        self.after_cancel(self.timer)
        if self.pending_seek:
            self.after_cancel(self.pending_seek)
        if self.engine:
            self.engine.close()
