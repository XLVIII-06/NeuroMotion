"""Python desktop client for offline alignment, calibration and result review."""

from __future__ import annotations

import csv
import json
import os
import queue
import subprocess
import sys
import threading
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path
from typing import Callable
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .calibration import calibrate_intrinsics, calibrate_plane
from .pipeline import RunConfig, run_pipeline
from .video import Rect, select_roi
from .live_ui import LiveAcquisitionPage
from .replay_ui import ReplayPage
from .paths import app_root, resource_path


PROJECT_ROOT = app_root()
BLUE = "#2563eb"
INK = "#18283d"
MUTED = "#64748b"
BACKGROUND = "#f3f6fa"


class ScrollablePage(ttk.Frame):
    """Keep the input forms usable on smaller screens and at larger font sizes."""

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.canvas = tk.Canvas(self, background=BACKGROUND, highlightthickness=0, height=460)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.content = ttk.Frame(self.canvas, padding=(14, 12))
        window = self.canvas.create_window(0, 0, window=self.content, anchor="nw")
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(window, width=event.width))
        self.content.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))


def open_local(path: str | Path) -> None:
    """Open a known local output with the operating system's default viewer."""
    target = Path(path).resolve()
    if not target.exists():
        raise FileNotFoundError(f"文件或目录不存在：{target}")
    if sys.platform == "win32":
        os.startfile(str(target))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])


class NeuroMotionApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("NeuroMotion · 神经信号与运动同步")
        width = min(1200, self.winfo_screenwidth() - 70)
        height = min(900, self.winfo_screenheight() - 90)
        self.geometry(f"{width}x{height}")
        self.minsize(min(1080, width), min(780, height))
        self.configure(background=BACKGROUND)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.busy = False
        self.cancel_event = threading.Event()
        self.cancellable = False
        self.actions: list[ttk.Button] = []
        self.full_only: list[ttk.Widget] = []
        self.current_result: Path | None = None
        self.preview_image: tk.PhotoImage | None = None
        self.extra_options: dict = {}
        self._preview_after: str | None = None
        self.emg = tk.StringVar()
        self.video = tk.StringVar()
        self.output = tk.StringVar(value=str(self._new_output()))
        self.video_output = tk.StringVar(value=str(self._new_output().with_name("video_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))))
        self.analysis_cache = tk.StringVar()
        self.extract_led = tk.BooleanVar(value=True)
        self.extract_markers = tk.BooleanVar(value=True)
        self.led_roi = tk.StringVar()
        self.marker_roi = tk.StringVar()
        self.intrinsics = tk.StringVar()
        self.homography = tk.StringVar()
        self.markers = tk.StringVar(value="5")
        self.sample_rate = tk.StringVar(value="1000")
        self.led_metric = tk.StringVar(value="p99")
        self.led_threshold = tk.StringVar()
        self.marker_threshold = tk.StringVar(value="200")
        self.sync_only = tk.BooleanVar(value=True)
        self.allow_fps_fallback = tk.BooleanVar(value=False)
        self.images_glob = tk.StringVar()
        self.calib_cols = tk.StringVar(value="11")
        self.calib_rows = tk.StringVar(value="8")
        self.square_mm = tk.StringVar(value="3.0")
        self.calib_frame = tk.StringVar(value="0")
        self.status = tk.StringVar(value="就绪 · 选择配对数据，或载入现有同步样例")
        self.result_path = tk.StringVar(value="尚未载入结果")
        self.result_note = tk.StringVar(value="处理完成后，可在这里查看时间对齐指标和输出数据。")
        self.result_metrics = {key: tk.StringVar(value="—") for key in ("status", "edges", "rms", "max")}
        self.table_name = tk.StringVar()
        self.table_note = tk.StringVar(value="选择导出的 CSV，可预览前 100 行。")
        self._style()
        self._build()
        self.sync_only.trace_add("write", lambda *_: self._update_controls())
        self._update_controls()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._pump_id = self.after(100, self._pump)

    @staticmethod
    def _new_output() -> Path:
        return PROJECT_ROOT / "output" / f"run_{datetime.now():%Y%m%d_%H%M%S_%f}"

    def _style(self) -> None:
        self.option_add("*Font", ("Microsoft YaHei UI", 9))
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 9), foreground=INK)
        style.configure("TFrame", background=BACKGROUND)
        style.configure("TLabel", background=BACKGROUND)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 23, "bold"))
        style.configure("TLabelframe", background=BACKGROUND, bordercolor="#dce4ee", relief="solid")
        style.configure("TLabelframe.Label", background=BACKGROUND, foreground=INK, font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TButton", padding=(12, 6), background="#ffffff", bordercolor="#dce4ee")
        style.map("TButton", background=[("active", "#eaf0f9")])
        style.configure("Accent.TButton", background=BLUE, foreground="white", bordercolor=BLUE, padding=(20, 8))
        style.map("Accent.TButton", background=[("disabled", "#94a3b8"), ("active", "#1d4ed8")], foreground=[("disabled", "#ffffff")])
        style.configure("TEntry", padding=5, fieldbackground="white", bordercolor="#dce4ee")
        style.configure("TCheckbutton", background=BACKGROUND)
        style.configure("TNotebook", background=BACKGROUND, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(20, 9), background="#e7edf5")
        style.map("TNotebook.Tab", background=[("selected", "#ffffff")], foreground=[("selected", BLUE)])
        style.configure("Metric.TLabel", font=("Microsoft YaHei UI", 19, "bold"), foreground=BLUE)
        style.configure("Treeview", rowheight=27, fieldbackground="white", background="white")
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"), padding=5)

    def _button(self, parent, text: str, command: Callable, *, primary: bool = False) -> ttk.Button:
        button = ttk.Button(parent, text=text, command=command, style="Accent.TButton" if primary else "TButton")
        self.actions.append(button)
        return button

    def _row(self, parent, row: int, label: str, variable: tk.StringVar, browse: Callable | None = None, button_text: str = "选择文件", *, full: bool = False) -> None:
        ttk.Label(parent, text=label, width=19).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=5)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=5)
        widgets = [entry]
        if browse is not None:
            button = self._button(parent, button_text, browse)
            button.grid(row=row, column=2, padx=(10, 0), pady=5)
            widgets.append(button)
        if full:
            self.full_only.extend(widgets)

    @staticmethod
    def _card(parent, title: str) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text=title, padding=(14, 8))
        frame.columnconfigure(1, weight=1)
        frame.pack(fill="x", pady=(0, 10))
        return frame

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=(22, 16))
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 14))
        brand = ttk.Frame(header)
        brand.pack(side="left")
        ttk.Label(brand, text="NeuroMotion", style="Title.TLabel").pack(anchor="w")
        ttk.Label(brand, text="神经信号 · 二维运动 · 同一时间轴", style="Muted.TLabel").pack(anchor="w")
        toolbar = ttk.Frame(header)
        toolbar.pack(side="right", anchor="center")
        self._button(toolbar, "载入同步样例", self._load_sample).pack(side="left", padx=4)
        self._button(toolbar, "导入配置", self._load_config).pack(side="left", padx=4)
        self._button(toolbar, "保存配置", self._save_config).pack(side="left", padx=4)
        self.book = ttk.Notebook(outer)
        self.book.pack(fill="both", expand=True)
        self.analysis_page = ScrollablePage(self.book)
        self.calibration_page = ScrollablePage(self.book)
        self.live_tab = ScrollablePage(self.book)
        self.video_page = ScrollablePage(self.book)
        self.analysis = self.analysis_page.content
        self.calibration = self.calibration_page.content
        self.results = ttk.Frame(self.book, padding=(14, 12))
        for page, title in ((self.live_tab, "实时采集"), (self.video_page, "视频分析"), (self.analysis_page, "时间对齐"), (self.calibration_page, "相机标定"), (self.results, "结果与质检")):
            self.book.add(page, text=title)
        self.live_page = LiveAcquisitionPage(self.live_tab.content, self)
        self.live_page.pack(fill="both", expand=True)
        self.replay_page = ReplayPage(self.book, self)
        self.book.add(self.replay_page, text="同步回放")
        self.bind("<MouseWheel>", self._scroll_form, add="+")
        self._build_analysis()
        self._build_video_page()
        self._build_calibration()
        self._build_results()
        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(footer, textvariable=self.status, style="Muted.TLabel").pack(side="left")
        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=190)
        self.progress.pack(side="right")
        self.cancel_button = ttk.Button(footer, text="取消处理", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="right", padx=10)
        log_frame = ttk.Frame(outer)
        log_frame.pack(fill="x", pady=(8, 0))
        self.log = tk.Text(log_frame, height=4, state="disabled", wrap="word", background="#eaf0f6", foreground=INK, relief="flat", padx=10, pady=7)
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log.pack(fill="x", expand=True)

    def _build_analysis(self) -> None:
        files = self._card(self.analysis, "配对输入")
        self._row(files, 0, "EMG 信号 CSV", self.emg, lambda: self._browse_file(self.emg, [("EMG CSV", "*.csv")]))
        self._row(files, 1, "原始视频", self.video, lambda: self._browse_file(self.video, [("视频", "*.mp4 *.mov *.avi *.mkv")]))
        self._row(files, 2, "本次输出目录", self.output, lambda: self._browse_folder(self.output), "选择目录")
        self._row(files, 3, "已有视频分析（可选）", self.analysis_cache, lambda: self._browse_folder(self.analysis_cache), "选择结果")
        mode = ttk.Frame(self.analysis)
        mode.pack(fill="x", pady=(0, 8))
        ttk.Checkbutton(mode, text="仅验证 LED / TTL 同步", variable=self.sync_only).pack(side="left")
        ttk.Label(mode, text="完整融合请取消勾选，并提供反光点 ROI 和标定文件。", style="Muted.TLabel").pack(side="left", padx=16)
        regions = self._card(self.analysis, "检测区域与标定")
        self._row(regions, 0, "LED ROI · 原始帧", self.led_roi, self._choose_led_roi, "视频框选")
        self._row(regions, 1, "反光点 ROI · 去畸变帧", self.marker_roi, self._choose_marker_roi, "视频框选", full=True)
        self._row(regions, 2, "相机内参 JSON", self.intrinsics, lambda: self._browse_file(self.intrinsics, [("JSON", "*.json")]), full=True)
        self._row(regions, 3, "平面单应矩阵 JSON", self.homography, lambda: self._browse_file(self.homography, [("JSON", "*.json")]), full=True)
        ttk.Label(regions, text="ROI 格式为 x,y,w,h。LED 与反光点应位于不同检测区域。", style="Muted.TLabel").grid(row=4, column=1, sticky="w", pady=(3, 2))
        options = self._card(self.analysis, "处理参数")
        options_row = ttk.Frame(options)
        options_row.grid(row=0, column=0, columnspan=3, sticky="w")
        for label, variable, full in (("采样率 Hz", self.sample_rate, False), ("反光点数", self.markers, True), ("标记阈值", self.marker_threshold, True)):
            ttk.Label(options_row, text=label).pack(side="left", padx=(0, 6))
            entry = ttk.Entry(options_row, textvariable=variable, width=7)
            entry.pack(side="left", padx=(0, 20))
            if full:
                self.full_only.append(entry)
        ttk.Label(options_row, text="LED 指标").pack(side="left", padx=(0, 6))
        ttk.Combobox(options_row, textvariable=self.led_metric, values=("p99", "mean"), width=6, state="readonly").pack(side="left", padx=(0, 20))
        ttk.Label(options_row, text="LED 阈值").pack(side="left", padx=(0, 6))
        ttk.Entry(options_row, textvariable=self.led_threshold, width=7).pack(side="left")
        ttk.Label(options, text="LED 阈值留空时自动估计。导出按视频帧和按 EMG 样本组织的两种数据表。", style="Muted.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(9, 0))
        ttk.Checkbutton(options, text="已确认视频为固定帧率：仅在原时间戳无效时允许按 FPS 重建", variable=self.allow_fps_fallback).grid(row=2, column=0, columnspan=3, sticky="w", pady=(5, 0))
        run_row = ttk.Frame(self.analysis)
        run_row.pack(fill="x", pady=(2, 0))
        self.run_button = self._button(run_row, "开始处理", self._run, primary=True)
        self.run_button.pack(side="right")
        self._button(run_row, "新建输出目录", lambda: self.output.set(str(self._new_output()))).pack(side="right", padx=10)
        ttk.Label(run_row, text="起始编码必需 · 结束编码有则校验", style="Muted.TLabel").pack(side="left", anchor="center")

    def _build_calibration(self) -> None:
        ttk.Label(self.calibration, text="固定相机、焦距、分辨率和运动平面后进行标定。", style="Muted.TLabel").pack(anchor="w", pady=(0, 12))
        board = self._card(self.calibration, "棋盘格设置")
        options = ttk.Frame(board)
        options.grid(row=0, column=0, columnspan=3, sticky="w")
        for label, variable in (("内角点列数", self.calib_cols), ("内角点行数", self.calib_rows), ("方格边长 mm", self.square_mm)):
            ttk.Label(options, text=label).pack(side="left", padx=(0, 8))
            ttk.Entry(options, textvariable=variable, width=8).pack(side="left", padx=(0, 24))
        intrinsics = self._card(self.calibration, "1 / 相机内参")
        self._row(intrinsics, 0, "棋盘格图片", self.images_glob, self._choose_images, "选择图片目录")
        self._row(intrinsics, 1, "内参保存位置", self.intrinsics, lambda: self._browse_save(self.intrinsics), "保存到…")
        ttk.Label(intrinsics, text="支持 *.jpg、*.png 等路径模式；至少 3 张不同角度的棋盘格图片。", style="Muted.TLabel").grid(row=2, column=1, sticky="w", pady=5)
        self._button(intrinsics, "计算相机内参", self._calibrate_intrinsics, primary=True).grid(row=3, column=1, sticky="w", pady=8)
        plane = self._card(self.calibration, "2 / 运动平面的毫米坐标")
        self._row(plane, 0, "含棋盘格的原始视频", self.video, lambda: self._browse_file(self.video, [("视频", "*.mp4 *.mov *.avi *.mkv")]))
        self._row(plane, 1, "棋盘格帧号（从 0 开始）", self.calib_frame)
        self._row(plane, 2, "平面矩阵保存位置", self.homography, lambda: self._browse_save(self.homography), "保存到…")
        ttk.Label(plane, text="棋盘格须放在反光点运动平面内，且与实验视频使用相同拍摄设置。", style="Muted.TLabel").grid(row=3, column=1, sticky="w", pady=5)
        self._button(plane, "计算平面单应矩阵", self._calibrate_plane, primary=True).grid(row=4, column=1, sticky="w", pady=8)

    def _build_video_page(self) -> None:
        page = self.video_page.content
        ttk.Label(page, text="不需要 EMG 文件。可单独提取 LED、追踪反光点，或共享解码一起分析。", style="Muted.TLabel").pack(anchor="w", pady=8)
        card = self._card(page, "原视频与独立输出")
        self._row(card, 0, "原始视频", self.video, lambda: self._browse_file(self.video, [("视频", "*.mp4 *.mov *.avi *.mkv")]))
        self._row(card, 1, "视频分析输出目录", self.video_output, lambda: self._browse_folder(self.video_output), "选择目录")
        options = ttk.Frame(page)
        options.pack(fill="x", pady=8)
        ttk.Checkbutton(options, text="LED → TTL", variable=self.extract_led).pack(side="left", padx=8)
        ttk.Checkbutton(options, text="五点追踪 / 坐标换算", variable=self.extract_markers).pack(side="left", padx=8)
        card = self._card(page, "检测参数与坐标系")
        self._row(card, 0, "LED ROI · 原始帧", self.led_roi, self._choose_led_roi, "视频框选")
        self._row(card, 1, "反光点 ROI", self.marker_roi, self._choose_marker_roi, "视频框选")
        self._row(card, 2, "相机内参（可选）", self.intrinsics, lambda: self._browse_file(self.intrinsics, [("JSON", "*.json")]))
        self._row(card, 3, "平面矩阵（毫米坐标）", self.homography, lambda: self._browse_file(self.homography, [("JSON", "*.json")]))
        self._row(card, 4, "反光点数量", self.markers)
        self._row(card, 5, "反光点亮度阈值", self.marker_threshold)
        ttk.Label(card, text="有内参时在去畸变帧框选；无内参时输出原始像素诊断。毫米坐标需要内参及平面矩阵。", style="Muted.TLabel", wraplength=900).grid(row=6, column=0, columnspan=3, sticky="w", pady=8)
        row = ttk.Frame(page)
        row.pack(fill="x", pady=8)
        self._button(row, "开始视频分析", self._run_video, primary=True).pack(side="right")
        self._button(row, "新建输出目录", lambda: self.video_output.set(str(self._new_output()))).pack(side="right", padx=8)
        self._button(row, "打开视频结果", lambda: self._open_path(Path(self.video_output.get()))).pack(side="left")

    def _run_video(self) -> None:
        from .video_analysis import VideoAnalysisConfig, analyze_video
        try:
            config = VideoAnalysisConfig(Path(self.video.get()), Path(self.video_output.get()),
                                         Rect.parse(self.led_roi.get()) if self.extract_led.get() else None,
                                         Rect.parse(self.marker_roi.get()) if self.extract_markers.get() else None,
                                         int(self.markers.get()), Path(self.intrinsics.get()) if self.intrinsics.get() else None,
                                         Path(self.homography.get()) if self.homography.get() else None,
                                         self.led_metric.get(), float(self.led_threshold.get()) if self.led_threshold.get() else None,
                                         int(self.marker_threshold.get()), allow_constant_fps_fallback=self.allow_fps_fallback.get())
        except Exception as exc:
            messagebox.showerror("视频参数错误", str(exc), parent=self)
            return
        self._background("正在独立分析视频…", lambda: analyze_video(config, progress=self._processing_progress), config.output_dir, cancellable=True)

    def _build_results(self) -> None:
        toolbar = ttk.Frame(self.results)
        toolbar.pack(fill="x", pady=(0, 10))
        self._button(toolbar, "载入历史结果", self._browse_result).pack(side="left")
        self._button(toolbar, "打开结果目录", self._open_result).pack(side="left", padx=8)
        self._button(toolbar, "打开诊断原图", self._open_qc).pack(side="left")
        self._button(toolbar, "生成 30 帧抽查包", self._prepare_review).pack(side="left", padx=8)
        self._button(toolbar, "打开抽查包", self._open_review).pack(side="left")
        ttk.Label(self.results, textvariable=self.result_path, style="Muted.TLabel", wraplength=1000).pack(anchor="w", pady=(0, 8))
        metrics = ttk.Frame(self.results)
        metrics.pack(fill="x", pady=(0, 8))
        for column, (key, title) in enumerate((("status", "处理状态"), ("edges", "匹配 TTL 边沿"), ("rms", "RMS 残差"), ("max", "最大残差"))):
            metrics.columnconfigure(column, weight=1)
            card = ttk.LabelFrame(metrics, text=title, padding=(12, 7))
            card.grid(row=0, column=column, sticky="nsew", padx=(0, 8 if column < 3 else 0))
            ttk.Label(card, textvariable=self.result_metrics[key], style="Metric.TLabel").pack(anchor="w")
        ttk.Label(self.results, textvariable=self.result_note, wraplength=1000, style="Muted.TLabel").pack(fill="x", pady=(0, 10))
        views = ttk.Notebook(self.results)
        views.pack(fill="both", expand=True)
        chart = ttk.Frame(views)
        data = ttk.Frame(views, padding=8)
        views.add(chart, text="TTL 诊断图")
        views.add(data, text="数据预览")
        self.qc_canvas = tk.Canvas(chart, background="white", highlightthickness=0)
        self.qc_canvas.pack(fill="both", expand=True)
        self.qc_canvas.bind("<Configure>", self._schedule_preview)
        data_toolbar = ttk.Frame(data)
        data_toolbar.pack(fill="x", pady=(0, 8))
        self.table_combo = ttk.Combobox(data_toolbar, textvariable=self.table_name, state="readonly", width=37)
        self.table_combo.pack(side="left")
        self.table_combo.bind("<<ComboboxSelected>>", lambda _: self._show_table())
        ttk.Button(data_toolbar, text="打开完整 CSV", command=self._open_table).pack(side="left", padx=8)
        ttk.Label(data, textvariable=self.table_note, style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        table_frame = ttk.Frame(data)
        table_frame.pack(fill="both", expand=True)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.table = ttk.Treeview(table_frame, show="headings")
        self.table.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self._render_preview()

    def _browse_file(self, variable: tk.StringVar, types: list[tuple[str, str]]) -> None:
        selected = filedialog.askopenfilename(parent=self, filetypes=types + [("所有文件", "*.*")])
        if selected:
            variable.set(selected)

    def _browse_save(self, variable: tk.StringVar) -> None:
        selected = filedialog.asksaveasfilename(parent=self, defaultextension=".json", filetypes=[("JSON", "*.json")])
        if selected:
            variable.set(selected)

    def _browse_folder(self, variable: tk.StringVar) -> None:
        selected = filedialog.askdirectory(parent=self)
        if selected:
            variable.set(selected)

    def _choose_images(self) -> None:
        selected = filedialog.askdirectory(parent=self, title="选择棋盘格图片目录")
        if selected:
            self.images_glob.set(str(Path(selected) / "*.jpg"))

    def _log(self, value: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", f"[{datetime.now():%H:%M:%S}] {value}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _update_controls(self) -> None:
        disabled = self.busy or (hasattr(self, "live_page") and (self.live_page.active or self.live_page.operating))
        for button in self.actions:
            button.state(["disabled"] if disabled else ["!disabled"])
        for widget in self.full_only:
            widget.state(["disabled"] if disabled or self.sync_only.get() else ["!disabled"])

    def _choose_led_roi(self) -> None:
        try:
            roi = select_roi(self.video.get(), "LED")
            self.led_roi.set(f"{roi.x},{roi.y},{roi.w},{roi.h}")
        except Exception as exc:
            messagebox.showerror("LED 区域选择失败", str(exc), parent=self)

    def _choose_marker_roi(self) -> None:
        try:
            roi = select_roi(self.video.get(), "marker", self.intrinsics.get() or None)
            self.marker_roi.set(f"{roi.x},{roi.y},{roi.w},{roi.h}")
        except Exception as exc:
            messagebox.showerror("反光点区域选择失败", str(exc), parent=self)

    def _form_config(self) -> RunConfig:
        required = [("EMG CSV", self.emg), ("原始视频", self.video), ("输出目录", self.output)]
        if not self.analysis_cache.get():
            required.append(("LED ROI", self.led_roi))
        for label, variable in required:
            if not variable.get().strip():
                raise ValueError(f"请填写或选择{label}。")
        return RunConfig(
            emg_csv=Path(self.emg.get().strip()).resolve(), video_path=Path(self.video.get().strip()).resolve(),
            output_dir=Path(self.output.get().strip()).resolve(), led_roi=Rect.parse(self.led_roi.get()) if self.led_roi.get() else Rect(0, 0, 1, 1),
            sync_only=self.sync_only.get(), marker_roi=Rect.parse(self.marker_roi.get()) if self.marker_roi.get().strip() else None,
            marker_count=int(self.markers.get()), intrinsics_path=Path(self.intrinsics.get()).resolve() if self.intrinsics.get().strip() else None,
            homography_path=Path(self.homography.get()).resolve() if self.homography.get().strip() else None,
            nominal_rate_hz=float(self.sample_rate.get()), led_metric=self.led_metric.get(),
            led_threshold=float(self.led_threshold.get()) if self.led_threshold.get().strip() else None,
            marker_threshold=int(self.marker_threshold.get()), **self.extra_options,
            allow_constant_fps_fallback=self.allow_fps_fallback.get(),
            video_analysis_dir=Path(self.analysis_cache.get()).resolve() if self.analysis_cache.get() else None,
        )

    def _apply_config(self, values: dict, base: Path) -> None:
        if not isinstance(values, dict):
            raise ValueError("配置 JSON 应为一个对象。")
        text_vars = {
            "emg_csv": self.emg, "video_path": self.video, "output_dir": self.output,
            "intrinsics_path": self.intrinsics, "homography_path": self.homography,
            "marker_count": self.markers, "nominal_rate_hz": self.sample_rate,
            "led_metric": self.led_metric, "led_threshold": self.led_threshold,
            "marker_threshold": self.marker_threshold,
            "video_analysis_dir": self.analysis_cache,
        }
        defaults = RunConfig(Path(), Path(), Path(), Rect(0, 0, 1, 1))
        path_keys = {"emg_csv", "video_path", "output_dir", "intrinsics_path", "homography_path", "video_analysis_dir"}
        for key, variable in text_vars.items():
            value = values.get(key, getattr(defaults, key))
            if key in path_keys and value:
                candidate = Path(value)
                value = str((base / candidate).resolve() if not candidate.is_absolute() else candidate)
            variable.set("" if value is None else str(value))
        for key, variable in (("led_roi", self.led_roi), ("marker_roi", self.marker_roi)):
            roi = values.get(key)
            if isinstance(roi, dict):
                roi = Rect(**roi)
                roi = f"{roi.x},{roi.y},{roi.w},{roi.h}"
            variable.set(roi or "")
        self.sync_only.set(bool(values.get("sync_only", False)))
        self.allow_fps_fallback.set(bool(values.get("allow_constant_fps_fallback", False)))
        exposed = set(text_vars) | {"led_roi", "marker_roi", "sync_only", "allow_constant_fps_fallback"}
        self.extra_options = {field.name: values[field.name] for field in fields(RunConfig) if field.name not in exposed and field.name in values}

    def _load_sample(self) -> None:
        try:
            path = resource_path("config.sync-example.json")
            if not path.is_file():
                path = resource_path("config.sync-template.json")
            self._apply_config(json.loads(path.read_text(encoding="utf-8-sig")), path.parent)
            self.output.set(str(self._new_output()))
            self.book.select(self.analysis_page)
            self._log("已载入同步配置；请选择实际配对的 EMG CSV、视频和 LED 区域。")
        except Exception as exc:
            messagebox.showerror("样例载入失败", str(exc), parent=self)

    def _load_config(self) -> None:
        selected = filedialog.askopenfilename(parent=self, title="导入处理配置", filetypes=[("JSON 配置", "*.json")])
        if not selected:
            return
        try:
            path = Path(selected)
            self._apply_config(json.loads(path.read_text(encoding="utf-8-sig")), path.parent)
            self._log(f"配置已导入：{path}")
        except Exception as exc:
            messagebox.showerror("配置载入失败", str(exc), parent=self)

    def _scroll_form(self, event) -> str | None:
        if event.widget.winfo_class() in ("Text", "Treeview", "TCombobox"):
            return None
        for page in (self.analysis_page, self.calibration_page, self.live_tab, self.video_page):
            if self.book.select() == str(page) and str(event.widget).startswith(str(page)):
                top, bottom = page.canvas.yview()
                if top > 0 or bottom < 1:
                    page.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
                    return "break"
        return None

    def _save_config(self) -> None:
        try:
            config = self._form_config()
        except Exception as exc:
            messagebox.showerror("配置不完整", str(exc), parent=self)
            return
        selected = filedialog.asksaveasfilename(parent=self, title="保存处理配置", defaultextension=".json", filetypes=[("JSON 配置", "*.json")])
        if selected:
            try:
                values = {key: str(value) if isinstance(value, Path) else value for key, value in asdict(config).items()}
                Path(selected).write_text(json.dumps(values, indent=2, ensure_ascii=False), encoding="utf-8")
                self._log(f"配置已保存：{selected}")
            except Exception as exc:
                messagebox.showerror("配置保存失败", str(exc), parent=self)

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.status.set("正在取消处理并保留原始数据…")

    def _processing_progress(self, message: str) -> None:
        if self.cancel_event.is_set():
            raise InterruptedError("Processing cancelled; original inputs are preserved")
        self.events.put(("log", message))

    def _background(self, label: str, operation: Callable, result_dir: Path | None = None, cancellable=False) -> None:
        if self.busy:
            return
        if self.live_page.active or self.live_page.operating:
            messagebox.showwarning("采集优先", "请先停止记录并断开串口预览，再运行离线任务。", parent=self)
            return
        self.busy = True
        self.cancel_event.clear()
        self.cancellable = cancellable
        self.cancel_button.state(["!disabled"] if cancellable else ["disabled"])
        self._update_controls()
        self.status.set(label)
        self.progress.start(12)
        self._log(label)

        def worker() -> None:
            try:
                result = operation()
                self.events.put(("done", {"label": label, "result": result, "directory": result_dir}))
            except Exception as exc:
                self.events.put(("error", {"message": f"{type(exc).__name__}: {exc}", "directory": result_dir}))

        threading.Thread(target=worker, daemon=True).start()

    def _run(self) -> None:
        if self.busy:
            return
        try:
            config = self._form_config()
            config.validate()
            if config.output_dir.exists() and any(config.output_dir.iterdir()):
                raise ValueError("输出目录已有文件。请点击“新建输出目录”，保留本次独立结果。")
        except Exception as exc:
            messagebox.showerror("请检查输入", str(exc), parent=self)
            return
        self._background("正在处理配对数据…", lambda: run_pipeline(config, progress=self._processing_progress, cancel_event=self.cancel_event), config.output_dir, cancellable=True)

    def _calibration_pattern(self) -> tuple[tuple[int, int], float]:
        pattern = (int(self.calib_cols.get()), int(self.calib_rows.get()))
        square = float(self.square_mm.get())
        if min(pattern) < 2 or square <= 0:
            raise ValueError("棋盘格内角点数至少为 2，方格边长必须大于 0。")
        return pattern, square

    def _calibrate_intrinsics(self) -> None:
        if self.busy:
            return
        try:
            pattern, square = self._calibration_pattern()
            images = self.images_glob.get().strip()
            if not images:
                raise ValueError("请选择棋盘格图片目录，或填写图片路径模式。")
            if not self.intrinsics.get().strip():
                self._browse_save(self.intrinsics)
            output = self.intrinsics.get().strip()
            if not output:
                return
        except Exception as exc:
            messagebox.showerror("标定输入错误", str(exc), parent=self)
            return
        self._background("正在计算相机内参…", lambda: calibrate_intrinsics(images, output, pattern=pattern, square_mm=square))

    def _calibrate_plane(self) -> None:
        if self.busy:
            return
        try:
            pattern, square = self._calibration_pattern()
            frame = int(self.calib_frame.get())
            video, intrinsics = self.video.get().strip(), self.intrinsics.get().strip()
            if frame < 0 or not Path(video).is_file() or not Path(intrinsics).is_file():
                raise ValueError("请提供有效视频、内参 JSON 和非负视频帧号。")
            if not self.homography.get().strip():
                self._browse_save(self.homography)
            output = self.homography.get().strip()
            if not output:
                return
        except Exception as exc:
            messagebox.showerror("标定输入错误", str(exc), parent=self)
            return
        self._background("正在计算平面单应矩阵…", lambda: calibrate_plane(video, intrinsics, output, frame_index=frame, pattern=pattern, square_mm=square))

    def _browse_result(self) -> None:
        directory = filedialog.askdirectory(parent=self, title="选择包含 alignment_report.json 的结果目录", initialdir=str(PROJECT_ROOT / "output"))
        if directory:
            try:
                self._load_result(Path(directory))
            except Exception as exc:
                messagebox.showerror("结果载入失败", str(exc), parent=self)

    def _load_result(self, directory: Path) -> None:
        if not (directory / "alignment_report.json").is_file() and (directory / "video_analysis_report.json").is_file():
            report = json.loads((directory / "video_analysis_report.json").read_text(encoding="utf-8"))
            self.current_result = directory.resolve()
            self.result_path.set(str(self.current_result))
            for variable in self.result_metrics.values():
                variable.set("—")
            self.result_metrics["status"].set("视频分析完成" if report["status"] == "video_analysis_complete" else "处理失败")
            self.result_note.set(f"独立视频结果 · 坐标系 {report.get('coordinate_system', '原始帧 TTL')} · 毫米坐标 {report.get('millimeter_coordinates', False)}")
            tables = [name for name in ("tracking.csv", "video_ttl.csv", "frame_timing.csv") if (directory / name).is_file()]
            self.table_combo.configure(values=tables)
            self.table_name.set(tables[0] if tables else "")
            self._show_table()
            self.book.select(self.results)
            self.after_idle(self._render_preview)
            return
        report = json.loads((directory / "alignment_report.json").read_text(encoding="utf-8-sig"))
        if not isinstance(report, dict):
            raise ValueError("对齐报告应为 JSON 对象。")
        self.current_result = directory.resolve()
        self.result_path.set(str(self.current_result))
        alignment = report.get("alignment", {})
        status = report.get("status", "unknown")
        self.result_metrics["status"].set({"sync_validated": "同步通过", "full_fusion_generated": "融合已生成", "failed": "处理失败", "cancelled": "已取消"}.get(status, status))
        self.result_metrics["edges"].set(str(alignment.get("matched_edges", "—")))
        for key, field in (("rms", "rms_residual_s"), ("max", "max_residual_s")):
            value = alignment.get(field)
            self.result_metrics[key].set(f"{value * 1000:.2f} ms" if isinstance(value, (int, float)) else "—")
        if status in ("failed", "cancelled"):
            note = f"失败原因：{report.get('error', '查看报告获取详情。')}"
        else:
            interval = alignment.get("frame_interval_s")
            note = f"视频帧间隔 {interval * 1000:.2f} ms。" if isinstance(interval, (int, float)) else ""
            note += "结束标志已验证。" if alignment.get("end_marker_verified") else "结束标志未验证。"
            if status == "sync_validated":
                note += " 本次仅验证同步，反光点与毫米坐标需在标定后的配对录制中验证。"
            elif status == "full_fusion_generated":
                note += " 两种粒度的融合表已生成；请按实施计划人工抽查位置与 ID。"
        self.result_note.set(note)
        tables = [name for name in ("aligned_by_frame.csv", "aligned_by_emg_sample.csv", "tracking.csv", "frame_alignment.csv", "video_ttl.csv") if (directory / name).is_file()]
        self.table_combo.configure(values=tables)
        self.table_name.set(tables[0] if tables else "")
        self._show_table()
        self.book.select(self.results)
        self.after_idle(self._render_preview)

    def _schedule_preview(self, _event=None) -> None:
        if self._preview_after is not None:
            self.after_cancel(self._preview_after)
        self._preview_after = self.after(100, self._render_preview)

    def _render_preview(self) -> None:
        self._preview_after = None
        canvas = self.qc_canvas
        canvas.delete("all")
        width, height = max(300, canvas.winfo_width()), max(180, canvas.winfo_height())
        path = self.current_result / "qc_alignment.png" if self.current_result else None
        self.preview_image = None
        if path is None or not path.is_file():
            canvas.create_text(width / 2, height / 2, text="载入结果后显示 TTL 对齐诊断图", fill=MUTED, font=("Microsoft YaHei UI", 12))
            return
        try:
            import cv2
            import numpy as np
            image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("诊断图无法解码")
            scale = min((width - 16) / image.shape[1], (height - 16) / image.shape[0], 1.0)
            image = cv2.resize(image, (max(1, round(image.shape[1] * scale)), max(1, round(image.shape[0] * scale))), interpolation=cv2.INTER_AREA)
            rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            data = f"P6\n{rgb.shape[1]} {rgb.shape[0]}\n255\n".encode("ascii") + rgb.tobytes()
            self.preview_image = tk.PhotoImage(master=self, data=data, format="PPM")
            canvas.create_image(width / 2, height / 2, image=self.preview_image)
        except Exception as exc:
            canvas.create_text(width / 2, height / 2, text=f"诊断图预览失败：{exc}", fill=MUTED, width=width - 30)

    def _show_table(self) -> None:
        self.table.delete(*self.table.get_children())
        self.table.configure(columns=())
        if self.current_result is None or not self.table_name.get():
            self.table_note.set("当前结果没有可预览的数据表。")
            return
        try:
            with (self.current_result / self.table_name.get()).open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.reader(stream)
                names = next(reader)
                columns = [f"col_{i}" for i in range(len(names))]
                self.table.configure(columns=columns)
                for column, name in zip(columns, names):
                    self.table.heading(column, text=name)
                    self.table.column(column, width=max(115, min(220, len(name) * 8 + 24)), stretch=False)
                count = 0
                for row in reader:
                    self.table.insert("", "end", values=row)
                    count += 1
                    if count >= 100:
                        break
            self.table_note.set(f"{self.table_name.get()} · 预览前 {count} 行，完整数据保存在结果目录。")
        except Exception as exc:
            self.table_note.set(f"数据预览失败：{exc}")

    def _open_path(self, path: Path | None) -> None:
        try:
            if path is None:
                raise ValueError("请先完成处理，或载入历史结果。")
            open_local(path)
        except Exception as exc:
            messagebox.showerror("无法打开", str(exc), parent=self)

    def _open_result(self) -> None:
        self._open_path(self.current_result)

    def _open_qc(self) -> None:
        self._open_path(self.current_result / "qc_alignment.png" if self.current_result else None)

    def _open_table(self) -> None:
        self._open_path(self.current_result / self.table_name.get() if self.current_result and self.table_name.get() else None)

    def _prepare_review(self) -> None:
        if self.current_result is None:
            messagebox.showerror("尚无结果", "请先完成处理或载入历史结果。", parent=self)
            return
        from .review import prepare_review
        directory = self.current_result
        self._background("正在生成 30 帧人工抽查包…", lambda: prepare_review(directory))

    def _open_review(self) -> None:
        self._open_path(self.current_result / "manual_review" if self.current_result else None)

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._log(str(payload))
                    self.status.set(str(payload))
                    continue
                self.busy = False
                self.progress.stop()
                self.cancel_button.state(["disabled"])
                self._update_controls()
                directory = payload.get("directory")
                if directory and ((directory / "alignment_report.json").is_file() or (directory / "video_analysis_report.json").is_file()):
                    try:
                        self._load_result(directory)
                    except Exception as exc:
                        self._log(f"结果预览失败：{exc}")
                if kind == "error":
                    self.status.set("处理失败 · 请查看运行记录与报告")
                    self._log(payload["message"])
                    messagebox.showerror("处理失败", payload["message"], parent=self)
                else:
                    result = payload.get("result") or {}
                    message = "处理完成"
                    if "rms_reprojection_px" in result:
                        message = f"相机内参完成 · RMS {result['rms_reprojection_px']:.3f} px"
                    elif "rms_reprojection_mm" in result:
                        message = f"平面标定完成 · RMS {result['rms_reprojection_mm']:.3f} mm"
                    elif result.get("status") == "pending_human_review":
                        message = "30 帧抽查包已生成 · 请打开抽查包填写人工记录"
                    elif result.get("status") == "video_analysis_complete":
                        self.analysis_cache.set(result["config"]["output_dir"])
                        message = "视频分析完成 · 已交给时间对齐页，可复用提取结果"
                    elif result.get("status") == "replay_ready":
                        self.replay_page.set_engine(result["engine"])
                        message = "同步回放已就绪"
                    self.status.set(message)
                    self._log(message)
        except queue.Empty:
            pass
        self._pump_id = self.after(100, self._pump)

    def _close(self) -> None:
        if self.busy and not messagebox.askyesno("正在处理", "当前任务尚未完成，关闭窗口将中断本次处理。是否关闭？", parent=self):
            return
        if self.live_page.active and not messagebox.askyesno("采集正在运行", "关闭窗口会结束并保存当前记录。是否关闭？", parent=self):
            return
        try:
            self.live_page.shutdown()
            self.replay_page.shutdown()
        except Exception as exc:
            messagebox.showerror("记录尚未安全关闭", str(exc), parent=self)
            return
        self.after_cancel(self._pump_id)
        if self._preview_after is not None:
            self.after_cancel(self._preview_after)
        self.destroy()


def launch() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    NeuroMotionApp().mainloop()
