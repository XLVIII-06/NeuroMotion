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
from .result_store import export_replay_package, relocate_video


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
        self.status = tk.StringVar(value="载入成功对齐的结果目录，建立索引后可同步查看视频和各通道 EMG。")
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Entry(bar, textvariable=self.path).pack(side="left", fill="x", expand=True)
        host._button(bar, "选择结果", self._browse).pack(side="left", padx=6)
        host._button(bar, "载入当前结果", self.load_current).pack(side="left", padx=6)
        host._button(bar, "载入回放", self.load).pack(side="left")
        host._button(bar, "重新定位视频", self.relocate).pack(side="left", padx=5)
        host._button(bar, "打包回放", self.package).pack(side="left")
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
        ttk.Button(controls, text="上一刺激", command=lambda: self.jump_event(-1)).pack(side='left')
        ttk.Button(controls, text="下一刺激", command=lambda: self.jump_event(1)).pack(side='left', padx=5)
        self.slider = ttk.Scale(self, orient="horizontal", from_=0, to=1, command=self._slider)
        self.slider.pack(fill="x", pady=(0, 6))
        self.slider.bind("<ButtonPress-1>", lambda _: self.pause())
        ttk.Label(self, textvariable=self.status, wraplength=1050, foreground="#64748b").pack(fill="x", pady=(0, 6))
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky='nsew', padx=(0, 10))
        self.canvas = tk.Canvas(left, background="#101b2b", height=300, highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind("<Configure>", lambda _: self._paint_image())
        ttk.Label(left, text='刺激标注（设定电流；双击跳转，灰色行不在共同覆盖内）').pack(anchor='w', pady=(5, 0))
        event_host = ttk.Frame(left)
        event_host.pack(fill='x')
        self.event_table = ttk.Treeview(event_host, columns=('time', 'current', 'width', 'state'), show='headings', height=5)
        for key, name, width in zip(('time', 'current', 'width', 'state'), ('公共时间 s', '设定 mA', '脉宽 ms', '状态 / 备注'), (105, 85, 85, 220)):
            self.event_table.heading(key, text=name)
            self.event_table.column(key, width=width)
        scroll = ttk.Scrollbar(event_host, command=self.event_table.yview)
        self.event_table.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.event_table.pack(fill='x', expand=True)
        self.event_table.tag_configure('outside', foreground='#64748b')
        self.event_table.bind('<Double-1>', self._event_selected)
        self.event_cursor = None
        side = ttk.Frame(body)
        side.grid(row=0, column=1, sticky="nsew")
        side.columnconfigure(0, weight=1)
        self.trace_host = side
        self.traces = [TraceCanvas(side, "CH1 · 公共时间 / μV", "#2563eb"), TraceCanvas(side, "CH2 · 公共时间 / μV", "#0f9e82")]
        fields = ("id", "u_px", "v_px", "X_mm", "Y_mm", "state")
        self.positions = ttk.Treeview(side, columns=fields, show="headings", height=6)
        for field in fields:
            self.positions.heading(field, text=field)
            self.positions.column(field, width=60 if field != "state" else 145, stretch=True)
        self._layout_traces()
        self.timer = self.after(33, self._tick)

    def _layout_traces(self):
        # Reserve all position rows, then divide remaining height between traces.
        # pack's request-order shrinking otherwise clips the five-marker table
        # when switching from two to four channels on smaller screens.
        for i in range(5):
            self.trace_host.rowconfigure(i, weight=0, uniform='')
        for i, trace in enumerate(self.traces):
            self.trace_host.rowconfigure(i, weight=1, uniform='signals')
            trace.grid(row=i, column=0, sticky='nsew', pady=(0, 6))
            trace.margin_group = self.traces
        self.positions.grid(row=len(self.traces), column=0, sticky='ew')

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
        self.event_cursor = None
        self.event_table.delete(*self.event_table.get_children())
        for e in engine.events:
            t = e['common_time_s']
            self.event_table.insert('', 'end', iid=e['event_id'], values=[f'{t:.6f}' if t is not None else '未映射',
                e['commanded_current_ma'] if e['commanded_current_ma'] is not None else '未解析',
                e['pulse_width_ms'] if e['pulse_width_ms'] is not None else '未知',
                ('覆盖内' if e['in_common_coverage'] else '覆盖外')+' '+e.get('notes', '')],
                tags=() if e['in_common_coverage'] else ('outside',))
        count=engine.manifest.get("channel_count",2)
        if len(self.traces)!=count:
            for trace in self.traces:trace.destroy()
            colors=("#2563eb","#0f9e82","#9333ea","#dc4775")
            self.traces=[TraceCanvas(self.trace_host,f"CH{i+1} · 公共时间 / μV",colors[i]) for i in range(count)]
            self._layout_traces()
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

    def render(self, t, *, keep_event_cursor=False):
        self.pending_seek = None
        if self.engine is None or self.host.live_page.active:
            return
        if not keep_event_cursor:
            self.event_cursor = None
        try:
            self.snapshot = self.engine.seek(t)
            current = self.snapshot["common_time_s"]
            self.setting_slider = True
            self.slider.set(current)
            self.setting_slider = False
            window = self.snapshot["emg_window"]
            fields = tuple(f"{'filtered' if self.mode.get().startswith('滤波') else 'primary'}{i}" for i in range(1,self.snapshot.get("channel_count",2)+1))
            margin = max(canvas.required_margin(window[field][np.isfinite(window[field])]) for canvas, field in zip(self.traces, fields))
            times = window["common_time_s"]
            end = float(times[-1]) if len(times) else current + 2.5
            x_limits = (end - 5, end)
            for canvas, field in zip(self.traces, fields):
                values = window[field]
                canvas.draw(times, values, 5, current, x_limits=x_limits, left_margin=margin,
                            event_times=[e['common_time_s'] for e in self.engine.events if e['common_time_s'] is not None and e['in_common_coverage']])
            self.positions.delete(*self.positions.get_children())
            for point in self.snapshot["positions"]:
                coordinates = [f"{point[key]:.2f}" if np.isfinite(point[key]) else "—" for key in ("u_px", "v_px", "X_mm", "Y_mm")]
                self.positions.insert("", "end", values=[point["particle_id"], *coordinates, point["quality_flag"]])
            raw_note = "原始值可用" if self.engine.manifest["raw_available"] else "旧记录原始属性未知，显示保存值"
            if self.snapshot.get("channel_count", 2) == 4:
                raw_note = "四通道为软件对齐值；原生 ADC 值见采集文件"
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

    def _seek_event(self, event):
        if self.host.busy or self.host.live_page.active or not event['in_common_coverage']:
            return
        self.pause()
        self.event_cursor = event['common_time_s']
        self.render(self.event_cursor, keep_event_cursor=True)
        self.event_table.selection_set(event['event_id'])
        self.event_table.see(event['event_id'])

    def _event_selected(self, _=None):
        if self.engine and self.event_table.selection():
            key = self.event_table.selection()[0]
            self._seek_event(next(e for e in self.engine.events if e['event_id'] == key))

    def jump_event(self, direction):
        if not self.engine or not self.snapshot:
            return
        current = self.event_cursor if self.event_cursor is not None else self.snapshot['common_time_s']
        eligible = [e for e in self.engine.events if e['in_common_coverage'] and direction*(e['common_time_s']-current) > 1e-9]
        if eligible:
            self._seek_event(min(eligible, key=lambda e: abs(e['common_time_s']-current)))

    def relocate(self):
        if self.host.busy or self.host.live_page.active:
            return
        root = Path(self.path.get())
        if not (root/'result_manifest.json').is_file():
            messagebox.showerror('需要新版结果', '视频重定位适用于包含 result_manifest.json 的结果。', parent=self)
            return
        path = filedialog.askopenfilename(parent=self, title='选择内容相同的视频')
        if path:
            self.pause()
            self.host._background('正在核对视频内容…', lambda: self._relocate(root, path), cancellable=False)

    @staticmethod
    def _relocate(root, path):
        relocate_video(root, path)
        return {'status': 'video_relocated', 'directory': str(root)}

    def package(self):
        if self.host.busy or self.host.live_page.active:
            return
        root = Path(self.path.get())
        if not (root/'result_manifest.json').is_file():
            messagebox.showerror('需要新版结果', '请先用新版生成结果清单，再打包回放。', parent=self)
            return
        parent = filedialog.askdirectory(parent=self, title='选择打包目标父目录')
        if not parent:
            return
        from datetime import datetime
        destination = Path(parent)/f'NeuroMotion_replay_{datetime.now():%Y%m%d_%H%M%S}'
        self.pause()
        include_raw = messagebox.askyesno('打包内容', '是否附带原始 EMG、BL 数据和采集旁文件？\n选择“否”仍可完整回放，且无需原始 CSV。', parent=self)
        self.host._background('正在打包可搬移的同步回放…',
            lambda: export_replay_package(root, destination, include_raw=include_raw,
                cancel_event=self.host.cancel_event, progress=self.host._processing_progress), cancellable=True)

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
